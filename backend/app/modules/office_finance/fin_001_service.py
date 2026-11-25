"""FIN-001: immutable synthetic reconciliation workpapers; no upstream write."""
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
import json
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.command_guard import lock_employee_authority
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.office_intake.ofc_002_service import get_bytes as evidence_bytes, OfficeIntakeError
from backend.app.modules.office_finance.fin_001_models import FinanceWorkpaper
from backend.app.modules.office_finance.fin_001_schemas import WorkpaperCreate, WorkpaperEnvelope

router = APIRouter(prefix="/office-finance", tags=["office-finance"])
_MONEY = re.compile(r"-?(?:0|[1-9][0-9]{0,14})\.[0-9]{2}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")

def _fail(code: str, status: int = 409):
    raise HTTPException(status, code)

def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

def _text(value, label):
    if not value or value != value.strip() or any(ord(c) < 32 for c in value):
        _fail("finance_" + label + "_invalid", 422)
    return value

def _date(value, label):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value: raise ValueError()
        return parsed
    except ValueError:
        _fail("finance_" + label + "_invalid", 422)

def _money(value, label, *, nonnegative=False):
    if value is None: return None
    if not _MONEY.fullmatch(value): _fail("finance_" + label + "_invalid", 422)
    amount = Decimal(value)
    if nonnegative and amount < 0: _fail("finance_" + label + "_invalid", 422)
    return amount

