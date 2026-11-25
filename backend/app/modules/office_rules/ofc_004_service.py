"""OFC-004: immutable, human-approved office rule candidates. Never ERP writes."""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
from uuid import uuid4
import json
import re
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.core.idempotency import require_idempotency_key
from backend.app.db.session import get_session
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.office_intake.ofc_002_models import OfficeEvidence
from backend.app.modules.office_rules.ofc_004_models import OfficeRuleVersion, OfficeRuleDecision
from backend.app.modules.office_rules.ofc_004_schemas import (
    RuleSubmit, RuleDecide, RuleEnvelope, DecisionEnvelope, ResolutionEnvelope,
)

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_AMOUNT = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?\Z")
_KINDS = {"customer_mapping","supplier_mapping","package_conversion",
          "return_policy","exchange_policy"}


class OfficeRuleError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise OfficeRuleError(code)


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail("office_rule_time_invalid")
    return value.astimezone(timezone.utc)


def _digest(value):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",",":"), allow_nan=False)
    except (ValueError, TypeError):
        _fail("office_rule_body_invalid")
    return sha256(raw.encode("utf-8")).hexdigest()


def _valid_string(value, field, limit=200):
    if not isinstance(value,str) or not 1 <= len(value) <= limit or value != value.strip():
        _fail("office_rule_" + field + "_invalid")
    return value


def _employee(db, actor, permission):
    if actor.account_id is None or actor.session_id is None:
        raise HTTPException(403,"office_rule_employee_required")
    db.flush()
    db.expire_all()
    authorize_scope(db,actor,organization_id=actor.organization_id,
                    store_id=actor.store_id,permission=permission)


def _valid_body(kind, scope, body):
    if kind not in _KINDS or not isinstance(body,dict):
        _fail("office_rule_kind_invalid")
    if kind=="package_conversion":
        if set(body)!={"product_id","source_unit","target_unit","numerator","denominator"}:
            _fail("office_rule_body_invalid")
        for key in ("product_id","source_unit","target_unit"):
            _valid_string(body[key],key)
        for key in ("numerator","denominator"):
            if type(body[key]) is not int or not 1 <= body[key] <= 1000000000:
                _fail("office_rule_factor_invalid")
        if body["source_unit"]==body["target_unit"]:
            _fail("office_rule_conversion_identity")
        if scope!="|".join(body[k] for k in ("product_id","source_unit","target_unit")):
            _fail("office_rule_scope_mismatch")
    elif kind in ("customer_mapping","supplier_mapping"):
        if set(body)!={"local_ref","external_ref"}:
            _fail("office_rule_body_invalid")
        _valid_string(body["local_ref"],"local_ref")
        _valid_string(body["external_ref"],"external_ref")
        if scope!=body["local_ref"]:
            _fail("office_rule_scope_mismatch")
    elif set(body)!={"requires_original_sale","requires_same_unit",
                     "requires_human_refund_confirmation"} or any(x is not True for x in body.values()):
        _fail("office_rule_policy_requires_human")
    _digest(body)


def _rule(db,actor,rule_id,lock=False):
    stmt=select(OfficeRuleVersion).where(
        OfficeRuleVersion.id==rule_id,
        OfficeRuleVersion.organization_id==actor.organization_id,
        OfficeRuleVersion.store_id==actor.store_id).execution_options(populate_existing=True)
    if lock: stmt=stmt.with_for_update()
    row=db.scalar(stmt)
    if row is None: _fail("office_rule_not_found")
    return row


def _status(db,actor,row):
    receipts=list(db.scalars(select(OfficeRuleDecision).where(
        OfficeRuleDecision.rule_id==row.id,
        OfficeRuleDecision.organization_id==actor.organization_id,
        OfficeRuleDecision.store_id==actor.store_id
    ).order_by(OfficeRuleDecision.decision_version).execution_options(populate_existing=True)))
    if not receipts:return "draft",0,None
    if len(receipts)>2 or [x.decision_version for x in receipts]!=list(range(1,len(receipts)+1)):
        _fail("office_rule_receipt_corrupt")
    if receipts[0].decision not in ("approved","rejected") or receipts[0].rule_version!=row.version:
        _fail("office_rule_receipt_corrupt")
    if len(receipts)==2 and (receipts[0].decision!="approved" or
          receipts[1].decision!="revoked" or receipts[1].rule_version!=row.version):
        _fail("office_rule_receipt_corrupt")
    return receipts[-1].decision,len(receipts),receipts[-1]


