"""OFC-002 restricted, immutable, scoped office evidence intake.

One database/source of truth for metadata and idempotency, one content-addressed
blob store for bytes. No OCR/parser, shell, macro, outbound channel or Agent tool.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.office_intake.ofc_002_models import OfficeEvidence, OfficeIntakeReceipt

MAX_BYTES = 12 * 1024 * 1024
MAX_TEXT = 128 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_CHANNELS = frozenset(("manual_upload", "manual_paste", "manual_export"))
_EXT = {
    ".txt": "text/plain", ".csv": "text/csv", ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".wav": "audio/wav",
    ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
}
_PDF_UNSAFE = (b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile",
               b"/RichMedia", b"/OpenAction", b"/AA", b"/XFA")


class OfficeIntakeError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str):
    raise OfficeIntakeError(code)


def _require(db: Session, actor: AuthenticatedSubject, permission: str):
    if actor.account_id is None or actor.session_id is None:
        raise HTTPException(403, "office_person_required")
    db.flush()
    db.expire_all()
    authorize_scope(db, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission=permission)


def _digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def _filename(name: str) -> tuple[str, str]:
    if (not isinstance(name, str) or not 1 <= len(name) <= 255
            or name != name.strip() or name in (".", "..")
            or "/" in name or "\\" in name or "\x00" in name
            or any(ord(c) < 32 for c in name)):
        _fail("office_filename_invalid")
    extension = Path(name).suffix.lower()
    if extension not in _EXT:
        _fail("office_type_unsupported")
    return name, extension


def _utf8(data: bytes):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        _fail("office_encoding_invalid")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        _fail("office_encoding_invalid")
    if "\x00" in text or any(ord(c) < 9 for c in text):
        _fail("office_encoding_invalid")
    return text


def _xlsx_safe(data: bytes):
    try:
        with ZipFile(BytesIO(data)) as package:
            members = package.infolist()
            if not 1 <= len(members) <= 2500:
                _fail("office_spreadsheet_unsafe")
            names = {m.filename for m in members}
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                _fail("office_spreadsheet_unsafe")
            for member in members:
                n = member.filename.replace("\\", "/")
                if (n.startswith("/") or ".." in n.split("/")
                        or any(x in n.lower() for x in ("vbaproject", "activex", "externallink",
                                                        "embeddings/", "connections.xml"))):
                    _fail("office_spreadsheet_unsafe")
                if member.file_size > 12 * 1024 * 1024:
                    _fail("office_spreadsheet_unsafe")
                if member.file_size > 0 and (
                        member.compress_size == 0
                        or member.file_size > 100 * member.compress_size):
                    _fail("office_spreadsheet_unsafe")
            if sum(m.file_size for m in members) > 60 * 1024 * 1024:
                _fail("office_spreadsheet_unsafe")
    except BadZipFile:
        _fail("office_spreadsheet_unsafe")


def _classify(filename: str, data: bytes) -> str:
    _, extension = _filename(filename)
    if not isinstance(data, bytes) or len(data) == 0:
        _fail("office_empty")
    if len(data) > MAX_BYTES:
        _fail("office_too_large")
    mime = _EXT[extension]
    if extension in (".txt", ".csv"):
        if len(data) > MAX_TEXT:
            _fail("office_text_too_large")
        _utf8(data)
    elif extension == ".pdf":
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
            _fail("office_type_mismatch")
        if any(token.lower() in data.lower() for token in _PDF_UNSAFE):
            _fail("office_document_active_content")
    elif extension == ".xlsx":
        _xlsx_safe(data)
    elif extension in (".jpg", ".jpeg"):
        if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
            _fail("office_type_mismatch")
    elif extension == ".png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or b"IEND" not in data[-32:]:
            _fail("office_type_mismatch")
    elif extension == ".webp":
        if not (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
            _fail("office_type_mismatch")
    elif extension == ".wav":
        if not (data[:4] == b"RIFF" and data[8:12] == b"WAVE"):
            _fail("office_type_mismatch")
    elif extension == ".mp3":
        if not (data.startswith(b"ID3") or (len(data) > 2 and data[0] == 255
                                             and data[1] & 0xE0 == 0xE0)):
            _fail("office_type_mismatch")
    elif extension == ".ogg":
        if not data.startswith(b"OggS"):
            _fail("office_type_mismatch")
    return mime


def _storage_root() -> Path:
    return Path(os.environ.get("OFFICE_INTAKE_ROOT", "data-local/office-intake")).resolve()


def _key(org: str, store: str, digest: str) -> str:
    if not _ID.fullmatch(org) or not _ID.fullmatch(store):
        _fail("office_scope_path_invalid")
    return f"{org}/{store}/{digest[:2]}/{digest}.blob"


def _write_immutable(data: bytes, key: str):
    root = _storage_root()
    path = (root / key).resolve()
    if root not in path.parents:
        _fail("office_path_escape")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".office-staged-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, path)  # atomic exclusive publication, no overwrite
        except FileExistsError:
            if not path.is_file() or sha256(path.read_bytes()).digest() != sha256(data).digest():
                _fail("office_blob_integrity_conflict")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _out(e: OfficeEvidence) -> dict:
    return {
        "id": e.id, "version": e.version, "sha256": e.sha256,
        "byte_size": e.byte_size, "content_type": e.content_type,
        "original_name": e.original_name, "source_channel": e.source_channel,
        "created_at": e.created_at,
        "created_by": e.created_by,
    }


def receive(db: Session, actor: AuthenticatedSubject, *,
            filename: str, data: bytes, source_channel: str,
            idempotency_key: str, now: datetime | None = None) -> dict:
    _require(db, actor, "office.intake.write")
    if source_channel not in _CHANNELS:
        _fail("office_source_invalid")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
        _fail("office_idempotency_invalid")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        _fail("office_time_invalid")
    name, extension = _filename(filename)
    if source_channel == "manual_paste" and extension != ".txt":
        _fail("office_paste_type_invalid")
    mime = _classify(filename, data)
    digest = sha256(data).hexdigest()
    request_digest = _digest({
        "actor": actor.subject_id, "sha256": digest, "name": name,
        "source": source_channel, "type": mime,
    })
    scope = (OfficeIntakeReceipt.organization_id == actor.organization_id,
             OfficeIntakeReceipt.store_id == actor.store_id)
    existing_receipt = db.scalar(select(OfficeIntakeReceipt).where(
        *scope, OfficeIntakeReceipt.idempotency_key == idempotency_key))
    if existing_receipt is not None:
        if existing_receipt.request_digest != request_digest:
            _fail("office_idempotency_conflict")
        evidence = db.get(OfficeEvidence, existing_receipt.evidence_id)
        if evidence is None:
            _fail("office_receipt_incomplete")
        root = _storage_root()
        path = (root / evidence.storage_key).resolve()
        if root not in path.parents or not path.is_file():
            _fail("office_blob_missing")
        if sha256(path.read_bytes()).hexdigest() != evidence.sha256:
            _fail("office_blob_corrupt")
        return {**_out(evidence), "receipt_id": existing_receipt.id,
                "source_channel": existing_receipt.source_channel, "replayed": True}
    e = db.scalar(select(OfficeEvidence).where(
        OfficeEvidence.organization_id == actor.organization_id,
        OfficeEvidence.store_id == actor.store_id,
        OfficeEvidence.sha256 == digest))
    if e is None:
        key = _key(actor.organization_id, actor.store_id, digest)
        _write_immutable(data, key)
        candidate = OfficeEvidence(
            id=str(uuid4()), organization_id=actor.organization_id,
            store_id=actor.store_id, version=1, sha256=digest,
            byte_size=len(data), content_type=mime,
            original_name=name, source_channel=source_channel, storage_key=key,
            created_by=actor.subject_id, created_at=current)
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
        except IntegrityError:
            e = db.scalar(select(OfficeEvidence).where(
                OfficeEvidence.organization_id == actor.organization_id,
                OfficeEvidence.store_id == actor.store_id,
                OfficeEvidence.sha256 == digest))
            if e is None:
                raise
        else:
            e = candidate
    if e.content_type != mime or e.byte_size != len(data):
        _fail("office_blob_integrity_conflict")
    # A content-addressed row must not be treated as valid if its on-disk
    # original was lost or changed. Re-upload cannot silently certify it.
    root = _storage_root()
    path = (root / e.storage_key).resolve()
    if root not in path.parents or not path.is_file():
        _fail("office_blob_missing")
    if sha256(path.read_bytes()).hexdigest() != e.sha256:
        _fail("office_blob_corrupt")
    receipt = OfficeIntakeReceipt(
        id=str(uuid4()), organization_id=actor.organization_id,
        store_id=actor.store_id, evidence_id=e.id, submitted_by=actor.subject_id,
        idempotency_key=idempotency_key, request_digest=request_digest,
        source_channel=source_channel, version=1, submitted_at=current)
    try:
        with db.begin_nested():
            db.add(receipt)
            db.flush()
    except IntegrityError:
        matching = db.scalar(select(OfficeIntakeReceipt).where(
            *scope, OfficeIntakeReceipt.idempotency_key == idempotency_key))
        if matching is None or matching.request_digest != request_digest:
            _fail("office_idempotency_conflict")
        receipt = matching
        replayed = True
    else:
        replayed = False
        record_event(db, actor_id=actor.subject_id,
                     organization_id=actor.organization_id, store_id=actor.store_id,
                     action="office.intake.received", target_type="office_evidence",
                     target_id=e.id, idempotency_key="office:" + sha256(idempotency_key.encode("utf-8")).hexdigest(),
                     details={"sha256": digest, "receipt_id": receipt.id,
                              "source": source_channel, "byte_size": len(data)},
                     occurred_at=current)
    return {**_out(e), "receipt_id": receipt.id,
            "source_channel": receipt.source_channel, "replayed": replayed}


def list_evidence(db: Session, actor: AuthenticatedSubject, *, limit: int = 50) -> list[dict]:
    _require(db, actor, "office.intake.read")
    if type(limit) is not int or not 1 <= limit <= 100:
        _fail("office_limit_invalid")
    rows = db.scalars(select(OfficeEvidence).where(
        OfficeEvidence.organization_id == actor.organization_id,
        OfficeEvidence.store_id == actor.store_id,
    ).order_by(OfficeEvidence.created_at.desc(), OfficeEvidence.id).limit(limit))
    return [_out(e) for e in rows]


def get_evidence(db: Session, actor: AuthenticatedSubject, evidence_id: str) -> OfficeEvidence:
    _require(db, actor, "office.intake.read")
    e = db.scalar(select(OfficeEvidence).where(
        OfficeEvidence.organization_id == actor.organization_id,
        OfficeEvidence.store_id == actor.store_id, OfficeEvidence.id == evidence_id))
    if e is None:
        _fail("office_evidence_not_found")
    return e


def get_bytes(db: Session, actor: AuthenticatedSubject, evidence_id: str) -> tuple[OfficeEvidence, bytes]:
    e = get_evidence(db, actor, evidence_id)
    root = _storage_root()
    path = (root / e.storage_key).resolve()
    if root not in path.parents or not path.is_file() or path.is_symlink():
        _fail("office_blob_missing")
    data = path.read_bytes()
    if len(data) != e.byte_size or sha256(data).hexdigest() != e.sha256:
        _fail("office_blob_corrupt")
    return e, data
