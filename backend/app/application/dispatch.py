"""Finite adapters to existing services. The caller owns commit and rollback."""
from dataclasses import replace
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from backend.app.application import commit_guard
from backend.app.application.contracts import CommandIntent, CommandReceipt, CommandResult, ExecutionContext
from backend.app.modules.identity.command_guard import lock_command_authority
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.tasks import service as tasks
from backend.app.modules.tasks.models import TaskIdempotency
from backend.app.modules.tasks.repository import list_tasks_visible
from backend.app.modules.tasks.schemas import TaskCreate, TaskManagedCreate, TaskSuggestion, TaskReason, TaskDecision, TaskAssignment
from backend.app.modules.sales import service as sales
from backend.app.modules.sales.models import SalesHandoff, SalesReceipt
from backend.app.modules.sales.schemas import (DraftCreate, HandoffCreate, ReceiptCreate,
    LineQuoteRequestV2, LineBindRequestV2)

_SEAL = object()
_TASK_MODELS = {
    "tasks.create": TaskCreate, "tasks.manage": TaskManagedCreate, "tasks.suggest_review": TaskSuggestion,
    "tasks.accept": TaskReason, "tasks.assign": TaskAssignment, "tasks.start": TaskReason,
    "tasks.verify": TaskReason, "tasks.close": TaskReason, "tasks.decide": TaskDecision,
}
_SALES_MODELS = {
    "sales.create_draft": DraftCreate, "sales.create_handoff": HandoffCreate, "sales.record_receipt": ReceiptCreate,
}


_SALES_V2_MODELS = {"sales.quote_line_v2": LineQuoteRequestV2, "sales.bind_line_v2": LineBindRequestV2}


def _context(session, actor, origin):
    if (not actor.account_id or not actor.session_id or type(actor.account_version) is not int
            or actor.account_version <= 0):
        raise HTTPException(403, "human_session_required")
    with session.no_autoflush:
        epoch = lock_command_authority(session, actor)
        authorize_scope(session, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, now=commit_guard._db_clock(session))
        kind = session.scalar(select(Subject.subject_type).where(Subject.id == actor.subject_id,
            Subject.organization_id == actor.organization_id))
        if kind != "person":
            raise HTTPException(403, "human_session_required")
    return ExecutionContext(actor, origin, epoch, session, session.get_transaction(), _SEAL)


def execute_http(session, actor, intent: CommandIntent) -> CommandResult:
    return _execute_person(session, _entry_context(session, actor, intent, "employee_http"), intent)


def execute_host(session, actor, intent: CommandIntent) -> CommandResult:
    if intent.action in _SALES_V2_MODELS:
        raise HTTPException(403, "command_employee_http_required")
    return _execute_person(session, _entry_context(session, actor, intent, "person_host"), intent)


def _entry_context(session, actor, intent, origin):
    try:
        return _context(session, actor, origin)
    except HTTPException as error:
        # A real sx1 dependency can succeed before waiting on this command's
        # fence. Preserve the sales domain's existing post-wait denial mapping.
        if ((intent.action in _SALES_MODELS or intent.action in _SALES_V2_MODELS) and actor.account_id and actor.session_id
                and type(actor.account_version) is int and actor.account_version > 0):
            raise sales.SalesError("sales_permission_denied") from error
        raise


def _payload(intent, models):
    model = models.get(intent.action)
    if model is None or intent.version != "1":
        raise HTTPException(400, "command_action_unknown")
    try:
        return model.model_validate(intent.payload)
    except ValidationError as error:
        raise HTTPException(422, "command_input_invalid") from error


def _task_receipt(session, actor, key, task):
    row = session.scalar(select(TaskIdempotency).where(
        TaskIdempotency.organization_id == actor.organization_id,
        TaskIdempotency.store_id == actor.store_id, TaskIdempotency.idempotency_key == key))
    return CommandReceipt("tasks", task.id, "task_idempotency:organization_store_key", key,
        row.id if row else None, row.payload_digest if row else None)