def _normalize(body: WorkpaperCreate) -> dict:
    start, end = _date(body.period_start, "period_start"), _date(body.period_end, "period_end")
    if start > end: _fail("finance_period_invalid", 422)
    try:
        observed = datetime.fromisoformat(body.observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None or observed.utcoffset() is None: raise ValueError()
    except ValueError:
        _fail("finance_observed_at_invalid", 422)
    if observed.date() < end: _fail("finance_source_before_period_end", 422)
    opening, closing = _money(body.opening_balance, "opening"), _money(body.closing_balance, "closing")
    seen, entries = set(), []
    with localcontext() as ctx:
        ctx.prec = 40
        net = Decimal("0.00")
        has_missing = False
        for line in body.entries:
            source_id = _text(line.source_id, "source_id")
            if source_id in seen: _fail("finance_duplicate_source_id", 422)
            seen.add(source_id)
            if not start <= _date(line.date, "entry_date") <= end: _fail("finance_entry_outside_period", 422)
            amount = _money(line.amount, "amount", nonnegative=True)
            if amount is None: has_missing = True
            else:
                increases = "debit" if body.balance_convention == "debit_increases" else "credit"
                net += amount if line.direction == increases else -amount
            entries.append({**line.model_dump(), "source_id": source_id,
                            "source_version": _text(line.source_version, "source_version"),
                            "evidence_locator": _text(line.evidence_locator, "evidence_locator"),
                            "evidence_verification": "unverified", "match_state": "unmatched"})
        if body.complete and (opening is None or closing is None or has_missing):
            _fail("finance_incomplete_source", 422)
        known = body.complete and opening is not None and closing is not None and not has_missing and body.balance_convention is not None
        calculated = opening + net if known else None
        difference = closing - calculated if calculated is not None and closing is not None else None
    return {"source_kind": body.source_kind, "source_snapshot_id": _text(body.source_snapshot_id, "snapshot_id"),
            "source_version": _text(body.source_version, "source_version"),
            "observed_at": observed.astimezone(timezone.utc).isoformat(),
            "period_start": body.period_start, "period_end": body.period_end,
            "party_type": body.party_type, "party_ref": _text(body.party_ref, "party_ref"),
            "currency": body.currency, "opening_balance": body.opening_balance,
            "closing_balance": body.closing_balance, "complete": body.complete,
            "balance_convention": body.balance_convention,
            "entries": entries, "calculated_closing": f"{calculated:.2f}" if calculated is not None else None,
            "closing_difference": f"{difference:.2f}" if difference is not None else None,
            "warnings": (["incomplete_source"] if not body.complete or opening is None or closing is None or has_missing else []) +
                        (["unconfirmed_balance_convention"] if body.balance_convention is None else []), "formal_accounting": False,
            "disclaimer": "合成数据；差异候选待人工核对，不代表正式财务事实。"}

def _human(db, actor, permission):
    if actor.account_id is None or actor.session_id is None: _fail("finance_human_session_required", 403)
    db.flush(); db.expire_all()
    for cap in (permission, "office.intake.read"):
        authorize_scope(db, actor, organization_id=actor.organization_id, store_id=actor.store_id, permission=cap)
    if db.scalar(select(Subject.subject_type).where(Subject.organization_id == actor.organization_id,
                                                     Subject.id == actor.subject_id)) != "person":
        _fail("finance_person_required", 403)

def _source(db, actor, evidence_id, digest):
    if not _SHA.fullmatch(digest): _fail("finance_source_digest_invalid", 422)
    try:
        evidence, content = evidence_bytes(db, actor, evidence_id)
    except OfficeIntakeError as exc:
        raise HTTPException(409, "finance_source_unavailable_or_corrupt") from exc
    if evidence.sha256 != digest or sha256(content).hexdigest() != digest:
        _fail("finance_source_changed")

def _view(row, replayed=False):
    if _hash(row.contents) != row.content_sha256: _fail("finance_workpaper_integrity_error")
    return {"id": row.id, "workpaper_key": row.workpaper_key, "version": row.version,
            "parent_id": row.parent_id, "evidence_id": row.evidence_id,
            "evidence_sha256": row.evidence_sha256, "content_sha256": row.content_sha256,
            "contents": row.contents, "state": "needs_review", "created_by": row.created_by,
            "created_at": row.created_at.isoformat(), "replayed": replayed}

def create_workpaper(db: Session, actor: AuthenticatedSubject, body: WorkpaperCreate, key: str):
    if not isinstance(key, str) or not 1 <= len(key) <= 200 or key.strip() != key:
        _fail("finance_idempotency_invalid", 422)
    _human(db, actor, "office.finance.write")
    normalized = _normalize(body)
    workpaper_key = _text(body.workpaper_key, "workpaper_key")
    request_digest = _hash({"actor": actor.subject_id, "workpaper_key": workpaper_key,
                            "expected_version": body.expected_version, "evidence_id": body.evidence_id,
                            "evidence_sha256": body.evidence_sha256, "contents": normalized})
    lock_employee_authority(db, actor)
    _human(db, actor, "office.finance.write")
    _source(db, actor, body.evidence_id, body.evidence_sha256)
    scope = (FinanceWorkpaper.organization_id == actor.organization_id, FinanceWorkpaper.store_id == actor.store_id)
    old = db.scalar(select(FinanceWorkpaper).where(*scope, FinanceWorkpaper.request_key == key))
    if old:
        if old.request_digest != request_digest: _fail("finance_idempotency_conflict")
        return _view(old, replayed=True)
    prev = db.scalar(select(FinanceWorkpaper).where(*scope, FinanceWorkpaper.workpaper_key == workpaper_key)
                     .order_by(FinanceWorkpaper.version.desc()).limit(1))
    if (prev.version if prev else 0) != body.expected_version: _fail("finance_version_conflict")
    if prev and any(prev.contents.get(k) != normalized[k] for k in
                    ("party_type", "party_ref", "period_start", "period_end", "currency", "source_kind", "balance_convention")):
        _fail("finance_identity_changed")
    row = FinanceWorkpaper(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        workpaper_key=workpaper_key, version=body.expected_version + 1, parent_id=prev.id if prev else None,
        evidence_id=body.evidence_id, evidence_sha256=body.evidence_sha256, created_by=actor.subject_id,
        request_key=key, request_digest=request_digest, content_sha256=_hash(normalized),
        contents=normalized, created_at=datetime.now(timezone.utc))
    db.add(row); db.flush()
    record_event(db, actor_id=actor.subject_id, organization_id=actor.organization_id, store_id=actor.store_id,
        action="office.finance.workpaper_created", target_type="finance_workpaper", target_id=row.id,
        idempotency_key="fin001:create:" + row.id,
        details={"version": row.version, "content_sha256": row.content_sha256, "evidence_sha256": row.evidence_sha256})
    return _view(row)

def read_workpaper(db: Session, actor: AuthenticatedSubject, identity: str):
    _human(db, actor, "office.finance.read")
    lock_employee_authority(db, actor)
    _human(db, actor, "office.finance.read")
    row = db.scalar(select(FinanceWorkpaper).where(FinanceWorkpaper.id == identity,
        FinanceWorkpaper.organization_id == actor.organization_id, FinanceWorkpaper.store_id == actor.store_id))
    if row is None: _fail("finance_workpaper_not_found", 404)
    _source(db, actor, row.evidence_id, row.evidence_sha256)
    return _view(row)

@router.get("/workpapers", response_model=WorkpaperEnvelope)
def list_route(workpaper_key: str = Query(min_length=1, max_length=120), limit: int = Query(default=50, ge=1, le=50),
               before_version: int | None = Query(default=None, ge=2),
               actor: AuthenticatedSubject = Depends(require_authenticated_subject), db: Session = Depends(get_session)):
    _human(db, actor, "office.finance.read")
    lock_employee_authority(db, actor)
    _human(db, actor, "office.finance.read")
    workpaper_key = _text(workpaper_key, "workpaper_key")
    query = select(FinanceWorkpaper).where(
        FinanceWorkpaper.organization_id == actor.organization_id, FinanceWorkpaper.store_id == actor.store_id,
        FinanceWorkpaper.workpaper_key == workpaper_key)
    if before_version is not None: query = query.where(FinanceWorkpaper.version < before_version)
    selected = db.scalars(query.order_by(FinanceWorkpaper.version.desc()).limit(limit + 1)).all()
    rows = selected[:limit]
    next_before_version = rows[-1].version if len(selected) > limit else None
    verified = set()
    for row in rows:
        source = (row.evidence_id, row.evidence_sha256)
        if source not in verified:
            _source(db, actor, *source); verified.add(source)
    return WorkpaperEnvelope(success=True, data={"items": [_view(row) for row in rows], "next_before_version": next_before_version}, code="OK",
                             error=None, user_message="已回查底稿版本", hint=None)

@router.post("/workpapers", response_model=WorkpaperEnvelope, status_code=201)
def create_route(body: WorkpaperCreate, key: str = Depends(require_idempotency_key),
                 actor: AuthenticatedSubject = Depends(require_authenticated_subject), db: Session = Depends(get_session)):
    try:
        data = create_workpaper(db, actor, body, key); db.commit()
        return WorkpaperEnvelope(success=True, data=data, code="OK", error=None, user_message="已创建待核对底稿", hint=None)
    except BaseException:
        db.rollback(); raise

@router.get("/workpapers/{identity}", response_model=WorkpaperEnvelope)
def read_route(identity: str, actor: AuthenticatedSubject = Depends(require_authenticated_subject), db: Session = Depends(get_session)):
    data = read_workpaper(db, actor, identity)
    return WorkpaperEnvelope(success=True, data=data, code="OK", error=None, user_message="已读取待核对底稿", hint=None)
