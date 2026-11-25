"""OFC-003 evidence-bound read-only extraction and immutable field reviews.

TXT/CSV/standard XLSX are parsed locally. OCR/ASR/PDF depend on an explicitly
trusted host-supplied adapter: never forward evidence or accept client-supplied
recognition results. Candidates are NEVER authoritative product/cost/quantity.
"""
from __future__ import annotations
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO, BytesIO
import json
import re
from typing import Callable
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.office_intake import ofc_002_service as intake
from backend.app.modules.office_extraction.ofc_003_models import OfficeExtraction, OfficeFieldReview

MAX_SPANS = 3000
MAX_FIELDS = 500
ALLOWED = frozenset(("product", "quantity", "unit", "price", "supplier"))
ALIASES = {
    "product": frozenset(("商品", "产品", "品名", "sku", "product")),
    "quantity": frozenset(("数量", "件数", "quantity", "qty")),
    "unit": frozenset(("单位", "unit")),
    "price": frozenset(("单价", "价格", "price")),
    "supplier": frozenset(("供应商", "supplier")),
}
_NUM = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CELL = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})\Z")
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class ExtractionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise ExtractionError(code)


def _canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _hash(value):
    return sha256(_canon(value).encode("utf-8")).hexdigest()


def _require_actor(db, actor, permission):
    if actor.account_id is None or actor.session_id is None:
        raise HTTPException(403, "office_extraction_person_required")
    db.flush()
    db.expire_all()
    authorize_scope(db, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission=permission)


def _loc_valid(loc, kind):
    if not isinstance(loc, dict) or loc.get("type") not in (
            "text_line", "cell", "page_bbox", "audio_interval"):
        _fail("office_extraction_location_invalid")
    t = loc["type"]
    if t == "text_line":
        if kind != "text" or type(loc.get("line")) is not int or not 1 <= loc["line"] <= 100000:
            _fail("office_extraction_location_invalid")
    elif t == "cell":
        if kind != "spreadsheet" or not isinstance(loc.get("sheet"), str) or not 1 <= len(loc["sheet"]) <= 100 or not isinstance(loc.get("cell"), str) or not _CELL.fullmatch(loc["cell"]):
            _fail("office_extraction_location_invalid")
    elif t == "page_bbox":
        box = loc.get("bbox")
        if kind not in ("image", "pdf") or type(loc.get("page")) is not int or not 1 <= loc["page"] <= 10000 or not isinstance(box, list) or len(box) != 4 or not all(type(v) in (int, float) and 0 <= v <= 1 for v in box) or not (box[0] < box[2] and box[1] < box[3]):
            _fail("office_extraction_location_invalid")
    else:
        if kind != "audio" or any(type(loc.get(p)) is not int or loc[p] < 0 for p in ("start_ms", "end_ms")) or loc["end_ms"] <= loc["start_ms"]:
            _fail("office_extraction_location_invalid")


def _span(raw, label, location, *, warning=None):
    if not isinstance(raw, str) or len(raw) > 500 or (
            label is not None and (not isinstance(label, str) or len(label) > 80)):
        _fail("office_extraction_span_invalid")
    return {"raw_text": raw, "label": label, "location": location, "warning": warning}


def _text_spans(raw):
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise ExtractionError("office_extraction_encoding_invalid") from exc
    if "\x00" in text:
        _fail("office_extraction_encoding_invalid")
    spans = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        label, sep, value = line.partition(":")
        if sep and label.strip().lower() in set().union(*ALIASES.values()):
            spans.append(_span(value, label.strip(), {"type": "text_line", "line": i}))
        else:
            spans.append(_span(line, None, {"type": "text_line", "line": i},
                               warning="unstructured_text"))
    return spans


def _col(num):
    name = ""
    while num:
        num, rem = divmod(num - 1, 26)
        name = chr(65 + rem) + name
    return name


