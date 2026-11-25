"""Unconnected, metadata-only structural projection; NEVER an authorization gate.

See AGT-046-history-source-adapter-contract.md. No runtime, HTTP or archive caller
is installed. The supplied session is trusted, not a sandbox or a read permit.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.modules.agents.models import AgentExecution, AgentRun, AgentStep, CostLedger
from backend.app.modules.assistant.models import AssistantDispatch, AssistantWork, AssistantWorkAuthority
from backend.app.modules.tasks.models import Task


MAX_WORKS = 100
MAX_RELATED_ROWS = 10_000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,35}\Z")

# Deliberately explicit Core columns: no ORM entity or relationship loading.
_COLUMNS = {
    AssistantWork.__table__: ("task_id", "organization_id", "store_id", "owner_id", "origin_account_id"),
    Task.__table__: ("id", "organization_id", "store_id", "created_by", "state"),
    AssistantDispatch.__table__: ("work_id", "run_id", "state"),
    AgentRun.__table__: ("id", "organization_id", "store_id", "requester_id", "state"),
    AgentExecution.__table__: ("run_id", "organization_id", "store_id", "account_id", "phase",
                              "dispatch_state", "cancel_resolution", "effect_status"),
    AgentStep.__table__: ("id", "organization_id", "store_id", "run_id", "kind", "state"),
    CostLedger.__table__: ("id", "organization_id", "store_id", "run_id", "step_id", "requester_id", "status"),
    AssistantWorkAuthority.__table__: ("work_id", "run_id", "organization_id", "store_id", "owner_id",
                                       "account_id", "issued_at", "expires_at", "revoked_at"),
}
_STATES = {
    "task": frozenset(("suggested", "accepted", "assigned", "in_progress", "verified", "closed")),
    "dispatch": frozenset(("pending", "launching", "settled", "denied", "unknown")),
    "run": frozenset(("created", "running", "waiting_for_human", "waiting_for_external", "completed", "failed", "cancelled")),
    "phase": frozenset(("queued", "claimed", "unknown", "finished", "cancelled")),
    "execution_dispatch": frozenset(("not_dispatched", "dispatched", "unknown", "settled")),
    "cancel": frozenset(("not_requested", "requested", "cancelled", "too_late", "unknown")),
    "effect": frozenset(("none", "local_task_committed", "external_unknown")),
    "step": frozenset(("pending", "running", "succeeded", "failed", "unknown", "cancelled")),
    "kind": frozenset(("model", "tool", "checkpoint")),
    "cost": frozenset(("reserved", "settled", "unknown", "released", "reconciliation_required")),
}


class HistoryProjectionError(ValueError):
    """Stable non-enumerating code; no raw database error or partial output."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SourceScope:
    organization_id: str
    store_id: str
    owner_id: str
    account_id: str