def _out_rule(db,actor,row,replayed=False):
    state,version,_=_status(db,actor,row)
    return {"id":row.id,"rule_key":row.rule_key,"version":row.version,
            "kind":row.kind,"scope_key":row.scope_key,"body_sha256":row.body_sha256,
            "source_ref":row.source_ref,"source_sha256":row.source_sha256,
            "valid_from":row.valid_from,"valid_until":row.valid_until,
            "decision_state":state,"decision_version":version,
            "submitted_by":row.submitted_by,"replayed":replayed,"effect":"candidate_only"}


def _out_decision(row,replayed=False):
    return {"id":row.id,"rule_id":row.rule_id,"rule_version":row.rule_version,
            "version":row.decision_version,"decision":row.decision,
            "input_sha256":row.input_sha256,"reason":row.reason,
            "decided_by":row.decided_by,"replayed":replayed,"effect":"review_only"}


def _audit(db,actor,action,target,key,details):
    record_event(db,actor_id=actor.subject_id,
        organization_id=actor.organization_id,store_id=actor.store_id,
        action=action,target_type="office_rule",target_id=target,
        idempotency_key="ofc4:"+sha256(
            (actor.organization_id+"|"+actor.store_id+"|"+action+"|"+key).encode()).hexdigest(),
        details=details)


def _verified_evidence(db,actor,source_ref,source_sha256):
    if not source_ref.startswith("office_evidence:"):
        _fail("office_rule_source_unverified")
    evidence_id=source_ref[len("office_evidence:"):]
    _valid_string(evidence_id,"source_id",36)
    record=db.scalar(select(OfficeEvidence).where(
        OfficeEvidence.id==evidence_id,
        OfficeEvidence.organization_id==actor.organization_id,
        OfficeEvidence.store_id==actor.store_id,
        OfficeEvidence.sha256==source_sha256,
        OfficeEvidence.version==1))
    if record is None:
        _fail("office_rule_source_unverified")


def propose_rule(db:Session,actor:AuthenticatedSubject,body:RuleSubmit,request_key:str):
    _employee(db,actor,"office.rules.manage")
    _valid_string(request_key,"request_key")
    if not _SHA.fullmatch(body.source_sha256):_fail("office_rule_source_invalid")
    _verified_evidence(db,actor,body.source_ref,body.source_sha256)
    _valid_body(body.kind,body.scope_key,body.rule_body)
    start=_utc(body.valid_from)
    end=_utc(body.valid_until) if body.valid_until else None
    if end is not None and end<=start:_fail("office_rule_window_invalid")
    payload={"rule_key":body.rule_key,"version":body.version,"kind":body.kind,
             "scope_key":body.scope_key,"rule_body":body.rule_body,
             "source_ref":body.source_ref,"source_sha256":body.source_sha256,
             "valid_from":start.isoformat(),"valid_until":end.isoformat() if end else None,
             "submitted_by":actor.subject_id}
    digest=_digest(payload)
    prior=db.scalar(select(OfficeRuleVersion).where(
        OfficeRuleVersion.organization_id==actor.organization_id,
        OfficeRuleVersion.store_id==actor.store_id,
        OfficeRuleVersion.submit_key==request_key))
    if prior:
        if prior.submit_digest!=digest:_fail("office_rule_idempotency_conflict")
        return _out_rule(db,actor,prior,True)
    latest=db.scalar(select(OfficeRuleVersion.version).where(
        OfficeRuleVersion.organization_id==actor.organization_id,
        OfficeRuleVersion.store_id==actor.store_id,
        OfficeRuleVersion.rule_key==body.rule_key
    ).order_by(OfficeRuleVersion.version.desc()).limit(1))
    if body.version!=(latest or 0)+1:_fail("office_rule_version_conflict")
    row=OfficeRuleVersion(id=str(uuid4()),organization_id=actor.organization_id,
        store_id=actor.store_id,rule_key=body.rule_key,version=body.version,
        kind=body.kind,scope_key=body.scope_key,rule_body=body.rule_body,
        source_ref=body.source_ref,source_sha256=body.source_sha256,
        body_sha256=_digest(body.rule_body),valid_from=start,valid_until=end,
        submitted_by=actor.subject_id,submit_key=request_key,submit_digest=digest,
        created_at=datetime.now(timezone.utc))
    db.add(row)
    db.flush()
    _audit(db,actor,"office.rules.proposed",row.id,request_key,
           {"version":row.version,"body_sha256":row.body_sha256})
    return _out_rule(db,actor,row)


