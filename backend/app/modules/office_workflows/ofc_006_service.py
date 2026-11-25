"""Fixed manual office workflow, backed by real domain records and receipts.

The caller owns one transaction. No LLM, scheduler, network or formal ERP
writer lives here. A user's explicit perform command may invoke a registered
local extraction/Task service; observation never replays that action.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.app.modules.agents.models import AgentRun
from backend.app.modules.audit.service import record_event
from backend.app.modules.audit.models import AuditEvent
from backend.app.modules.auth.service import has_usable_account
from backend.app.modules.identity.command_guard import (
    check_command_authority, lock_command_authority, lock_employee_authority,
)
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.office_intake import ofc_002_service as intake
from backend.app.modules.office_intake.ofc_002_models import OfficeIntakeReceipt
from backend.app.modules.office_extraction import ofc_003_service as extraction
from backend.app.modules.office_extraction.ofc_003_models import OfficeExtraction, OfficeFieldReview
from backend.app.modules.office_rules import ofc_004_service as rules
from backend.app.modules.tasks import service as tasks
from backend.app.modules.tasks.models import Task, TaskEvent
from backend.app.modules.approvals import service as approvals
from backend.app.modules.approvals.models import ApprovalRequest, ApprovalIdempotency
from backend.app.modules.office_workflows.ofc_006_models import (
    OfficeWorkflow, OfficeWorkflowStep, OfficeWorkflowReceipt, STEP_KINDS,
)
from backend.app.modules.office_workflows.ofc_006_schemas import (
    WorkflowCreateInput, WorkflowAdvanceInput, WorkflowControlInput,
    WorkflowApprovalInput, WorkflowContext, WorkflowReceipt, WorkflowApprovalReceipt,
    WorkflowEnvelope, WorkflowContextEnvelope, WorkflowApprovalEnvelope, Identifier,
    WorkflowReceiptPage, WorkflowReceiptPageEnvelope,
)
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.errors import ApiEnvelope
from backend.app.db.session import get_session

router = APIRouter(prefix="/office/workflows", tags=["Office workflows"],
    responses={status: {"model": ApiEnvelope} for status in (400, 401, 403, 404, 409, 422, 500, 503)})


class WorkflowError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise WorkflowError(code)


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _stamp(value):
    return None if value is None else _utc(value).isoformat(timespec="microseconds")


def _digest(value):
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise WorkflowError("workflow_evidence_invalid") from None
    return sha256(data).hexdigest()


def _clock(db):
    # SQLite's CURRENT_TIMESTAMP loses subsecond precision. Use its own
    # millisecond clock so adjacent human approval/Task receipts need not wait
    # an artificial whole second; this remains a database-sourced clock.
    sql = "SELECT clock_timestamp()" if db.get_bind().dialect.name == "postgresql" else "SELECT strftime('%Y-%m-%d %H:%M:%f','now')"
    value = db.execute(text(sql)).scalar_one()
    return _utc(datetime.fromisoformat(value) if isinstance(value, str) else value)


def _allow(db, actor, permission, now):
    if (not actor.account_id or not actor.session_id
            or type(actor.account_version) is not int or actor.account_version <= 0):
        _fail("workflow_human_required")
    authorize_scope(db, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                    permission=permission, now=now)


def _guard(db, actor, permission):
    lock_employee_authority(db, actor)
    now = _clock(db)
    _allow(db, actor, permission, now)
    return now


def _guard_write(db, actor, permission):
    # The workflow command remains employee-only even though the shared
    # authority lock also supports other kinds of principals.
    if type(actor) is not AuthenticatedSubject or actor.account_id is None or actor.session_id is None:
        raise HTTPException(403, "human_session_required")
    epoch = lock_command_authority(db, actor)
    now = _clock(db)
    _allow(db, actor, permission, now)
    return epoch, now


def _scope(model, actor):
    return model.organization_id == actor.organization_id, model.store_id == actor.store_id


def _key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or value.strip() != value:
        _fail("workflow_key_invalid")
    return value


def _typed(model, value):
    if not isinstance(value, model):
        _fail("workflow_input_invalid")
    try:
        return model.model_validate(value.model_dump(warnings=False))
    except (ValidationError, ValueError, TypeError):
        raise WorkflowError("workflow_input_invalid") from None


def _task(db, actor, ident, now):
    try:
        tasks.get_task_for_scope(db, actor, ident, now=now)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise WorkflowError("workflow_task_not_found") from None
        raise
    row = db.scalar(select(Task).where(*_scope(Task, actor), Task.id == ident)
        .execution_options(populate_existing=True).with_for_update())
    if row is None:
        _fail("workflow_task_not_found")
    return row


def _task_context(row):
    fields = ("id", "organization_id", "store_id", "created_by", "title", "description",
              "source", "proposal_ref", "reviewer_id", "decision_state", "accepted_by",
              "assigned_by", "assignee_id")
    values = {key: getattr(row, key) for key in fields}
    values.update({key: _stamp(getattr(row, key)) for key in ("created_at", "accepted_at", "assigned_at", "due_at")})
    return _digest(values)


def _task_events(db, actor, task):
    rows = list(db.scalars(select(TaskEvent).where(*_scope(TaskEvent, actor), TaskEvent.task_id == task.id)
        .order_by(TaskEvent.occurred_at, TaskEvent.id).execution_options(populate_existing=True)))
    if not rows:
        _fail("workflow_task_receipt_missing")
    return rows


def _task_state_digest(db, actor, row):
    events = _task_events(db, actor, row)
    # The entire ordered set is evidence. UUID order is never called a causal
    # version: state/decision fields and every event are bound together.
    evidence = [{"id": e.id, "actor": e.actor_id, "from": e.from_state, "to": e.to_state,
                 "decision": e.decision_state, "responsible": e.responsible_id,
                 "due_at": _stamp(e.due_at), "at": _stamp(e.occurred_at),
                 "reason_digest": _digest(e.reason)} for e in events]
    values = {key: getattr(row, key) for key in ("state", "decision_state", "started_by", "verified_by", "closed_by")}
    values.update({key: _stamp(getattr(row, key)) for key in ("started_at", "verified_at", "closed_at")})
    values.update(context_digest=_task_context(row), events=evidence,
                  verification_reason=_digest(row.verification_reason), close_reason=_digest(row.close_reason))
    return _digest(values)


def _rule_context(db, actor, ident, now):
    if ident is None:
        return None
    _allow(db, actor, "office.rules.read", now)
    row = rules._rule(db, actor, ident, lock=True)
    state, version, decision = rules._status(db, actor, row)
    if (state != "approved" or decision is None or decision.input_sha256 != row.body_sha256
            or _digest(row.rule_body) != row.body_sha256 or _utc(row.valid_from) > now
            or (row.valid_until is not None and _utc(row.valid_until) <= now)):
        _fail("workflow_rule_unavailable")
    rules._verified_evidence(db, actor, row.source_ref, row.source_sha256)
    source, _ = intake.get_bytes(db, actor, row.source_ref.removeprefix("office_evidence:"))
    if source.sha256 != row.source_sha256:
        _fail("workflow_rule_unavailable")
    return _digest({"id": row.id, "version": row.version, "body_digest": row.body_sha256,
                    "source": row.source_sha256, "decision_id": decision.id,
                    "decision_version": version, "input_digest": decision.input_sha256,
                    "valid_from": _stamp(row.valid_from), "valid_until": _stamp(row.valid_until)})


def _context(db, actor, task_id, evidence_id, rule_id, origin_run_id, now, *, origin_creator=None):
    _allow(db, actor, "office.intake.read", now)
    _allow(db, actor, "office.extraction.read", now)
    task = _task(db, actor, task_id, now)
    evidence, _raw = intake.get_bytes(db, actor, evidence_id)
    rule_digest = _rule_context(db, actor, rule_id, now)
    if origin_run_id is not None:
        run = db.scalar(select(AgentRun).where(*_scope(AgentRun, actor), AgentRun.id == origin_run_id))
        if (run is None or run.requester_id != (origin_creator or actor.subject_id)
                or run.task_id not in (None, task.id)):
            _fail("workflow_origin_invalid")
        # An ID is provenance only. No Run message/answer/tool argument is read
        # or used as evidence of execution here.
    return WorkflowContext.model_validate(dict(
        task_id=task.id, task_context_digest=_task_context(task), task_state_digest=_task_state_digest(db, actor, task),
        task_state=task.state, task_decision_state=task.decision_state, task_assignee_id=task.assignee_id,
        evidence_id=evidence.id, evidence_digest=evidence.sha256,
        rule_id=rule_id, rule_digest=rule_digest, origin_run_id=origin_run_id))


def read_context(db, actor, *, task_id, evidence_id, rule_id=None, origin_run_id=None):
    now = _guard(db, actor, "office.workflows.read")
    return _context(db, actor, task_id, evidence_id, rule_id, origin_run_id, now).model_dump()


def _workflow(db, actor, ident):
    row = db.scalar(select(OfficeWorkflow).where(*_scope(OfficeWorkflow, actor), OfficeWorkflow.id == ident)
        .execution_options(populate_existing=True).with_for_update())
    if row is None:
        _fail("workflow_not_found")
    return row


def _steps(db, actor, workflow):
    rows = list(db.scalars(select(OfficeWorkflowStep).where(*_scope(OfficeWorkflowStep, actor),
        OfficeWorkflowStep.workflow_id == workflow.id).order_by(OfficeWorkflowStep.step_no)
        .execution_options(populate_existing=True).with_for_update()))
    if len(rows) != 7 or [(s.step_no, s.kind) for s in rows] != list(enumerate(STEP_KINDS, 1)):
        _fail("workflow_receipt_corrupt")
    return rows


def _advance_receipt(db, actor, workflow, number):
    rows = list(db.scalars(select(OfficeWorkflowReceipt).where(*_scope(OfficeWorkflowReceipt, actor),
        OfficeWorkflowReceipt.workflow_id == workflow.id, OfficeWorkflowReceipt.operation == "advance",
        OfficeWorkflowReceipt.step_no == number).limit(2)))
    if len(rows) != 1:
        _fail("workflow_receipt_corrupt")
    return rows[0]


def _input(workflow, steps):
    if workflow.next_step_no == 8:
        return None
    prefix = steps[:workflow.next_step_no - 1]
    if any(s.state != "completed" or not s.output_digest for s in prefix):
        _fail("workflow_receipt_corrupt")
    return _digest({"workflow_id": workflow.id, "input_digest": workflow.input_digest,
                    "step_no": workflow.next_step_no,
                    "prefix": [{"step": s.step_no, "output_digest": s.output_digest} for s in prefix]})


def _creation_digest(actor, context, responsibilities, *, created_by=None):
    return _digest({"scope": [actor.organization_id, actor.store_id], "created_by": created_by or actor.subject_id,
        "template_key": "material_review.v1", "template_version": 1, "execution_mode": "manual_task",
        "task_id": context.task_id, "task_context_digest": context.task_context_digest,
        "evidence_id": context.evidence_id, "evidence_digest": context.evidence_digest,
        "rule_id": context.rule_id, "rule_digest": context.rule_digest, "origin_run_id": context.origin_run_id,
        "responsibilities": responsibilities})


def _visibility(db, actor, workflow, now):
    _allow(db, actor, "office.workflows.read", now)
    _allow(db, actor, "office.intake.read", now)
    _allow(db, actor, "office.extraction.read", now)
    _task(db, actor, workflow.task_id, now)
    intake.get_evidence(db, actor, workflow.source_evidence_id)
    if workflow.rule_id:
        _allow(db, actor, "office.rules.read", now)
    if workflow.next_step_no > 4:
        _allow(db, actor, "approvals.read", now)


def _live_context(db, actor, workflow, now):
    context = _context(db, actor, workflow.task_id, workflow.source_evidence_id,
        workflow.rule_id, workflow.origin_run_id, now, origin_creator=workflow.created_by)
    if (context.task_context_digest != workflow.task_context_digest
            or context.evidence_digest != workflow.source_evidence_digest
            or context.rule_digest != workflow.rule_digest):
        _fail("workflow_context_changed")
    return context


def _command_view(row):
    return dict(id=row.id, workflow_id=row.workflow_id, step_no=row.step_no, operation=row.operation,
        workflow_version=row.workflow_version, request_digest=row.request_digest,
        input_digest=row.input_digest, output_digest=row.output_digest, actor_id=row.actor_id,
        references=row.references, created_at=row.created_at)


def _view(db, actor, workflow, now, *, command=None, replayed=False):
    _visibility(db, actor, workflow, now)
    steps = _steps(db, actor, workflow)
    context, blocking = None, None
    try:
        context = _live_context(db, actor, workflow, now)
        _validate_chain(db, actor, workflow, steps, now, context=context)
    except WorkflowError as exc:
        blocking = exc.code.upper()
    except (intake.OfficeIntakeError, extraction.ExtractionError, rules.OfficeRuleError):
        blocking = "WORKFLOW_SOURCE_UNAVAILABLE"
    review_digest, approval_digest = None, None
    if steps[1].state == "completed":
        parsed = _advance_receipt(db, actor, workflow, 2).references
        try:
            review_digest = _review_facts(db, actor, workflow, parsed["extraction_id"], now,
                                           require_complete=False)["review_digest"]
        except (WorkflowError, intake.OfficeIntakeError, extraction.ExtractionError):
            blocking = blocking or "WORKFLOW_REVIEW_UNAVAILABLE"
    if workflow.next_step_no == 4:
        approval_digest = _input(workflow, steps)
    elif steps[3].state == "completed":
        approval_digest = _advance_receipt(db, actor, workflow, 4).input_digest
    values = dict(id=workflow.id, state=workflow.state, version=workflow.version,
        next_step_no=workflow.next_step_no, input_digest=workflow.input_digest,
        task_id=workflow.task_id, task_context_digest=workflow.task_context_digest,
        source_evidence_id=workflow.source_evidence_id, source_evidence_digest=workflow.source_evidence_digest,
        rule_id=workflow.rule_id, rule_digest=workflow.rule_digest, origin_run_id=workflow.origin_run_id,
        context=context, blocking_code=blocking, next_input_digest=_input(workflow, steps),
        steps=[dict(step_no=s.step_no, kind=s.kind, responsible_id=s.responsible_id, state=s.state,
                    version=s.version, input_digest=s.input_digest, output_digest=s.output_digest,
                    completed_at=s.completed_at) for s in steps],
        review_digest=review_digest, approval_input_digest=approval_digest,
        command_receipt=None if command is None else _command_view(command),
        created_at=workflow.created_at, updated_at=workflow.updated_at, closed_at=workflow.closed_at,
        replayed=replayed)
    return WorkflowReceipt.model_validate(values).model_dump()


def _checked_view(db, actor, workflow, now, *, command, permission, epoch, replayed=False):
    result = _view(db, actor, workflow, now, command=command, replayed=replayed)
    check_command_authority(db, actor, permission=permission, epoch=epoch, now=_clock(db))
    return result


def read_workflow(db, actor, ident):
    now = _guard(db, actor, "office.workflows.read")
    return _view(db, actor, _workflow(db, actor, ident), now)


def _command_digest(actor, operation, ident, body):
    return _digest({"scope": [actor.organization_id, actor.store_id], "actor": actor.subject_id,
                    "operation": operation, "workflow_id": ident, "input": body.model_dump(mode="json")})


def _replay(db, actor, key, digest):
    row = db.scalar(select(OfficeWorkflowReceipt).where(*_scope(OfficeWorkflowReceipt, actor),
        OfficeWorkflowReceipt.request_key == key))
    if row is not None and (row.request_digest != digest or row.actor_id != actor.subject_id):
        _fail("workflow_idempotency_conflict")
    return row


def _record(db, actor, workflow, operation, key, request_digest, input_digest, output_digest, references, now, *, step_no=None):
    row = OfficeWorkflowReceipt(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        workflow_id=workflow.id, step_no=step_no, operation=operation, workflow_version=workflow.version,
        request_key=key, request_digest=request_digest, input_digest=input_digest,
        output_digest=output_digest, actor_id=actor.subject_id, references=references, created_at=now)
    db.add(row)
    record_event(db, actor_id=actor.subject_id, organization_id=actor.organization_id, store_id=actor.store_id,
        action="office.workflow." + operation, target_type="office_workflow", target_id=workflow.id,
        idempotency_key="ofc6:" + _digest({"key": key, "scope": [actor.organization_id, actor.store_id]}),
        details={"receipt_id": row.id, "workflow_version": workflow.version,
                 "input_digest": input_digest, "output_digest": output_digest,
                 "step_no": step_no, "state": workflow.state}, occurred_at=now)
    db.flush()
    return row


def create_workflow(db: Session, actor, body: WorkflowCreateInput, key: str):
    body = _typed(WorkflowCreateInput, body)
    key = _key(key)
    epoch, now = _guard_write(db, actor, "office.workflows.manage")
    request_digest = _command_digest(actor, "create", None, body)
    previous = _replay(db, actor, key, request_digest)
    if previous is not None:
        return _checked_view(db, actor, _workflow(db, actor, previous.workflow_id), now,
            command=previous, permission="office.workflows.manage", epoch=epoch, replayed=True)
    context = _context(db, actor, body.task_id, body.evidence_id, body.rule_id, body.origin_run_id, now)
    if (context.task_state != "assigned" or context.task_decision_state != "accepted"
            or context.task_assignee_id is None):
        _fail("workflow_task_not_assigned")
    if (body.task_context_digest != context.task_context_digest or body.evidence_digest != context.evidence_digest
            or body.rule_digest != context.rule_digest):
        _fail("workflow_context_changed")
    for responsibility in body.responsibilities:
        if not has_usable_account(db, organization_id=actor.organization_id, store_id=actor.store_id,
                                  subject_id=responsibility.subject_id, now=now):
            _fail("workflow_responsible_unavailable")
    owners = {r.kind: r.subject_id for r in body.responsibilities}
    if owners["execution"] != context.task_assignee_id or owners["verification"] == context.task_assignee_id:
        _fail("workflow_task_responsibility_invalid")
    if owners["approval"] == actor.subject_id:
        _fail("workflow_self_approval_forbidden")
    if db.scalar(select(OfficeWorkflow.id).where(*_scope(OfficeWorkflow, actor), OfficeWorkflow.task_id == body.task_id)) is not None:
        _fail("workflow_task_already_bound")
    now = _clock(db)
    check_command_authority(db, actor, permission="office.workflows.manage", epoch=epoch, now=now)
    _allow(db, actor, "office.workflows.read", now)
    workflow = OfficeWorkflow(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        template_key="material_review.v1", template_version=1, execution_mode="manual_task",
        task_id=body.task_id, task_context_digest=context.task_context_digest,
        source_evidence_id=body.evidence_id, source_evidence_digest=context.evidence_digest,
        input_digest=_creation_digest(actor, context, [r.model_dump() for r in body.responsibilities]),
        rule_id=body.rule_id, rule_digest=body.rule_digest, origin_run_id=body.origin_run_id,
        create_key=key, created_by=actor.subject_id, state="active", version=1, next_step_no=1,
        created_at=now, updated_at=now)
    db.add(workflow)
    db.flush()
    for number, responsibility in enumerate(body.responsibilities, 1):
        db.add(OfficeWorkflowStep(organization_id=actor.organization_id, store_id=actor.store_id,
            workflow_id=workflow.id, step_no=number, kind=responsibility.kind,
            responsible_id=responsibility.subject_id, state="pending", version=1))
    db.flush()
    receipt = _record(db, actor, workflow, "create", key, request_digest, workflow.input_digest,
        _digest({"workflow_id": workflow.id, "input_digest": workflow.input_digest}),
        {"task_id": workflow.task_id, "evidence_id": workflow.source_evidence_id,
         "task_context_digest": workflow.task_context_digest}, now)
    return _checked_view(db, actor, workflow, now, command=receipt,
        permission="office.workflows.manage", epoch=epoch)


def _intake_facts(db, actor, workflow, receipt_id):
    row = db.scalar(select(OfficeIntakeReceipt).where(*_scope(OfficeIntakeReceipt, actor),
        OfficeIntakeReceipt.id == receipt_id, OfficeIntakeReceipt.evidence_id == workflow.source_evidence_id))
    if row is None or row.version != 1:
        _fail("workflow_intake_receipt_invalid")
    audit_id = _audit_proof(db, actor, "office.intake.received", row.evidence_id, row.submitted_by,
                            {"receipt_id": row.id, "sha256": workflow.source_evidence_digest})
    return {"receipt_id": row.id, "request_digest": row.request_digest, "audit_id": audit_id,
            "evidence_id": row.evidence_id, "evidence_digest": workflow.source_evidence_digest}


def _extraction_facts(db, actor, workflow, ident, claimed_digest=None):
    row = extraction.get_extraction(db, actor, ident)
    if (row.evidence_id != workflow.source_evidence_id or row.evidence_version != 1
            or row.evidence_sha256 != workflow.source_evidence_digest
            or (claimed_digest is not None and row.content_sha256 != claimed_digest)):
        _fail("workflow_extraction_binding_invalid")
    audit_id = _audit_proof(db, actor, "office.extraction.created", row.id, row.created_by,
                            {"evidence_id": row.evidence_id, "content_sha256": row.content_sha256})
    return {"extraction_id": row.id, "content_digest": row.content_sha256, "audit_id": audit_id,
            "evidence_id": row.evidence_id, "evidence_digest": row.evidence_sha256,
            "extractor_key": row.extractor_key, "extractor_version": row.extractor_version}


def _review_facts(db, actor, workflow, ident, now, *, require_complete=True):
    parsed = _advance_receipt(db, actor, workflow, 2).references
    if ident != parsed.get("extraction_id"):
        _fail("workflow_extraction_binding_invalid")
    _extraction_facts(db, actor, workflow, ident, parsed.get("content_digest"))
    row = db.scalar(select(OfficeExtraction).where(*_scope(OfficeExtraction, actor), OfficeExtraction.id == ident))
    fields = row.content.get("fields")
    if not isinstance(fields, list) or len(fields) > extraction.MAX_FIELDS:
        _fail("workflow_review_invalid")
    required = {field["field_id"] for field in fields}
    if len(required) != len(fields):
        _fail("workflow_review_invalid")
    reviews = list(db.scalars(select(OfficeFieldReview).where(*_scope(OfficeFieldReview, actor),
        OfficeFieldReview.extraction_id == ident).order_by(OfficeFieldReview.field_id, OfficeFieldReview.id)))
    accepted = bool(required) and {r.field_id for r in reviews} == required and len(reviews) == len(required)
    evidence = []
    for review in reviews:
        payload = dict(extraction=ident, hash=row.content_sha256, field_id=review.field_id,
            decision=review.decision, corrected_text=review.corrected_text, reason=review.reason,
            actor=review.reviewed_by, expected_version=0)
        if (review.extraction_sha256 != row.content_sha256 or review.expected_version != 0
                or review.request_sha256 != _digest(payload)):
            _fail("workflow_review_changed")
        if (review.decision not in ("confirmed", "corrected")
                or (review.decision == "corrected" and not (review.corrected_text or "").strip())):
            accepted = False
        audit_id = _audit_proof(db, actor, "office.extraction.field_reviewed", ident, review.reviewed_by,
            {"receipt_id": review.id, "field_id": review.field_id, "extraction_sha256": row.content_sha256,
             "decision": review.decision})
        evidence.append({"id": review.id, "field_id": review.field_id,
                         "request_digest": review.request_sha256, "at": _stamp(review.created_at), "audit_id": audit_id})
    if require_complete and not accepted:
        _fail("workflow_review_incomplete")
    return {"extraction_id": ident, "content_digest": row.content_sha256,
            "review_digest": _digest({"extraction_id": ident, "content_digest": row.content_sha256,
                                      "reviews": evidence}), "review_ids": [r.id for r in reviews]}


def _approval_facts(db, actor, workflow, steps, ident, now, *, fresh=False, lock=True):
    _allow(db, actor, "approvals.read", now)
    query = select(ApprovalRequest).where(*_scope(ApprovalRequest, actor),
        ApprovalRequest.id == ident).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    # The exact step-4 input is stable after it has been recorded; pausing or
    # later Task state changes do not silently mint a new approval binding.
    binding = (_input(workflow, steps) if workflow.next_step_no == 4
               else _advance_receipt(db, actor, workflow, 4).input_digest)
    if (row is None or row.subject_type != "office_workflow"
            or row.subject_id != workflow.id + ":" + binding
            or row.action_key != "office.workflow.execute.v1" or row.state != "approved"
            or row.requester_id != workflow.created_by or row.decided_by != steps[3].responsible_id
            or row.decided_by == row.requester_id or row.decided_at is None
            or not _utc(row.requested_at) <= _utc(row.decided_at) < _utc(row.expires_at)
            or (fresh and now >= _utc(row.expires_at))):
        _fail("workflow_approval_invalid")
    audit_id = _audit_proof(db, actor, "approval.approved", row.id, row.decided_by,
                            {"from_state": "requested", "to_state": "approved"})
    return {"approval_id": row.id, "input_digest": binding, "audit_id": audit_id,
            "decided_by": row.decided_by, "decided_at": _stamp(row.decided_at),
            "expires_at": _stamp(row.expires_at), "subject_type": row.subject_type,
            "subject_id": row.subject_id, "action_key": row.action_key}


def _task_phase_facts(db, actor, workflow, steps, kind, now):
    row = _task(db, actor, workflow.task_id, now)
    events = _task_events(db, actor, row)
    field, state, prior = {"execution": ("started", "in_progress", "assigned"),
                           "verification": ("verified", "verified", "in_progress"),
                           "closure": ("closed", "closed", "verified")}[kind]
    who, when = getattr(row, field + "_by"), getattr(row, field + "_at")
    number = STEP_KINDS.index(kind)
    if who != steps[number].responsible_id or when is None:
        _fail("workflow_task_proof_invalid")
    matches = [event for event in events if event.to_state == state and event.from_state == prior
               and event.actor_id == who and event.responsible_id == row.assignee_id
               and event.decision_state == "accepted" and _utc(event.occurred_at) >= _utc(when)]
    if len(matches) != 1:
        _fail("workflow_task_receipt_invalid")
    if kind == "execution":
        if row.state not in ("in_progress", "verified", "closed") or who != row.assignee_id:
            _fail("workflow_task_proof_invalid")
        approval = _advance_receipt(db, actor, workflow, 4).references
        facts = _approval_facts(db, actor, workflow, steps, approval["approval_id"], now)
        if not datetime.fromisoformat(facts["decided_at"]) <= _utc(when) < datetime.fromisoformat(facts["expires_at"]):
            _fail("workflow_approval_expired_at_execution")
    elif kind == "verification":
        if (row.state not in ("verified", "closed") or who == row.assignee_id
                or row.started_at is None or _utc(when) < _utc(row.started_at)):
            _fail("workflow_task_proof_invalid")
    elif row.state != "closed" or row.verified_at is None or _utc(when) < _utc(row.verified_at):
        _fail("workflow_task_proof_invalid")
    operation = {"execution": "start", "verification": "verify", "closure": "close"}[kind]
    audit_id = _audit_proof(db, actor, "task." + operation, row.id, who,
                            {"from_state": prior, "to_state": state, "responsible_id": row.assignee_id})
    return {"task_id": row.id, "phase": kind, "actor_id": who, "audit_id": audit_id,
            "at": _stamp(when), "event_id": matches[0].id,
            "event_digest": _digest({"id": matches[0].id, "reason": matches[0].reason,
                                     "at": _stamp(matches[0].occurred_at), "from": prior, "to": state})}


def _validate_chain(db, actor, workflow, steps, now, *, context=None):
    creation = db.scalar(select(OfficeWorkflowReceipt).where(*_scope(OfficeWorkflowReceipt, actor),
        OfficeWorkflowReceipt.workflow_id == workflow.id, OfficeWorkflowReceipt.workflow_version == 1))
    if (creation is None or creation.operation != "create" or creation.input_digest != workflow.input_digest
            or creation.actor_id != workflow.created_by):
        _fail("workflow_receipt_corrupt")
    if context is not None and _creation_digest(actor, context,
            [{"kind": s.kind, "subject_id": s.responsible_id} for s in steps],
            created_by=workflow.created_by) != workflow.input_digest:
        _fail("workflow_context_changed")
    for step in steps:
        if step.state != "completed":
            continue
        receipt = _advance_receipt(db, actor, workflow, step.step_no)
        references = receipt.references
        if step.kind == "intake":
            facts = _intake_facts(db, actor, workflow, references["receipt_id"])
        elif step.kind == "extraction":
            facts = _extraction_facts(db, actor, workflow, references["extraction_id"], references["content_digest"])
        elif step.kind == "review":
            facts = _review_facts(db, actor, workflow, references["extraction_id"], now)
        elif step.kind == "approval":
            facts = _approval_facts(db, actor, workflow, steps, references["approval_id"], now)
        else:
            facts = _task_phase_facts(db, actor, workflow, steps, step.kind, now)
        if (step.output_digest != _digest(facts) or receipt.output_digest != step.output_digest
                or receipt.input_digest != step.input_digest or receipt.actor_id != step.responsible_id):
            _fail("workflow_evidence_changed")


def _audit_proof(db, actor, action, target, who, expected):
    candidates = db.scalars(select(AuditEvent).where(*_scope(AuditEvent, actor),
        AuditEvent.action == action, AuditEvent.target_id == target, AuditEvent.actor_id == who))
    matching = [row.id for row in candidates if all(row.details.get(k) == v for k, v in expected.items())]
    if len(matching) != 1:
        _fail("workflow_domain_audit_missing")
    return matching[0]


def _seen(workflow, steps, body):
    if workflow.version != body.expected_version:
        _fail("workflow_version_conflict")
    value = _input(workflow, steps)
    if value is None or value != body.expected_input_digest:
        _fail("workflow_input_changed")
    return value


def control_workflow(db, actor, ident, body: WorkflowControlInput, key):
    body = _typed(WorkflowControlInput, body)
    key = _key(key)
    epoch, now = _guard_write(db, actor, "office.workflows.manage")
    request_digest = _command_digest(actor, "control", ident, body)
    previous = _replay(db, actor, key, request_digest)
    workflow = _workflow(db, actor, ident)
    _visibility(db, actor, workflow, now)
    if previous is not None:
        return _checked_view(db, actor, workflow, now, command=previous,
            permission="office.workflows.manage", epoch=epoch, replayed=True)
    steps = _steps(db, actor, workflow)
    value = _seen(workflow, steps, body)
    step = steps[workflow.next_step_no - 1]
    operation = body.operation
    if operation == "pause" and workflow.state == "active":
        workflow.state = "paused"
    elif operation == "resume" and workflow.state == "paused":
        context = _live_context(db, actor, workflow, now)
        _validate_chain(db, actor, workflow, steps, now, context=context)
        workflow.state = "active"
    elif operation in ("fail", "mark_unknown") and workflow.state in ("active", "paused", "needs_human"):
        workflow.state = "failed" if operation == "fail" else "needs_human"
        step.state = "failed" if operation == "fail" else "unknown"
        step.version += 1
    else:
        _fail("workflow_invalid_transition")
    now = _clock(db)
    check_command_authority(db, actor, permission="office.workflows.manage", epoch=epoch, now=now)
    _visibility(db, actor, workflow, now)
    workflow.version += 1
    workflow.updated_at = now
    row = _record(db, actor, workflow, operation, key, request_digest, value,
        _digest({"state": workflow.state, "reason_digest": _digest(body.reason)}),
        {"reason_digest": _digest(body.reason)}, now)
    return _checked_view(db, actor, workflow, now, command=row,
        permission="office.workflows.manage", epoch=epoch)


def _domain_key(actor, operation, key):
    return "ofc6-" + operation + ":" + _digest({"scope": [actor.organization_id, actor.store_id],
        "actor": actor.subject_id, "key": key})


def request_workflow_approval(db, actor, ident, body: WorkflowApprovalInput, key):
    body = _typed(WorkflowApprovalInput, body)
    key = _key(key)
    epoch, now = _guard_write(db, actor, "office.workflows.manage")
    workflow = _workflow(db, actor, ident)
    _visibility(db, actor, workflow, now)
    steps = _steps(db, actor, workflow)
    binding = _seen(workflow, steps, body)
    if workflow.created_by != actor.subject_id:
        _fail("workflow_creator_required")
    if workflow.state != "active" or workflow.next_step_no != 4:
        _fail("workflow_approval_request_invalid")
    context = _live_context(db, actor, workflow, now)
    _validate_chain(db, actor, workflow, steps, now, context=context)
    now = _clock(db)
    check_command_authority(db, actor, permission="office.workflows.manage", epoch=epoch, now=now)
    _allow(db, actor, "approvals.request", now)
    _visibility(db, actor, workflow, now)
    request_key = _domain_key(actor, "approval", key)
    previous = db.scalar(select(ApprovalIdempotency.id).where(*_scope(ApprovalIdempotency, actor),
        ApprovalIdempotency.idempotency_key == request_key))
    row = approvals.request_approval(db, actor, subject_type="office_workflow",
        subject_id=workflow.id + ":" + binding, action_key="office.workflow.execute.v1",
        reason=body.reason, expires_at=body.expires_at, idempotency_key=request_key, now=now)
    check_command_authority(db, actor, permission="office.workflows.manage", epoch=epoch, now=_clock(db))
    return dict(workflow_id=workflow.id, approval_id=row.id, input_digest=binding,
        subject_type=row.subject_type, subject_id=row.subject_id, action_key=row.action_key,
        state=row.state, expires_at=_utc(row.expires_at), replayed=previous is not None)


def advance_workflow(db, actor, ident, body: WorkflowAdvanceInput, key):
    """Verify one stage, or explicitly perform exactly one fixed local action."""
    body = _typed(WorkflowAdvanceInput, body)
    key = _key(key)
    epoch, now = _guard_write(db, actor, "office.workflows.advance")
    request_digest = _command_digest(actor, "advance", ident, body)
    previous = _replay(db, actor, key, request_digest)
    workflow = _workflow(db, actor, ident)
    _visibility(db, actor, workflow, now)
    if previous is not None:
        return _checked_view(db, actor, workflow, now, command=previous,
            permission="office.workflows.advance", epoch=epoch, replayed=True)
    steps = _steps(db, actor, workflow)
    input_digest = _seen(workflow, steps, body)
    if workflow.state not in ("active", "needs_human"):
        _fail("workflow_invalid_transition")
    step = steps[workflow.next_step_no - 1]
    proof = body.proof
    if proof.kind != step.kind:
        _fail("workflow_step_mismatch")
    if actor.subject_id != step.responsible_id:
        _fail("workflow_responsible_required")
    perform = getattr(proof, "action", "observe") == "perform"
    if workflow.state == "needs_human" and perform:
        _fail("workflow_unknown_cannot_replay")
    context = _live_context(db, actor, workflow, now)
    _validate_chain(db, actor, workflow, steps, now, context=context)
    permission = {
        "intake": "office.intake.read", "extraction": "office.extraction.read",
        "review": "office.extraction.review", "approval": "approvals.decide",
        "execution": "tasks.start", "verification": "tasks.verify", "closure": "tasks.close",
    }[step.kind]
    _allow(db, actor, permission, now)
    if step.kind == "intake":
        facts = _intake_facts(db, actor, workflow, proof.receipt_id)
    elif step.kind == "extraction":
        if perform:
            now = _clock(db)
            check_command_authority(db, actor, permission="office.workflows.advance", epoch=epoch, now=now)
            _allow(db, actor, "office.extraction.submit", now)
            result = extraction.extract_evidence(db, actor, evidence_id=workflow.source_evidence_id,
                request_key=_domain_key(actor, "extract", key))
            facts = _extraction_facts(db, actor, workflow, result["id"], result["content_sha256"])
        else:
            facts = _extraction_facts(db, actor, workflow, proof.extraction_id, proof.content_digest)
    elif step.kind == "review":
        facts = _review_facts(db, actor, workflow, proof.extraction_id, now)
        if facts["review_digest"] != proof.review_digest:
            _fail("workflow_review_changed")
    elif step.kind == "approval":
        facts = _approval_facts(db, actor, workflow, steps, proof.approval_id, now, fresh=True)
    else:
        task = _task(db, actor, workflow.task_id, now)
        if proof.task_id != task.id or proof.task_state_digest != _task_state_digest(db, actor, task):
            _fail("workflow_task_state_changed")
        if perform:
            now = _clock(db)
            check_command_authority(db, actor, permission="office.workflows.advance", epoch=epoch, now=now)
            _allow(db, actor, permission, now)
            if step.kind == "execution":
                approved = _advance_receipt(db, actor, workflow, 4).references
                facts = _approval_facts(db, actor, workflow, steps, approved["approval_id"], now, fresh=True)
                if datetime.fromisoformat(facts["decided_at"]) > now:
                    _fail("workflow_approval_not_effective")
                operation = tasks.start_task
            elif step.kind == "verification":
                operation = tasks.verify_task
            else:
                operation = tasks.close_task
            operation(db, actor, task_id=task.id, reason="Explicit manual office workflow action",
                idempotency_key=_domain_key(actor, "task", key), now=now)
        facts = _task_phase_facts(db, actor, workflow, steps, step.kind, now)
    # Domain work and the workflow receipt share this transaction. Recheck the
    # live clock immediately before recording; an expired actor cannot finish
    # an action merely because it started while the session was valid.
    now = _clock(db)
    check_command_authority(db, actor, permission="office.workflows.advance", epoch=epoch, now=now)
    _allow(db, actor, permission, now)
    _visibility(db, actor, workflow, now)
    if perform and step.kind == "extraction":
        _allow(db, actor, "office.extraction.submit", now)
    if perform and step.kind == "execution":
        approved = _advance_receipt(db, actor, workflow, 4).references
        _approval_facts(db, actor, workflow, steps, approved["approval_id"], now, fresh=True)
    # Existing domain readers expire ORM attributes. Read versions before any
    # mutation and apply the complete invariant together: a lazy version load
    # must not autoflush state=completed before completed_at has been assigned.
    step_version, workflow_version = step.version, workflow.version
    next_number = workflow.next_step_no + 1
    with db.no_autoflush:
        step.state, step.input_digest, step.output_digest = "completed", input_digest, _digest(facts)
        step.version = step_version + 1
        step.completed_at = now
        workflow.version = workflow_version + 1
        workflow.next_step_no = next_number
        workflow.updated_at = now
        workflow.state = "closed" if next_number == 8 else "active"
        workflow.closed_at = now if next_number == 8 else None
    references = dict(facts)
    references["action"] = "perform" if perform else "observe"
    if step.kind in ("execution", "verification", "closure"):
        references["observed_task_digest"] = proof.task_state_digest
    receipt = _record(db, actor, workflow, "advance", key, request_digest, input_digest,
        step.output_digest, references, now, step_no=step.step_no)
    if perform and step.kind in ("execution", "verification", "closure"):
        from backend.app.application import commit_guard
        workflow_id, kind = workflow.id, step.kind
        def final(session, instant):
            check_command_authority(session, actor, permission="office.workflows.advance", epoch=epoch, now=instant)
            _allow(session, actor, permission, instant)
            current = session.scalar(select(OfficeWorkflow).where(*_scope(OfficeWorkflow, actor),
                OfficeWorkflow.id == workflow_id).execution_options(populate_existing=True))
            current_steps = list(session.scalars(select(OfficeWorkflowStep).where(
                *_scope(OfficeWorkflowStep, actor), OfficeWorkflowStep.workflow_id == workflow_id)
                .order_by(OfficeWorkflowStep.step_no).execution_options(populate_existing=True)))
            if kind == "execution":
                approved = _advance_receipt(session, actor, current, 4).references
                _approval_facts(session, actor, current, current_steps, approved["approval_id"],
                                instant, fresh=True, lock=False)
        commit_guard.enlist(db, action="office.workflow.perform:" + kind, check=final)
    return _checked_view(db, actor, workflow, now, command=receipt,
        permission="office.workflows.advance", epoch=epoch)


def error_status(code):
    if code in ("workflow_not_found", "workflow_task_not_found"):
        return 404
    if code in ("workflow_human_required", "workflow_responsible_required", "workflow_creator_required",
                "workflow_self_approval_forbidden", "workflow_task_responsibility_invalid"):
        return 403
    if code == "workflow_input_invalid":
        return 422
    if code == "workflow_key_invalid":
        return 400
    if code == "workflow_extractor_unavailable":
        return 503
    return 409


def _required_key(value: Annotated[str, Header(alias="Idempotency-Key")]):
    return require_idempotency_key(value)


def _api(db, function, actor, *args, envelope=WorkflowEnvelope, data_model=WorkflowReceipt, write=False, **kwargs):
    try:
        result = function(db, actor, *args, **kwargs)
        response = envelope(success=True, data=data_model.model_validate(result), code="OK", error=None,
            user_message="流程与来源校验状态已读取。" if not write else "人工流程记录已保存；正式业务结果仍需核对原系统。", hint=None)
        if write:
            db.commit()
        return response
    except intake.OfficeIntakeError:
        db.rollback()
        raise WorkflowError("workflow_source_unavailable") from None
    except extraction.ExtractionError as exc:
        db.rollback()
        code = "workflow_extractor_unavailable" if exc.code == "office_extractor_unavailable" else "workflow_source_unavailable"
        raise WorkflowError(code) from None
    except rules.OfficeRuleError:
        db.rollback()
        raise WorkflowError("workflow_source_unavailable") from None
    except Exception:
        db.rollback()
        raise


def read_receipt_history(db, actor, ident, *, after_version=0, limit=100):
    if type(after_version) is not int or after_version < 0 or type(limit) is not int or not 1 <= limit <= 100:
        _fail("workflow_input_invalid")
    now = _guard(db, actor, "office.workflows.read")
    workflow = _workflow(db, actor, ident)
    _visibility(db, actor, workflow, now)
    rows = list(db.scalars(select(OfficeWorkflowReceipt).where(*_scope(OfficeWorkflowReceipt, actor),
        OfficeWorkflowReceipt.workflow_id == workflow.id,
        OfficeWorkflowReceipt.workflow_version > after_version).order_by(OfficeWorkflowReceipt.workflow_version).limit(limit + 1)))
    page = rows[:limit]
    result = WorkflowReceiptPage(workflow_id=workflow.id, workflow_version=workflow.version,
        receipts=[_command_view(row) for row in page],
        next_after_version=page[-1].workflow_version if len(rows) > limit else None)
    return result.model_dump()


@router.get("/context", response_model=WorkflowContextEnvelope)
def context_api(task_id: Identifier, evidence_id: Identifier, rule_id: Identifier | None = None,
                origin_run_id: Identifier | None = None,
                actor: AuthenticatedSubject = Depends(require_authenticated_subject),
                db: Session = Depends(get_session)):
    return _api(db, read_context, actor, task_id=task_id, evidence_id=evidence_id,
        rule_id=rule_id, origin_run_id=origin_run_id, envelope=WorkflowContextEnvelope, data_model=WorkflowContext)


@router.post("", response_model=WorkflowEnvelope)
def create_api(body: WorkflowCreateInput, actor: AuthenticatedSubject = Depends(require_authenticated_subject),
               db: Session = Depends(get_session), key: str = Depends(_required_key)):
    return _api(db, create_workflow, actor, body, key, write=True)


@router.get("/{workflow_id}", response_model=WorkflowEnvelope)
def read_api(workflow_id: Identifier, actor: AuthenticatedSubject = Depends(require_authenticated_subject),
             db: Session = Depends(get_session)):
    return _api(db, read_workflow, actor, workflow_id)


@router.get("/{workflow_id}/receipts", response_model=WorkflowReceiptPageEnvelope)
def history_api(workflow_id: Identifier, after_version: Annotated[int, Query(ge=0)] = 0,
                limit: Annotated[int, Query(ge=1, le=100)] = 100,
                actor: AuthenticatedSubject = Depends(require_authenticated_subject),
                db: Session = Depends(get_session)):
    return _api(db, read_receipt_history, actor, workflow_id, after_version=after_version, limit=limit,
                envelope=WorkflowReceiptPageEnvelope, data_model=WorkflowReceiptPage)


@router.post("/{workflow_id}/advance", response_model=WorkflowEnvelope)
def advance_api(workflow_id: Identifier, body: WorkflowAdvanceInput,
                actor: AuthenticatedSubject = Depends(require_authenticated_subject),
                db: Session = Depends(get_session), key: str = Depends(_required_key)):
    return _api(db, advance_workflow, actor, workflow_id, body, key, write=True)


@router.post("/{workflow_id}/control", response_model=WorkflowEnvelope)
def control_api(workflow_id: Identifier, body: WorkflowControlInput,
                actor: AuthenticatedSubject = Depends(require_authenticated_subject),
                db: Session = Depends(get_session), key: str = Depends(_required_key)):
    return _api(db, control_workflow, actor, workflow_id, body, key, write=True)


@router.post("/{workflow_id}/request-approval", response_model=WorkflowApprovalEnvelope)
def approval_api(workflow_id: Identifier, body: WorkflowApprovalInput,
                 actor: AuthenticatedSubject = Depends(require_authenticated_subject),
                 db: Session = Depends(get_session), key: str = Depends(_required_key)):
    return _api(db, request_workflow_approval, actor, workflow_id, body, key, write=True,
                envelope=WorkflowApprovalEnvelope, data_model=WorkflowApprovalReceipt)