def _table_spans(rows, *, sheet):
    spans, headers = [], {}
    for r, values in enumerate(rows, 1):
        if r > 10000 or len(values) > 120:
            _fail("office_extraction_limit")
        lowered = [str(v).strip().lower() for v in values]
        matched = {}
        for c, label in enumerate(lowered):
            keys = [key for key, aliases in ALIASES.items() if label in aliases]
            if len(keys) == 1:
                matched[c] = keys[0]
        if len(matched) >= 2:
            headers = matched
            for c, cell in enumerate(values, 1):
                spans.append(_span(str(cell), None, {"type": "cell", "sheet": sheet,
                                    "cell": f"{_col(c)}{r}"}, warning="header"))
            continue
        subtotal = any(str(v).strip().lower().startswith(
            ("合计", "总计", "subtotal", "total")) for v in values)
        for c, value in enumerate(values, 1):
            if value is None or str(value) == "":
                continue
            label = headers.get(c-1) if not subtotal else None
            spans.append(_span(str(value), label, {"type": "cell", "sheet": sheet,
                               "cell": f"{_col(c)}{r}"},
                               warning="subtotal_or_total" if subtotal else
                                       "unlabeled_cell" if label is None else None))
    return spans


def _csv_spans(raw):
    try:
        data = raw.decode("utf-8-sig", errors="strict")
        reader = csv.reader(StringIO(data, newline=""), strict=True)
        rows = []
        for cells in reader:
            # Physical line positions must remain verifiable. CSV quoted
            # multiline cells otherwise invalidate synthetic A3/B3 pointers.
            if any("\n" in cell or "\r" in cell for cell in cells):
                _fail("office_extraction_multiline_location_ambiguous")
            rows.append(cells)
        return _table_spans(rows, sheet="CSV")
    except (UnicodeError, csv.Error) as exc:
        raise ExtractionError("office_extraction_csv_invalid") from exc


def _xlsx_spans(raw):
    # Only OOXML spreadsheet data, never macros, formulas, external links, or
    # spreadsheet evaluation. Office Intake already screens active ZIP content.
    try:
        with ZipFile(BytesIO(raw)) as zipf:
            names = zipf.namelist()
            if len(names) > 2500:
                _fail("office_extraction_limit")
            shared = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(zipf.read("xl/sharedStrings.xml"))
                shared = ["".join(x.itertext()) for x in root.findall(_NS + "si")]
            sheets = sorted(n for n in names if re.fullmatch(
                r"xl/worksheets/sheet[0-9]+\.xml", n))
            if not sheets:
                _fail("office_extraction_sheet_missing")
            spans = []
            for path in sheets[:30]:
                root = ET.fromstring(zipf.read(path))
                # A merged range hides multiple source cells, not only its
                # top-left anchor; mark ALL overlapping candidates uncertain.
                merged = []
                for group in root.findall(".//" + _NS + "mergeCell"):
                    ref = group.attrib.get("ref", "")
                    ends = ref.split(":")
                    if len(ends) != 2 or not all(_CELL.fullmatch(x) for x in ends):
                        _fail("office_extraction_cell_invalid")
                    merged.append(tuple(ends))
                def point(address):
                    found = _CELL.fullmatch(address)
                    if found is None:
                        _fail("office_extraction_cell_invalid")
                    column = 0
                    for character in found.group(1):
                        column = column * 26 + ord(character) - 64
                    return int(found.group(2)), column
                def overlaps(address):
                    row_number, column_number = point(address)
                    for first, last in merged:
                        r1, c1 = point(first)
                        r2, c2 = point(last)
                        if min(r1, r2) <= row_number <= max(r1, r2) and (
                                min(c1, c2) <= column_number <= max(c1, c2)):
                            return True
                    return False
                grid = {}
                for cell in root.findall(".//" + _NS + "c"):
                    address = cell.attrib.get("r", "")
                    match = _CELL.fullmatch(address)
                    if match is None:
                        _fail("office_extraction_cell_invalid")
                    value_node = cell.find(_NS + "v")
                    formula = cell.find(_NS + "f")
                    inline = cell.find(_NS + "is")
                    if formula is not None:
                        value = "".join(formula.itertext())
                        warning = "formula_untrusted"
                    elif inline is not None:
                        value = "".join(inline.itertext())
                        warning = None
                    elif value_node is not None:
                        value = value_node.text or ""
                        if cell.attrib.get("t") == "s":
                            index = int(value)
                            if not 0 <= index < len(shared):
                                _fail("office_extraction_cell_invalid")
                            value = shared[index]
                        warning = None
                    else:
                        continue
                    r = int(match.group(2))
                    c = 0
                    for ch in match.group(1):
                        c = c * 26 + ord(ch) - 64
                    grid.setdefault(r, {})[c] = (value, warning, overlaps(address))
                headers = {}
                for r, cells in sorted(grid.items()):
                    guessed_headers = {
                        c: next(iter([key for key, aliases in ALIASES.items()
                                      if str(info[0]).strip().lower() in aliases]), None)
                        for c, info in cells.items()
                    }
                    if sum(v is not None for v in guessed_headers.values()) >= 2:
                        headers = guessed_headers
                        for c, (v, warning, merged_flag) in sorted(cells.items()):
                            spans.append(_span(str(v), None, {"type": "cell", "sheet": path,
                                "cell": f"{_col(c)}{r}"}, warning=warning or
                                "merged_header" if merged_flag else warning or "header"))
                        continue
                    subtotal = any(str(x[0]).lower().startswith(("合计","总计","subtotal","total"))
                                   for x in cells.values())
                    for c, (value, warning, merged_flag) in sorted(cells.items()):
                        spans.append(_span(str(value), headers.get(c) if not (warning or merged_flag or subtotal) else None,
                            {"type": "cell", "sheet": path, "cell": f"{_col(c)}{r}"},
                            warning=warning or ("merged_needs_review" if merged_flag else
                                "subtotal_or_total" if subtotal else
                                "unlabeled_cell" if not headers.get(c) else None)))
            return spans
    except (ValueError, KeyError, ET.ParseError, IndexError, OverflowError) as exc:
        raise ExtractionError("office_extraction_spreadsheet_invalid") from exc