def get_rule(db,actor,rule_id):
    _employee(db,actor,"office.rules.read")
    return _out_rule(db,actor,_rule(db,actor,rule_id))


def decide_rule(db:Session,actor:AuthenticatedSubject,rule_id:str,
                body:RuleDecide,request_key:str):
    _employee(db,actor,"office.rules.review")
    _valid_string(request_key,"request_key")
    _valid_string(body.reason,"reason",2000)
    if not _SHA.fullmatch(body.input_sha256):_fail("office_rule_input_invalid")
    row=_rule(db,actor,rule_id,lock=True)
    if row.submitted_by==actor.subject_id:_fail("office_rule_self_approval_denied")
    payload={"rule_id":row.id,"rule_version":row.version,
             "expected_version":body.expected_version,"decision":body.decision,
             "input_sha256":body.input_sha256,"reason":body.reason,
             "decider":actor.subject_id}
    digest=_digest(payload)
    prev=db.scalar(select(OfficeRuleDecision).where(
        OfficeRuleDecision.organization_id==actor.organization_id,
        OfficeRuleDecision.store_id==actor.store_id,
        OfficeRuleDecision.request_key==request_key))
    if prev:
        if prev.request_digest!=digest:_fail("office_rule_idempotency_conflict")
        return _out_decision(prev,True)
    state,version,_=_status(db,actor,row)
    if body.expected_version!=version:_fail("office_rule_version_conflict")
    if body.input_sha256!=row.body_sha256:_fail("office_rule_input_changed")
    if (state=="draft" and body.decision not in ("approved","rejected")) or (
        state=="approved" and body.decision!="revoked") or state in ("rejected","revoked"):
        _fail("office_rule_decision_invalid")
    receipt=OfficeRuleDecision(id=str(uuid4()),organization_id=actor.organization_id,
        store_id=actor.store_id,rule_id=row.id,rule_version=row.version,
        decision_version=version+1,decision=body.decision,
        input_sha256=body.input_sha256,reason=body.reason,
        decided_by=actor.subject_id,request_key=request_key,request_digest=digest,
        created_at=datetime.now(timezone.utc))
    db.add(receipt)
    db.flush()
    _audit(db,actor,"office.rules.decided",row.id,request_key,
           {"decision":receipt.decision,"version":receipt.decision_version,
            "receipt_id":receipt.id})
    return _out_decision(receipt)


