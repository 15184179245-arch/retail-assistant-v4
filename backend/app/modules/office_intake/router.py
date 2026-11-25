"""OFC-002 employee-only routes; never forward uploaded material to a model."""
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.core.idempotency import require_idempotency_key
from backend.app.db.session import get_session
from backend.app.modules.office_intake import ofc_002_service as service
from backend.app.modules.office_intake.ofc_002_schemas import (
    PasteIn, EvidenceEnvelope, EvidenceListEnvelope, ReceiptEnvelope,
)

router = APIRouter(prefix="/office-intake", tags=["office-intake"])


def _http(error: service.OfficeIntakeError):
    if error.code == "office_evidence_not_found":
        code = 404
    elif error.code in ("office_idempotency_conflict", "office_blob_integrity_conflict"):
        code = 409
    elif error.code in ("office_too_large", "office_text_too_large"):
        code = 413
    elif error.code in ("office_blob_missing", "office_blob_corrupt", "office_receipt_incomplete"):
        code = 503
    else:
        code = 400
    return HTTPException(status_code=code, detail=error.code)


def _envelope(data, kind):
    return kind(success=True, data=data, code="OK", error=None,
                user_message="本店材料证据已处理；原件只用于人工复核。",
                hint=None)


@router.post("/evidence/text", response_model=ReceiptEnvelope, status_code=201)
def paste_text(
    body: PasteIn,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    db: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
) -> ReceiptEnvelope:
    try:
        result = service.receive(
            db, actor, filename="pasted-material.txt",
            data=body.text.encode("utf-8"), source_channel="manual_paste",
            idempotency_key=key)
        db.commit()
    except Exception as exc:
        db.rollback()
        if isinstance(exc, service.OfficeIntakeError):
            raise _http(exc) from exc
        raise
    return _envelope(result, ReceiptEnvelope)


@router.post("/evidence/file", response_model=ReceiptEnvelope, status_code=201)
async def upload_file(
    request: Request,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    db: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
    filename: Annotated[str, Query(min_length=1, max_length=255)],
) -> ReceiptEnvelope:
    # Limit both with and without Content-Length; never buffer an unbounded body.
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size_header = int(length)
        except ValueError as exc:
            raise HTTPException(400, "office_length_invalid") from exc
        if size_header > service.MAX_BYTES:
            raise HTTPException(413, "office_too_large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > service.MAX_BYTES:
            raise HTTPException(413, "office_too_large")
        chunks.append(chunk)
    try:
        result = service.receive(
            db, actor, filename=filename, data=b"".join(chunks),
            source_channel="manual_upload", idempotency_key=key)
        db.commit()
    except Exception as exc:
        db.rollback()
        if isinstance(exc, service.OfficeIntakeError):
            raise _http(exc) from exc
        raise
    return _envelope(result, ReceiptEnvelope)


@router.get("/evidence", response_model=EvidenceListEnvelope)
def list_evidence(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    db: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> EvidenceListEnvelope:
    try:
        items = service.list_evidence(db, actor, limit=limit)
    except service.OfficeIntakeError as exc:
        raise _http(exc) from exc
    return _envelope({"items": items}, EvidenceListEnvelope)


@router.get("/evidence/{evidence_id}", response_model=EvidenceEnvelope)
def evidence_detail(
    evidence_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    db: Annotated[Session, Depends(get_session)],
) -> EvidenceEnvelope:
    try:
        item = service.get_evidence(db, actor, evidence_id)
    except service.OfficeIntakeError as exc:
        raise _http(exc) from exc
    return _envelope(service._out(item), EvidenceEnvelope)


@router.get("/evidence/{evidence_id}/content",
            responses={200: {"content": {"application/octet-stream": {
                "schema": {"type": "string", "format": "binary"}}}}})
def evidence_content(
    evidence_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    db: Annotated[Session, Depends(get_session)],
) -> Response:
    try:
        item, data = service.get_bytes(db, actor, evidence_id)
    except service.OfficeIntakeError as exc:
        raise _http(exc) from exc
    # NEVER inline untrusted PDF/CSV/XLSX/audio, including on a same-site origin.
    safe_name = quote(item.original_name, safe="")
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{safe_name}",
                             "X-Content-Type-Options": "nosniff",
                             "Cache-Control": "no-store",
                             "Content-Security-Policy": "sandbox",
                             "X-Office-Evidence-SHA256": item.sha256})