def _classification(raw, label, warning):
    key = next((k for k, aliases in ALIASES.items()
                if label and label.strip().lower() in aliases), None)
    if key not in ALLOWED:
        return None
    flags = []
    if warning:
        flags.append(warning)
    if not raw.strip():
        flags.append("missing_value")
    normalized = None
    if key in ("quantity", "price"):
        if _NUM.fullmatch(raw) is None:
            flags.append("untrusted_number_or_sign")
        else:
            try:
                parsed = Decimal(raw)
                if parsed < 0 or not parsed.is_finite():
                    flags.append("untrusted_number_or_sign")
                else:
                    normalized = format(parsed, "f")
            except InvalidOperation:
                flags.append("untrusted_number_or_sign")
    else:
        # Even an exact product/supplier label is NOT a master-data mapping;
        # never bind product_id, supplier_id, or interpret unit conversions.
        flags.append("unbound_business_identity")
    return key, normalized, flags


def _build(evidence, raw, *, adapter=None):
    mime = evidence.content_type
    if mime == "text/plain":
        kind, key, spans = "text", "builtin-text", _text_spans(raw)
    elif mime == "text/csv":
        kind, key, spans = "spreadsheet", "builtin-csv", _csv_spans(raw)
    elif mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        kind, key, spans = "spreadsheet", "builtin-xlsx", _xlsx_spans(raw)
    else:
        kind = "pdf" if mime == "application/pdf" else (
            "audio" if mime.startswith("audio/") else "image")
        if adapter is None:
            _fail("office_extractor_unavailable")
        # Caller is trusted host wiring only. NOT exposed through HTTP.
        response = adapter(mime, raw)
        if not isinstance(response, dict) or response.get("source_kind") != kind:
            _fail("office_extractor_contract_invalid")
        key = response.get("extractor_key")
        version = response.get("extractor_version")
        spans = response.get("spans")
        if not isinstance(key, str) or not key.startswith("host-") or not isinstance(version, str) or not 1 <= len(version) <= 80:
            _fail("office_extractor_contract_invalid")
    if not isinstance(spans, list) or len(spans) > MAX_SPANS:
        _fail("office_extraction_limit")
    result = []
    for i, span in enumerate(spans):
        if not isinstance(span, dict) or set(span) - {"raw_text", "label", "location", "warning", "span_id"}:
            _fail("office_extraction_span_invalid")
        loc = span.get("location")
        _loc_valid(loc, kind)
        normalized = _span(span.get("raw_text"), span.get("label"), loc,
                           warning=span.get("warning"))
        normalized["span_id"] = f"s{i+1}"
        result.append(normalized)
    fields = []
    for s in result:
        check = _classification(s["raw_text"], s["label"], s["warning"])
        if check is None:
            continue
        key_name, normalized, flags = check
        fields.append({"field_id": f"f{len(fields)+1}", "field_key": key_name,
                       "span_id": s["span_id"], "raw_text": s["raw_text"],
                       "location": s["location"],
                       "normalized_value": normalized, "confidence": "unverified",
                       "state": "needs_review", "flags": flags})
        if len(fields) > MAX_FIELDS:
            _fail("office_extraction_limit")
    return {"source_kind": kind, "extractor_key": key,
            "extractor_version": "1" if key.startswith("builtin-") else version,
            "spans": result, "fields": fields,
            "warnings": ["manual_review_required",
                         "no_automatic_product_supplier_or_business_write"]}