def resolve_rule(db:Session,actor:AuthenticatedSubject,*,kind:str,scope_key:str,
                 at:datetime|None=None,quantity:str|None=None):
    """Read-only candidate resolution; ambiguity and missing evidence fail closed."""
    _employee(db,actor,"office.rules.read")
    if kind not in _KINDS:_fail("office_rule_kind_invalid")
    _valid_string(scope_key,"scope_key")
    moment=_utc(at) if at else datetime.now(timezone.utc)
    if kind=="package_conversion":
        if not isinstance(quantity,str) or len(quantity)>40 or not _AMOUNT.fullmatch(quantity):
            _fail("office_rule_quantity_invalid")
    elif quantity is not None:
        _fail("office_rule_quantity_invalid")
    rows=list(db.scalars(select(OfficeRuleVersion).where(
        OfficeRuleVersion.organization_id==actor.organization_id,
        OfficeRuleVersion.store_id==actor.store_id,
        OfficeRuleVersion.kind==kind,OfficeRuleVersion.scope_key==scope_key,
        OfficeRuleVersion.valid_from<=moment,
        (OfficeRuleVersion.valid_until.is_(None)|
         (OfficeRuleVersion.valid_until>moment))
    ).execution_options(populate_existing=True)))
    approved=[]
    for row in rows:
        status,_,receipt=_status(db,actor,row)
        if status=="approved" and receipt is not None:
            approved.append((row,receipt))
    base={"kind":kind,"scope_key":scope_key,"rule_id":None,"rule_version":None,
          "source_ref":None,"source_sha256":None,"input_sha256":None,
          "decision_evidence":None,"converted_quantity":None,
          "pending_human":True,"effect":"candidate_only"}
    if len(approved)!=1:
        conflict=len(approved)>1
        return {**base,"status":"needs_human_conflict" if conflict else "unknown_or_unapproved",
                "explanation":"overlapping_approved_rules" if conflict
                else "no_active_approved_rule"}
    row,receipt=approved[0]
    if row.body_sha256!=_digest(row.rule_body):
        _fail("office_rule_source_tampered")
    _verified_evidence(db,actor,row.source_ref,row.source_sha256)
    _valid_body(row.kind,row.scope_key,row.rule_body)
    output=None
    if kind=="package_conversion":
        from math import gcd
        n,d=row.rule_body["numerator"],row.rule_body["denominator"]
        d//=gcd(n,d)  # simplify before checking for exact decimal representation
        while d%2==0:d//=2
        while d%5==0:d//=5
        if d!=1:
            return {**base,"status":"unknown_or_unapproved",
                    "explanation":"nonterminating_factor_requires_human"}
        with localcontext() as ctx:
            # Inputs are bounded to 40 characters and factors to 1e9; default
            # Decimal(28) would silently round large terminating quantities.
            ctx.prec = 100
            converted = (Decimal(quantity) * Decimal(n) /
                         Decimal(row.rule_body["denominator"]))
            output = format(converted, "f")
    return {**base,"status":"resolved","rule_id":row.id,"rule_version":row.version,
            "source_ref":row.source_ref,"source_sha256":row.source_sha256,
            "input_sha256":row.body_sha256,
            "decision_evidence":receipt.request_digest,
            "converted_quantity":output,
            "explanation":"human_approved_candidate_only","pending_human":False}


router=APIRouter(prefix="/office-rules",tags=["office-rules"])
from typing import Annotated


def _http(exc:OfficeRuleError):
    code=404 if exc.code=="office_rule_not_found" else (
        409 if any(w in exc.code for w in ("conflict","changed","decision_invalid",
                   "self_approval","source_tampered")) else 422)
    return HTTPException(code,exc.code)


def _envelope(data,schema):
    return schema(success=True,data=data,code="OK",error=None,
                  user_message="仅供授权员工人工复核，未执行采购、改价或库存变动。",
                  hint=None)


@router.post("/rules",response_model=RuleEnvelope,status_code=201)
def rule_post(body:RuleSubmit,
    actor:Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
    db:Annotated[Session,Depends(get_session)],
    key:Annotated[str,Depends(require_idempotency_key)]):
    try:
        out=propose_rule(db,actor,body,key)
        db.commit()
        return _envelope(out,RuleEnvelope)
    except Exception as exc:
        db.rollback()
        if isinstance(exc,OfficeRuleError):raise _http(exc) from exc
        if isinstance(exc,IntegrityError):
            raise HTTPException(409,"office_rule_version_conflict") from exc
        raise


@router.get("/rules/{rule_id}",response_model=RuleEnvelope)
def rule_get(rule_id:str,
    actor:Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
    db:Annotated[Session,Depends(get_session)]):
    try:
        return _envelope(get_rule(db,actor,rule_id),RuleEnvelope)
    except OfficeRuleError as exc:raise _http(exc) from exc


@router.post("/rules/{rule_id}/decisions",response_model=DecisionEnvelope,status_code=201)
def rule_review(rule_id:str,body:RuleDecide,
    actor:Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
    db:Annotated[Session,Depends(get_session)],
    key:Annotated[str,Depends(require_idempotency_key)]):
    try:
        out=decide_rule(db,actor,rule_id,body,key)
        db.commit()
        return _envelope(out,DecisionEnvelope)
    except Exception as exc:
        db.rollback()
        if isinstance(exc,OfficeRuleError):raise _http(exc) from exc
        if isinstance(exc,IntegrityError):
            raise HTTPException(409,"office_rule_version_conflict") from exc
        raise


@router.get("/resolve",response_model=ResolutionEnvelope)
def rule_resolve(kind:str,scope_key:str,
    actor:Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
    db:Annotated[Session,Depends(get_session)],
    at:datetime|None=None,quantity:str|None=None):
    try:
        return _envelope(resolve_rule(db,actor,kind=kind,scope_key=scope_key,
                                      at=at,quantity=quantity),ResolutionEnvelope)
    except OfficeRuleError as exc:raise _http(exc) from exc