def _execute_person(session, context, intent):
    if (context.seal is not _SEAL or context.session is not session
            or context.root is not session.get_transaction()
            or context.origin not in ("employee_http", "person_host")):
        raise HTTPException(403, "command_context_invalid")
    actor, key = context.actor, intent.idempotency_key
    if intent.action in _SALES_V2_MODELS:
        if context.origin != "employee_http":
            raise HTTPException(403, "command_employee_http_required")
        body = _payload(intent, _SALES_V2_MODELS)
        if intent.action == "sales.quote_line_v2":
            data = sales.quote_line_v2(session, actor, intent.target_id, body, idempotency_key=key)
            row = sales._v2_quote(session, actor, intent.target_id, body.quote_key, body.line_key)
            receipt = CommandReceipt("sales_v2", row.id, "pricing_quotes_v2:organization_store_key", key,
                                     row.id, row.request_digest)
        else:
            data = sales.bind_line_v2(session, actor, intent.target_id, body, idempotency_key=key)
            row = sales._v2_line_receipt(session, actor, intent.target_id, body.line_key)
            receipt = CommandReceipt("sales_v2", row.line_id, "sales_line_commands_v2:draft_line_key", key,
                                     row.id, row.request_digest)
        return CommandResult(data, receipt, context)
    if intent.action in _TASK_MODELS:
        body = _payload(intent, _TASK_MODELS)
        args = body.model_dump()
        if intent.action == "tasks.create":
            data = tasks.create_task(session, actor, **args, idempotency_key=key)
        elif intent.action == "tasks.manage":
            data = tasks.create_managed_task(session, actor, **args, idempotency_key=key)
        elif intent.action == "tasks.suggest_review":
            # Preserve the existing person HTTP /suggestions rejection; only
            # the fixed Agent adapter below can create an Agent suggestion.
            data = tasks.create_task(session, actor, **args, idempotency_key=key, agent_only=True)
        else:
            action = {"tasks.accept": tasks.accept_task, "tasks.assign": tasks.assign_task,
                "tasks.start": tasks.start_task, "tasks.verify": tasks.verify_task,
                "tasks.close": tasks.close_task, "tasks.decide": tasks.decide_task}[intent.action]
            data = action(session, actor, task_id=intent.target_id, **args, idempotency_key=key)
        receipt = _task_receipt(session, actor, key, data)
    else:
        body = _payload(intent, _SALES_MODELS)
        if intent.action == "sales.create_draft":
            data = sales.create_draft_with_key(session, actor, body, idempotency_key=key)
            receipt = CommandReceipt("sales", data.id, "sales_drafts:organization_store_key", key,
                                     data.id, source_version=data.version)
        elif intent.action == "sales.create_handoff":
            data = sales.create_handoff(session, actor, intent.target_id, body, idempotency_key=key)
            receipt = CommandReceipt("sales", data.id, "sales_handoffs:organization_store_key", key, data.id)
        else:
            data = sales.record_receipt(session, actor, intent.target_id, body, idempotency_key=key)
            row = session.scalar(select(SalesReceipt).where(SalesReceipt.organization_id == actor.organization_id,
                SalesReceipt.store_id == actor.store_id, SalesReceipt.receipt_key == key))
            receipt = CommandReceipt("sales", data.id, "sales_receipts:organization_store_key", key,
                                     row.id, row.digest)
    return CommandResult(data, receipt, context)


def execute_agent_suggestion(session, *, requester, agent, payload, idempotency_key):
    """Fixed registry adapter; callers retain approval/lease/skill obligations."""
    context = replace(_context(session, requester, "agent_tool"), agent=agent)
    with session.no_autoflush:
        if ((agent.organization_id, agent.store_id) != (requester.organization_id, requester.store_id)
                or agent.account_id is not None or agent.session_id is not None):
            raise HTTPException(403, "tool_agent_identity_invalid")
        kind = session.scalar(select(Subject.subject_type).where(Subject.id == agent.subject_id,
            Subject.organization_id == agent.organization_id))
        if kind != "agent":
            raise HTTPException(403, "tool_agent_identity_invalid")
        for principal, permission in ((requester, "tasks.suggest"), (agent, "agents.tasks.suggest_review")):
            authorize_scope(session, principal, organization_id=requester.organization_id,
                store_id=requester.store_id, permission=permission, now=commit_guard._db_clock(session))
    body = TaskSuggestion.model_validate(payload)
    data = tasks.create_task(session, agent, **body.model_dump(), agent_only=True,
                             idempotency_key=idempotency_key)
    return CommandResult(data, _task_receipt(session, agent, idempotency_key, data), context)


def read_agent_tasks_mine(session, *, requester, limit):
    """Fixed own-task projection shared by the synchronous and Worker gateways."""
    with session.no_autoflush:
        from backend.app.modules.identity.command_guard import refresh_authority
        refresh_authority(session)
        authorize_scope(session, requester, organization_id=requester.organization_id,
            store_id=requester.store_id, permission="tasks.read_self", now=commit_guard._db_clock(session))
        return list_tasks_visible(session, organization_id=requester.organization_id,
            store_id=requester.store_id, subject_id=requester.subject_id,
            read_self=True, read_responsible=False, limit=limit)