def _out(row, replayed=False):
    return {"id": row.id, "evidence_id": row.evidence_id,
            "evidence_version": row.evidence_version,
            "evidence_sha256": row.evidence_sha256,
            "extractor_key": row.extractor_key,
            "extractor_version": row.extractor_version,
            "source_kind": row.source_kind, "content_sha256": row.content_sha256,
            "spans": row.content["spans"], "fields": row.content["fields"],
            "warnings": row.content["warnings"],
            "created_at": row.created_at, "replayed": replayed}


def extract_evidence(db: Session, actor: AuthenticatedSubject, *,
                     evidence_id: str, request_key: str,
                     adapter: Callable | None = None):
    _require_actor(db, actor, "office.extraction.submit")
    _require_actor(db, actor, "office.intake.read")
    if not isinstance(request_key, str) or not 1 <= len(request_key) <= 200:
        _fail("office_extraction_key_invalid")
    evidence, raw = intake.get_bytes(db, actor, evidence_id)
    result = _build(evidence, raw, adapter=adapter)
    body_sha = _hash(result)
    request_hash = _hash({"actor": actor.subject_id, "evidence_id": evidence.id,
                          "source_sha256": evidence.sha256,
                          "extraction_sha256": body_sha})
    scope = (OfficeExtraction.organization_id == actor.organization_id,
             OfficeExtraction.store_id == actor.store_id)
    previous = db.scalar(select(OfficeExtraction).where(
        *scope, OfficeExtraction.request_key == request_key))
    if previous:
        if previous.request_sha256 != request_hash:
            _fail("office_extraction_idempotency_conflict")
        get_extraction(db, actor, previous.id)
        return _out(previous, True)
    row = OfficeExtraction(
        id=str(uuid4()), organization_id=actor.organization_id,
        store_id=actor.store_id, evidence_id=evidence.id,
        evidence_version=evidence.version, evidence_sha256=evidence.sha256,
        extractor_key=result["extractor_key"],
        extractor_version=result["extractor_version"],
        source_kind=result["source_kind"], content_sha256=body_sha,
        content={"spans": result["spans"], "fields": result["fields"],
                 "warnings": result["warnings"]}, request_key=request_key,
        request_sha256=request_hash, created_by=actor.subject_id,
        created_at=datetime.now(timezone.utc))
    db.add(row)
    db.flush()
    record_event(db, actor_id=actor.subject_id, organization_id=actor.organization_id,
                 store_id=actor.store_id, action="office.extraction.created",
                 target_type="office_extraction", target_id=row.id,
                 idempotency_key="office-extraction:" + sha256(
                     (actor.organization_id + ":" + actor.store_id + ":" + request_key).encode()).hexdigest(),
                 details={"evidence_id": evidence.id, "evidence_sha256": evidence.sha256,
                          "content_sha256": body_sha, "extractor": result["extractor_key"]})
    return _out(row)


