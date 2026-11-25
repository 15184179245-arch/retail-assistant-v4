"""Internal exact-work principal. Never a login, bearer, or human approval."""
from dataclasses import dataclass
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.modules.assistant.models import AssistantWorkAuthority, AssistantTurnAuthority, AssistantTurnDispatch

CONTEXT_KEY = 'assistant_work_authority'
DEFAULT_CAPABILITIES = frozenset({'agents.run', 'tasks.manage', 'tasks.read_self',
    'catalog.read', 'knowledge.read', 'memory.read', 'memory.write',
    'guanjia.inventory.read', 'guanjia.pricing.read', 'guanjia.company.read'})
from backend.app.modules.workspace.service import _KIND_PERMISSION_MAP
_WORKSPACE_PERMISSIONS = frozenset(permission for actions in _KIND_PERMISSION_MAP.values()
    for operation in ('read', 'write') for permission in actions[operation])
CAPABILITIES=DEFAULT_CAPABILITIES|frozenset({'guanjia.sales.read','guanjia.catalog.read'})|_WORKSPACE_PERMISSIONS


@dataclass(frozen=True)
class WorkPrincipal:
    subject_id: str
    organization_id: str
    store_id: str
    account_id: str
    account_version: int
    session_id: str  # Provenance only; never passed to login validation.
    auth_expires_at: datetime
    work_id: str
    run_id: str
    work_epoch: int
    binding_sha256: str
    kind: str = 'work'


def _deny():
    raise HTTPException(403, 'assistant_work_principal_denied')


def _from_row(row):
    from backend.app.modules.assistant.authority import _utc, row_kind, _seal
    if _seal(row) != row.binding_sha256: _deny()
    return WorkPrincipal(row.owner_id, row.organization_id, row.store_id,
        row.account_id, row.account_version, row.origin_session_id, _utc(row.expires_at),
        row.work_id, row.run_id, row.work_epoch, row.binding_sha256, row_kind(row))


def frozen(principal):
    if type(principal) is not WorkPrincipal: _deny()
    if principal.kind not in ('work','turn'): _deny()
    value = {'version': 1 if principal.kind == 'work' else 2, 'work_id': principal.work_id, 'run_id': principal.run_id,
             'binding_sha256': principal.binding_sha256}
    if principal.kind == 'turn':value['kind'] = 'turn'
    return value


def check(session, principal, *, permission='agents.run', now=None, lock=False):
    from backend.app.application import commit_guard
    from backend.app.modules.assistant.authority import _validate, lock_work_authority, authority_model
    if type(principal) is not WorkPrincipal or permission not in CAPABILITIES: _deny()
    args = dict(work_id=principal.work_id, run_id=principal.run_id,
                binding_sha256=principal.binding_sha256, permission=permission, kind=principal.kind)
    if lock:
        snapshot = lock_work_authority(session, **args)
    else:
        # Read-only capability projections may run inside final-pin callbacks.
        # The enlisted lock-path recheck supplies the actual DB commit clock.
        # Never relock here: the authority lock was taken at the original
        # execution entry (lock_work_authority), and a FOR UPDATE inside the
        # commit guard's read-only validation is command_check_write_forbidden.
        snapshot = _validate(session, **args, now=now or datetime.now(timezone.utc), lock=False)
    row = session.get(authority_model(principal.kind), snapshot.work_id, populate_existing=True)
    if row is None or principal != _from_row(row): _deny()
    # Commit must preserve both grant validity and this exact principal, not
    # merely the fact that some work grant remains in the same scope.
    if lock:
        def recheck(db, instant):
            check(db, principal, permission=permission, now=instant)
        commit_guard.enlist(session, action='assistant.work_principal', check=recheck)
    return principal


def for_work(session, work_id, *, kind='work'):
    from backend.app.modules.assistant.authority import authority_model
    row = session.get(authority_model(kind), work_id, populate_existing=True)
    if row is None: return None
    return check(session, _from_row(row), lock=True)


def projection_permissions(session, principal, *, now=None):
    """One fresh tool-directory projection, never an execution authorization.

    Validate the exact work once, then resolve its optional permissions using
    the same grant precedence/expiry policy as authorize_scope. Nothing is
    retained on the session or reused across model/tool/commit boundaries.
    """
    from backend.app.modules.assistant.authority import authority_model
    from backend.app.modules.identity.repository import grants_for, load_scope
    from backend.app.modules.identity.service import permission_allowed
    instant = now or datetime.now(timezone.utc)
    with session.no_autoflush:
        check(session, principal, now=instant)
        row = session.get(authority_model(principal.kind), principal.work_id, populate_existing=True)
        if row is None or principal != _from_row(row): _deny()
        _, _, _, roles = load_scope(session, principal.organization_id,
                                     principal.store_id, principal.subject_id)
        allowed = frozenset(permission for permission in CAPABILITIES.intersection(row.permissions)
            if permission_allowed(grants_for(session, principal.organization_id,
                principal.store_id, principal.subject_id, roles, permission),
                principal.subject_id, now=instant))
        # Detect a lost binding, stop, security epoch or required permission
        # during projection. Optional permissions remain a point-in-time view;
        # executing a tool always performs its own current authorization.
        check(session, principal, now=now or datetime.now(timezone.utc))
        return allowed


def from_execution(session, execution, run):
    """Check immutable origin binding only; caller then checks current authority.

    Receipt/recovery readers may inspect terminal work without reauthorizing
    execution. Missing metadata cannot silently turn a delegated Run into a
    session-authorized Run, and legacy Runs cannot adopt a newly found grant.
    """
    from backend.app.modules.assistant.authority import _utc
    context = execution.frozen_context
    if type(context) is not dict: _deny()
    rows = [session.scalar(select(model).where(model.run_id == run.id).execution_options(populate_existing=True))
            for model in (AssistantWorkAuthority, AssistantTurnAuthority)]
    present = [row for row in rows if row is not None]
    if len(present) > 1: _deny()
    row = present[0] if present else None
    marker = context.get(CONTEXT_KEY)
    turn_intent=session.scalar(select(AssistantTurnDispatch).where(AssistantTurnDispatch.run_id==run.id)
        .execution_options(populate_existing=True))
    # A durable turn intent can never become a legacy session execution, even
    # if both its grant and frozen marker were lost/corrupted.
    if turn_intent is not None and (type(row) is not AssistantTurnAuthority or row.work_id!=turn_intent.work_id):_deny()
    if type(row) is AssistantTurnAuthority and turn_intent is None:_deny()
    if row is None:
        if CONTEXT_KEY in context: _deny()
        return None
    from backend.app.modules.assistant.workspace_binding import validate_origin
    validate_origin(session, row)
    principal = _from_row(row)
    if (type(marker) is not dict or marker != frozen(principal)
            or type(marker.get('version')) is not int
            or (run.id, run.requester_id, run.organization_id, run.store_id) !=
               (principal.run_id, principal.subject_id, principal.organization_id, principal.store_id)
            or (execution.run_id, execution.account_id, execution.session_id,
                execution.account_version, _utc(execution.authorization_expires_at), execution.authorization_epoch) !=
               (principal.run_id, principal.account_id, principal.session_id,
                principal.account_version, principal.auth_expires_at, principal.work_epoch)):
        _deny()
    return principal
