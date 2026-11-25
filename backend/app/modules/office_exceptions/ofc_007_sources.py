"""Fixed, read-only exception sources. No dispatch, reconciliation or domain write.

The caller holds its employee/Store fence and owns the transaction. Commands lock
source parents in domain order; display reads do not add adapter row locks.
Existing upstream read-service guards remain authoritative. We retain only
normalized references in the public projection. A digest is not permission.
"""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json

from sqlalchemy import select

from backend.app.modules.audit.models import AuditEvent
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.tasks import service as tasks
from backend.app.modules.tasks.models import Task, TaskEvent
from backend.app.modules.receiving import service as receiving
from backend.app.modules.receiving.models import (
    ReceivingBatch, ReceivingLine, ReceivingDiscrepancy, ReceivingDecision,
)
from backend.app.modules.erp_commands import erp_002_service as erp
from backend.app.modules.erp_commands import erp_003_service as sandbox
from backend.app.modules.erp_commands.erp_002_models import ERPCommandIntent, ERPCommandOutcome
from backend.app.modules.erp_commands.erp_003_models import ERPSandboxBatch, ERPSandboxLine, ERPSandboxObservation
from backend.app.modules.erp_commands.erp_003_wire import canonical_digest
from backend.app.modules.sales import service as sales
from backend.app.modules.sales.models import SalesDraft, SalesHandoff, SalesReceipt
from backend.app.modules.office_workflows import ofc_006_service as workflows
from backend.app.modules.office_workflows.ofc_006_models import OfficeWorkflow, OfficeWorkflowReceipt
from backend.app.modules.office_exceptions.ofc_007_schemas import ExceptionSource, SourceLocator

MAX_HISTORY = 1000


class ExceptionSourceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code="exception_source_unavailable"):
    raise ExceptionSourceError(code)


def _stamp(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(timezone.utc).isoformat()
    raise TypeError("unsupported source value")


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                             default=_stamp, allow_nan=False).encode()).hexdigest()


def scope(model, actor):
    return model.organization_id == actor.organization_id, model.store_id == actor.store_id


def _row(db, model, actor, ident, *, lock=True):
    query = select(model).where(*scope(model, actor), model.id == ident).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        _fail("exception_source_not_found")
    return row


def _rows(db, query):
    rows = list(db.scalars(query.execution_options(populate_existing=True).limit(MAX_HISTORY + 1)))
    if len(rows) > MAX_HISTORY:
        _fail()
    return rows