def get_extraction(db, actor, extraction_id):
    _require_actor(db, actor, "office.extraction.read")
    row = db.scalar(select(OfficeExtraction).where(
        OfficeExtraction.organization_id == actor.organization_id,
        OfficeExtraction.store_id == actor.store_id,
        OfficeExtraction.id == extraction_id))
    if row is None:
        _fail("office_extraction_not_found")
    evidence, _raw = intake.get_bytes(db, actor, row.evidence_id)
    if evidence.version != row.evidence_version or evidence.sha256 != row.evidence_sha256:
        _fail("office_extraction_source_changed")
    reconstructed = {"source_kind": row.source_kind,
                     "extractor_key": row.extractor_key,
                     "extractor_version": row.extractor_version,
                     "spans": row.content["spans"],
                     "fields": row.content["fields"],
                     "warnings": row.content["warnings"]}
    if _hash(reconstructed) != row.content_sha256:
        _fail("office_extraction_tampered")
    return row


def review_receipts(db, actor, extraction_id):
    row = get_extraction(db, actor, extraction_id)
    items = db.scalars(select(OfficeFieldReview).where(
        OfficeFieldReview.organization_id == actor.organization_id,
        OfficeFieldReview.store_id == actor.store_id,
        OfficeFieldReview.extraction_id == row.id,
    ).order_by(OfficeFieldReview.created_at, OfficeFieldReview.id)).all()
    return [_review_out(x) for x in items]


def review_field(db, actor, *, extraction_id, field_id, extraction_sha256,
                 expected_version, decision, corrected_text, reason, request_key):
    _require_actor(db, actor, "office.extraction.review")
    if (not isinstance(request_key, str) or not 1 <= len(request_key) <= 200
            or type(expected_version) is not int or expected_version != 0
            or decision not in ("confirmed", "rejected", "corrected")
            or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000
            or not isinstance(extraction_sha256, str) or not _SHA.fullmatch(extraction_sha256)):
        _fail("office_review_invalid")
    if decision == "corrected":
        if not isinstance(corrected_text, str) or not 1 <= len(corrected_text.strip()) <= 500:
            _fail("office_review_correction_invalid")
    elif corrected_text is not None:
        _fail("office_review_correction_invalid")
    row = get_extraction(db, actor, extraction_id)
    if row.content_sha256 != extraction_sha256:
        _fail("office_review_stale")
    field = next((x for x in row.content["fields"] if x["field_id"] == field_id), None)
    if field is None:
        _fail("office_review_field_not_found")
    payload = {"extraction": extraction_id, "hash": extraction_sha256, "field_id": field_id,
               "decision": decision, "corrected_text": corrected_text,
               "reason": reason.strip(), "actor": actor.subject_id,
               "expected_version": expected_version}
    fingerprint = _hash(payload)
    scope = (OfficeFieldReview.organization_id == actor.organization_id,
             OfficeFieldReview.store_id == actor.store_id)
    previous = db.scalar(select(OfficeFieldReview).where(
        *scope, OfficeFieldReview.request_key == request_key))
    if previous is not None:
        if previous.request_sha256 != fingerprint:
            _fail("office_review_idempotency_conflict")
        return _review_out(previous, True)
    if db.scalar(select(OfficeFieldReview.id).where(
            *scope, OfficeFieldReview.extraction_id == extraction_id,
            OfficeFieldReview.field_id == field_id)) is not None:
        _fail("office_review_version_conflict")
    receipt = OfficeFieldReview(
        id=str(uuid4()), organization_id=actor.organization_id,
        store_id=actor.store_id, extraction_id=extraction_id,
        extraction_sha256=extraction_sha256,
        field_id=field_id, decision=decision, corrected_text=corrected_text,
        reason=reason.strip(), reviewed_by=actor.subject_id,
        expected_version=expected_version, request_key=request_key,
        request_sha256=fingerprint, created_at=datetime.now(timezone.utc))
    db.add(receipt)
    db.flush()
    record_event(db, actor_id=actor.subject_id, organization_id=actor.organization_id,
                 store_id=actor.store_id, action="office.extraction.field_reviewed",
                 target_type="office_extraction", target_id=extraction_id,
                 idempotency_key="office-review:" + sha256(
                     (actor.organization_id + ":" + actor.store_id + ":" + request_key).encode()).hexdigest(),
                 details={"field_id": field_id, "decision": decision,
                          "extraction_sha256": extraction_sha256, "receipt_id": receipt.id})
    return _review_out(receipt)