def _identifier(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def _instant(value, code="inconsistent_source"):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise HistoryProjectionError(code)
    return value.astimezone(timezone.utc)


def _check(condition):
    if not condition:
        raise HistoryProjectionError("inconsistent_source")


def _state(row, field, vocabulary):
    value = row[field]
    _check(value in _STATES[vocabulary])
    return value


def _statement(table):
    return select(*(table.c[name] for name in _COLUMNS[table]))


def _rows(db, table, field, value, *, limit=1):
    result = db.execute(_statement(table).where(table.c[field] == value).limit(limit + 1)).mappings().all()
    _check(len(result) <= limit)
    return result


def _one(db, table, field, value):
    rows = _rows(db, table, field, value)
    return rows[0] if rows else None


def _scope(row, scope, *, owner=None, account=None):
    _check(row["organization_id"] == scope.organization_id and row["store_id"] == scope.store_id)
    if owner:
        _check(row[owner] == scope.owner_id)
    if account:
        _check(row[account] == scope.account_id)


def _authority(row, scope, run_id, observed_at):
    if row is None:
        return "absent"
    _scope(row, scope, owner="owner_id", account="account_id")
    _check(run_id is not None and row["run_id"] == run_id)
    issued = _instant(row["issued_at"])
    expires = _instant(row["expires_at"])
    revoked = None if row["revoked_at"] is None else _instant(row["revoked_at"])
    _check(expires > issued and (revoked is None or revoked >= issued))
    if issued > observed_at:
        return "not_yet_issued"
    if revoked is not None and revoked <= observed_at:
        return "revoked"
    return "expired" if expires <= observed_at else "unexpired"


def _counts(rows, field, vocabulary):
    counts = {}
    for row in rows:
        value = _state(row, field, vocabulary)
        counts[value] = counts.get(value, 0) + 1
    return {"row_count": len(rows), "states": dict(sorted(counts.items()))}


def _work(db, scope, work_id, observed_at):
    work = _one(db, AssistantWork.__table__, "task_id", work_id)
    if work is None:
        raise HistoryProjectionError("source_unavailable")
    _scope(work, scope, owner="owner_id", account="origin_account_id")
    task = _one(db, Task.__table__, "id", work_id)
    _check(task is not None)
    _scope(task, scope, owner="created_by")
    task_state = _state(task, "state", "task")
    dispatch = _one(db, AssistantDispatch.__table__, "work_id", work_id)
    authority = _one(db, AssistantWorkAuthority.__table__, "work_id", work_id)
    run_id = None
    dispatch_state = None
    run_state = None
    execution_fact = {"presence": "absent"}
    steps, costs = [], []
    run_presence = "not_planned"
    if dispatch is not None:
        run_id = dispatch["run_id"]
        _check(_identifier(run_id))
        dispatch_state = _state(dispatch, "state", "dispatch")
        run = _one(db, AgentRun.__table__, "id", run_id)
        execution = _one(db, AgentExecution.__table__, "run_id", run_id)
        steps = _rows(db, AgentStep.__table__, "run_id", run_id, limit=MAX_RELATED_ROWS)
        costs = _rows(db, CostLedger.__table__, "run_id", run_id, limit=MAX_RELATED_ROWS)
        if run is None:
            _check(dispatch_state != "settled" and execution is None and not steps and not costs)
            run_presence = "not_created"
        else:
            _scope(run, scope, owner="requester_id")
            run_state = _state(run, "state", "run")
            run_presence = "present"
            if execution is not None:
                _scope(execution, scope, account="account_id")
                execution_fact = {
                    "presence": "present",
                    "phase": _state(execution, "phase", "phase"),
                    "dispatch_state": _state(execution, "dispatch_state", "execution_dispatch"),
                    "cancel_resolution": _state(execution, "cancel_resolution", "cancel"),
                    "effect_status": _state(execution, "effect_status", "effect"),
                }
            step_by_id = {}
            for step in steps:
                _scope(step, scope)
                _check(_identifier(step["id"]) and step["id"] not in step_by_id)
                _state(step, "kind", "kind")
                step_by_id[step["id"]] = step
            for cost in costs:
                _scope(cost, scope, owner="requester_id")
                step = step_by_id.get(cost["step_id"])
                _check(step is not None and step["kind"] == "model")
    return {
        "work_id": work_id,
        "task_state": task_state,
        "dispatch": {"presence": "present" if dispatch is not None else "absent", "state": dispatch_state},
        "run": {"presence": run_presence, "id": run_id, "state": run_state},
        "execution": execution_fact,
        "steps": _counts(steps, "state", "step"),
        "costs": _counts(costs, "status", "cost"),
        "delegation_record_fact": _authority(authority, scope, run_id, observed_at),
    }


def project_source_metadata(db: Session, *, scope: SourceScope, work_ids, observed_at: datetime):
    """Project root-work metadata only. Structural success grants NO access.

    Requires a fresh clean caller-owned Session. Statements are explicit SELECTs;
    this function never opens a database or flushes/commits/rolls back a session.
    Its observation instant is not proof of an atomic or authorized DB snapshot.
    """
    if not isinstance(db, Session) or type(scope) is not SourceScope:
        raise HistoryProjectionError("invalid_request")
    if not all(_identifier(value) for value in (scope.organization_id, scope.store_id, scope.owner_id, scope.account_id)):
        raise HistoryProjectionError("invalid_request")
    if type(work_ids) not in (list, tuple) or not 1 <= len(work_ids) <= MAX_WORKS:
        raise HistoryProjectionError("invalid_request")
    if not all(_identifier(value) for value in work_ids) or len(set(work_ids)) != len(work_ids):
        raise HistoryProjectionError("invalid_request")
    observed_at = _instant(observed_at, "invalid_request")
    if db.new or db.dirty or db.deleted or db.in_transaction():
        raise HistoryProjectionError("unclean_session")
    try:
        with db.no_autoflush:
            # Do not interpret an absent migration/table/column as absent records.
            for table in _COLUMNS:
                db.execute(_statement(table).limit(0)).all()
            works = [_work(db, scope, work_id, observed_at) for work_id in work_ids]
    except HistoryProjectionError:
        raise
    except (SQLAlchemyError, ValueError, TypeError, OverflowError):
        raise HistoryProjectionError("unsupported_source") from None
    return {
        "contract": "agt046-history-source-projection/v1",
        "observed_at": observed_at.isoformat(),
        "structural_only": True,
        "authorization_checked": False,
        "source_binding_verified": False,
        "can_execute": False,
        "content_available": False,
        "works": works,
    }