def _values(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _audit(db, actor, action, target_type, target_id, *, actor_id=None, key=None, details=None):
    query = select(AuditEvent).where(*scope(AuditEvent, actor), AuditEvent.action == action,
        AuditEvent.target_type == target_type, AuditEvent.target_id == target_id)
    if actor_id is not None:
        query = query.where(AuditEvent.actor_id == actor_id)
    if key is not None:
        query = query.where(AuditEvent.idempotency_key == key)
    found = [row for row in _rows(db, query.order_by(AuditEvent.id))
             if all(row.details.get(k) == v for k, v in (details or {}).items())]
    if len(found) != 1:
        _fail()
    return found[0]


def _ref(kind, row, *, version=None, proof=None):
    return dict(kind=kind, id=row.id, version=version, digest=proof or digest(_values(row)))


def _result(locator, version, state, verification, facts, *, anomaly, proof=None,
            resolution_kind="verified_resolution", references=None, lines=None):
    result = dict(**locator, source_digest=digest(facts), source_version=version,
        source_state=state, source_verification=verification, anomaly_present=bool(anomaly),
        can_resolve=proof is not None, resolution_kind=resolution_kind if proof is not None else None,
        resolution_digest=digest(proof) if proof is not None else None,
        references=references or [], lines=lines or [], effect="case_tracking_only", mode="human_supervised")
    if len(result["references"]) > 100 or len(result["lines"]) > 100:
        _fail()
    return result


def _receiving(db, actor, locator, now, *, lock=True):
    # The discrepancy is immutable under the domain API; lock the batch before
    # decisions/lines, matching decide_discrepancy's parent CAS order.
    pointer = _row(db, ReceivingDiscrepancy, actor, locator["source_id"], lock=False)
    batch = _row(db, ReceivingBatch, actor, pointer.batch_id, lock=lock)
    current = receiving.get_batch(db, actor, batch.id, now=now)
    difference = _row(db, ReceivingDiscrepancy, actor, pointer.id, lock=lock)
    line = _row(db, ReceivingLine, actor, difference.line_id, lock=lock)
    if difference.batch_id != batch.id or line.batch_id != batch.id:
        _fail()
    kind = "identity_ambiguous" if difference.kind == "identity_unknown" else difference.kind
    if kind != locator["kind"]:
        _fail("exception_source_kind_mismatch")
    decisions = _rows(db, select(ReceivingDecision).where(*scope(ReceivingDecision, actor),
        ReceivingDecision.batch_id == batch.id, ReceivingDecision.discrepancy_id == difference.id)
        .order_by(ReceivingDecision.batch_version, ReceivingDecision.id))
    last = decisions[-1] if decisions else None
    proof, refs = None, [_ref("receiving_batch", batch, version=batch.version), _ref("receiving_line", line)]
    if last is not None and last.decision == "accept_observation":
        if last.reviewer_id == batch.created_by or last.batch_version > batch.version:
            _fail()
        audit = _audit(db, actor, "receiving.discrepancy_reviewed", "receiving_batch", batch.id,
            actor_id=last.reviewer_id,
            key=f"receiving:receiving.discrepancy_reviewed:{batch.id}:{last.batch_version}",
            details={"discrepancy_id": difference.id, "decision": last.decision,
                     "evidence_sha256": last.evidence_sha256, "responsible_id": last.responsible_id})
        proof = {"decision": _values(last), "audit": _values(audit)}
        refs += [_ref("receiving_decision", last, version=last.batch_version), _ref("audit", audit)]
    detail = difference.detail
    projected = dict(id=line.id, state=last.decision if last else "unreviewed",
        evidence_digest=line.evidence_sha256, source_verification="employee_observation",
        observed_quantity=line.observed_quantity, expected_quantity=detail.get("expected_quantity"),
        cumulative_observed_quantity=detail.get("cumulative_observed_quantity"), unit=line.unit)
    return _result(locator, batch.version, batch.status, "employee_observation",
        {"batch": current, "difference": _values(difference), "line": _values(line),
         "decisions": [_values(row) for row in decisions], "proof": proof},
        anomaly=last is None or last.decision != "accept_observation", proof=proof, references=refs, lines=[projected])


def _task(db, actor, locator, now, *, lock=True):
    tasks.get_task_for_scope(db, actor, locator["source_id"], now=now)
    task = _row(db, Task, actor, locator["source_id"], lock=lock)
    events = _rows(db, select(TaskEvent).where(*scope(TaskEvent, actor), TaskEvent.task_id == task.id)
                   .order_by(TaskEvent.occurred_at, TaskEvent.id))
    due = task.due_at
    if due is not None and due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    anomaly = task.state in ("assigned", "in_progress") and due is not None and due <= now
    refs, proof = [_ref("task", task)], None
    if task.state in ("verified", "closed"):
        operation = "verify" if task.state == "verified" else "close"
        by = task.verified_by if operation == "verify" else task.closed_by
        at = task.verified_at if operation == "verify" else task.closed_at
        reason = task.verification_reason if operation == "verify" else task.close_reason
        matches = [row for row in events if row.to_state == task.state and row.actor_id == by
                   and row.responsible_id == task.assignee_id and row.reason == reason
                   and row.decision_state == task.decision_state] if at is not None and by is not None else []
        if len(matches) != 1 or (operation == "verify" and by == task.assignee_id):
            _fail()
        audit = _audit(db, actor, "task." + operation, "task", task.id, actor_id=by,
                       details={"to_state": task.state, "responsible_id": task.assignee_id, "reason": reason})
        # The Task service obtains its field timestamp just before entering
        # _transition, then uses one later timestamp for event and audit. Those
        # two must agree; the earlier field is not an exact event identifier.
        event_time = datetime.fromisoformat(_stamp(matches[0].occurred_at))
        if (_stamp(audit.occurred_at) != _stamp(matches[0].occurred_at)
                or not datetime.fromisoformat(_stamp(at)) <= event_time <= now):
            _fail()
        proof = {"task": _values(task), "event": _values(matches[0]), "audit": _values(audit)}
        refs += [_ref("task_event", matches[0]), _ref("audit", audit)]
    return _result(locator, None, task.state, "task_record",
        {"task": _values(task), "events": [_values(row) for row in events], "proof": proof},
        anomaly=anomaly, proof=proof, references=refs)


def _workflow(db, actor, locator, now, *, lock=True):
    current = workflows.read_workflow(db, actor, locator["source_id"])
    row = _row(db, OfficeWorkflow, actor, locator["source_id"], lock=lock)
    commands = _rows(db, select(OfficeWorkflowReceipt).where(*scope(OfficeWorkflowReceipt, actor),
        OfficeWorkflowReceipt.workflow_id == row.id).order_by(OfficeWorkflowReceipt.workflow_version))
    proof = None
    if current["state"] == "closed" and current["blocking_code"] is None:
        advances = [r for r in commands if r.operation == "advance"]
        if {r.step_no for r in advances} != set(range(1, 8)) or len(advances) != 7:
            _fail()
        proof = {"closed_workflow": _values(row), "advances": [_values(r) for r in advances]}
    return _result(locator, row.version, row.state, "workflow_tracking",
        {"workflow": current, "commands": [_values(r) for r in commands]},
        anomaly=row.state in ("paused", "failed", "needs_human") or current["blocking_code"] is not None,
        proof=proof, references=[_ref("workflow", row, version=row.version)])


def _sandbox(db, actor, locator, now, *, lock=True):
    batch = _row(db, ERPSandboxBatch, actor, locator["source_id"], lock=lock)
    current = sandbox.read_execution(db, actor, batch.id)
    line_query = select(ERPSandboxLine).where(*scope(ERPSandboxLine, actor),
        ERPSandboxLine.batch_id == batch.id).order_by(ERPSandboxLine.line_no)
    rows = _rows(db, line_query.with_for_update() if lock else line_query)
    observations = _rows(db, select(ERPSandboxObservation).where(*scope(ERPSandboxObservation, actor),
        ERPSandboxObservation.batch_id == batch.id).order_by(ERPSandboxObservation.observed_at, ERPSandboxObservation.id))
    known = bool(rows) and all(row.state in ("sandbox_verified", "failed") for row in rows)
    proof, evidence = None, []
    if known:
        for row in rows:
            matched = None
            for observed in observations:
                content = observed.normalized_receipt
                if (observed.kind != "readback_receipt" or not isinstance(content, dict)
                        or canonical_digest(content) != observed.evidence_digest
                        or content.get("batch_id") != batch.id or content.get("input_digest") != batch.input_digest):
                    continue
                for line in content.get("lines", []):
                    if (line.get("line_id") == row.id and line.get("input_digest") == row.input_digest
                            and canonical_digest(line) == row.receipt_digest
                            and line.get("status") == ("accepted" if row.state == "sandbox_verified" else "rejected")
                            and line.get("external_ref") == row.external_ref and line.get("error_code") == row.error_code):
                        matched = observed
                        break
                if matched is not None:
                    break
            if matched is None:
                _fail()
            audit = _audit(db, actor, "erp.sandbox.readback", "erp_sandbox_batch", batch.id,
                details={"evidence_digest": matched.evidence_digest, "mode": "synthetic"},
                key="erp-sandbox:" + canonical_digest({"batch": batch.id, "operation": "readback",
                                                       "key": matched.observation_key}))
            evidence.append({"line": _values(row), "observation": _values(matched), "audit": _values(audit)})
        proof = {"mode": "synthetic", "batch": _values(batch), "evidence": evidence}
    states = {row.state for row in rows}
    resolution = "reconciled_partial" if len(states) > 1 else (
        "verified_rejection" if states == {"failed"} else "verified_resolution")
    anomaly = batch.state == "partial" if locator["kind"] == "partial" else (
        batch.state == "dispatched" or bool(states.intersection({"unknown"})))
    return _result(locator, batch.version, batch.state, "synthetic_erp",
        {"receipt": current, "observations": [_values(r) for r in observations], "proof": proof},
        anomaly=anomaly, proof=proof, resolution_kind=resolution,
        references=[_ref("sandbox_batch", batch, version=batch.version)],
        lines=[dict(id=row.id, state=row.state, version=row.version, receipt_digest=row.receipt_digest,
                    error_code=row.error_code, source_verification="synthetic_erp") for row in rows])


def _erp(db, actor, locator, now, *, lock=True):
    intent = _row(db, ERPCommandIntent, actor, locator["source_id"], lock=lock)
    current = erp.read_command(db, actor, intent.request_id)
    outcomes = _rows(db, select(ERPCommandOutcome).where(*scope(ERPCommandOutcome, actor),
        ERPCommandOutcome.intent_id == intent.id).order_by(ERPCommandOutcome.id))
    if len(outcomes) > 1:
        _fail()
    outcome = outcomes[0] if outcomes else None
    proof, refs = None, [_ref("erp_intent", intent)]
    if outcome is not None and outcome.outcome == "manual_rejected":
        audit = _audit(db, actor, "erp.command.manual_manual_rejected", "erp_command_request", intent.request_id,
            actor_id=outcome.verified_by, details={"outcome_id": outcome.id,
            "evidence_sha256": outcome.evidence_sha256, "no_erp_network_dispatch": True})
        proof = {"manual_rejection": _values(outcome), "audit": _values(audit)}
        refs += [_ref("erp_outcome", outcome), _ref("audit", audit)]
    return _result(locator, None, current["state"], "manual_erp_evidence",
        {"intent": _values(intent), "command": current, "outcomes": [_values(r) for r in outcomes], "proof": proof},
        anomaly=outcome is not None and outcome.outcome == "unknown", proof=proof,
        resolution_kind="verified_rejection", references=refs)


def _sales(db, actor, locator, now, *, lock=True):
    pointer = _row(db, SalesHandoff, actor, locator["source_id"], lock=False)
    draft = _row(db, SalesDraft, actor, pointer.draft_id, lock=lock)
    handoff = _row(db, SalesHandoff, actor, pointer.id, lock=lock)
    current = sales.get_draft(db, actor, draft.id, now=now).model_dump()
    receipts = _rows(db, select(SalesReceipt).where(*scope(SalesReceipt, actor),
        SalesReceipt.handoff_id == handoff.id).order_by(SalesReceipt.created_at, SalesReceipt.id))
    proof, refs = None, [_ref("sales_draft", draft, version=draft.version), _ref("sales_handoff", handoff)]
    if handoff.handoff_state == "confirmed":
        matches = [r for r in receipts if r.state_after == "confirmed" and r.paid and not r.unknown
                   and r.external_ref == handoff.external_ref]
        if not matches:
            _fail()
        receipt = matches[-1]
        if receipt.digest != digest(dict(handoff_id=handoff.id, external_ref=receipt.external_ref,
                                         paid=receipt.paid, unknown=receipt.unknown)):
            _fail()
        audit = _audit(db, actor, "sales.receipt_confirmed", "sales_draft", draft.id,
                       key=f"sales:{draft.id}:receipt:{receipt.digest}",
                       details={"handoff_id": handoff.id, "paid": True, "receipt_key": receipt.receipt_key})
        proof = {"handoff": _values(handoff), "receipt": _values(receipt), "audit": _values(audit)}
        refs += [_ref("sales_receipt", receipt), _ref("audit", audit)]
    return _result(locator, None, handoff.handoff_state, "sales_record",
        {"draft": current, "handoff": _values(handoff), "receipts": [_values(r) for r in receipts], "proof": proof},
        anomaly=handoff.handoff_state == "receipt_unknown", proof=proof, references=refs)


_SOURCES = {"receiving_discrepancy": _receiving, "task": _task, "workflow": _workflow,
            "erp_sandbox": _sandbox, "erp_intent": _erp, "sales_handoff": _sales}


def read_source(db, actor, locator, now, *, lock=True):
    """Caller must first strictly validate SourceLocator and authenticate actor."""
    if not isinstance(locator, dict) or set(locator) != {"source_type", "source_id", "kind"}:
        _fail("exception_source_invalid")
    locator = SourceLocator.model_validate(locator).model_dump()
    function = _SOURCES.get(locator["source_type"])
    if function is None:
        _fail("exception_source_invalid")
    permission = {"receiving_discrepancy": "receiving.read", "erp_sandbox": "erp.command.read",
                  "erp_intent": "erp.command.read", "sales_handoff": "sales.read",
                  "workflow": "office.workflows.read"}.get(locator["source_type"])
    if permission is not None:
        # Deny before even probing a source pointer. The real domain reader
        # below still enforces all dynamic source permissions and visibility.
        authorize_scope(db, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission=permission, now=now)
    return ExceptionSource.model_validate(function(db, actor, locator, now, lock=lock)).model_dump()