def _review_out(r, replayed=False):
    return {"id": r.id, "extraction_id": r.extraction_id,
            "extraction_sha256": r.extraction_sha256, "field_id": r.field_id,
            "decision": r.decision, "corrected_text": r.corrected_text,
            "reason": r.reason, "reviewed_by": r.reviewed_by,
            "created_at": r.created_at, "version": 1,
            "replayed": replayed, "effect": "candidate_review_only"}


# Expose only a small authenticated employee workflow; machine adapters cannot
# be specified by HTTP. Successful writes commit record+audit together.
from typing import Annotated
from fastapi import APIRouter, Depends
from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.security import require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.office_extraction.ofc_003_schemas import (
    ExtractRequest, ReviewRequest, ExtractionEnvelope, ReviewEnvelope,
)

router = APIRouter(prefix="/office-extraction", tags=["office-extraction"])


def _http_error(exc: ExtractionError):
    if exc.code in ("office_extraction_not_found", "office_review_field_not_found"):
        status = 404
    elif exc.code in ("office_extraction_idempotency_conflict",
                      "office_review_idempotency_conflict",
                      "office_review_version_conflict", "office_review_stale",
                      "office_extraction_source_changed"):
        status = 409
    elif exc.code in ("office_extractor_unavailable",
                      "office_extraction_tampered"):
        status = 503
    else:
        status = 422
    return HTTPException(status, exc.code)


def _env(data, schema):
    return schema(success=True, data=data, code="OK", error=None,
                  user_message="候选解析仅供人工核对，未生成正式业务单据。",
                  hint=None)


@router.post("/extractions", response_model=ExtractionEnvelope, status_code=201)
def extraction_post(
    payload: ExtractRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    db: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
):
    try:
        result = extract_evidence(db, actor, evidence_id=payload.evidence_id,
                                  request_key=key)
        db.commit()
    except Exception as exc:
        db.rollback()
        if isinstance(exc, ExtractionError):
            raise _http_error(exc) from exc
        if isinstance(exc, IntegrityError):
            raise HTTPException(409, "office_extraction_idempotency_conflict") from exc
        raise
    return _env(result, ExtractionEnvelope)


@router.get("/extractions/{extraction_id}", response_model=ExtractionEnvelope)
def extraction_get(
    extraction_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    db: Annotated[Session, Depends(get_session)],
):
    try:
        row = get_extraction(db, actor, extraction_id)
        return _env({**_out(row),
                     "reviews": review_receipts(db, actor, extraction_id)},
                    ExtractionEnvelope)
    except ExtractionError as exc:
        raise _http_error(exc) from exc


@router.post("/extractions/{extraction_id}/review",
             response_model=ReviewEnvelope, status_code=201)
def extraction_review_post(
    extraction_id: str,
    payload: ReviewRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    db: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
):
    try:
        result = review_field(
            db, actor, extraction_id=extraction_id,
            field_id=payload.field_id, extraction_sha256=payload.extraction_sha256,
            expected_version=payload.expected_version,
            decision=payload.decision, corrected_text=payload.corrected_text,
            reason=payload.reason, request_key=key)
        db.commit()
    except Exception as exc:
        db.rollback()
        if isinstance(exc, ExtractionError):
            raise _http_error(exc) from exc
        if isinstance(exc, IntegrityError):
            raise HTTPException(409, "office_review_version_conflict") from exc
        raise
    return _env(result, ReviewEnvelope)
