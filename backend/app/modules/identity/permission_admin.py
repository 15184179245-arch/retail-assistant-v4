"""Trusted-host provisioning only; no API or Agent tool exposes these writes."""

from datetime import datetime, timezone
import hashlib
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.modules.audit.repository import get_by_idempotency_key
from backend.app.modules.audit.service import record_event
from backend.app.modules.auth.models import EmployeeAccount
from backend.app.modules.identity.command_guard import (
    ScopeSecurityFenceError,
    advance_scope_security_fence,
    lock_scope_security_fence,
)
from backend.app.modules.identity.models import PermissionGrant, Subject
from backend.app.modules.identity.repository import grants_for, load_scope
from backend.app.modules.identity.service import effective_permission_grants, permission_allowed

KNOWN_PERMISSIONS = frozenset({
    "guanjia.company.read", "guanjia.catalog.read", "guanjia.inventory.read", "guanjia.pricing.read",
    "guanjia.sales.read", "guanjia.customers.read", "guanjia.finance.read",
    "audit.read", "machine.manage", "approvals.request", "approvals.read", "approvals.decide",
    "approvals.cancel", "approvals.expire", "tasks.suggest", "tasks.read",
    "tasks.accept", "tasks.assign", "tasks.start", "tasks.verify", "tasks.close",
    "tasks.manage", "tasks.decide", "tasks.read_self", "tasks.read_responsible",
    "agents.run", "agents.tasks.list_mine", "agents.tasks.suggest_review",
    "agents.catalog.search", "catalog.read", "agents.knowledge.search", "agents.memory.search",
    "agents.memory.save", "agents.memory.correct", "agents.memory.withdraw",
    "agents.stocktake.read_evidence",
    "agents.purchasing.read_evidence",
    "agents.guanjia.sales.read", "agents.guanjia.prices.read", "agents.guanjia.stock.read", "agents.guanjia.research.read",
    "agents.cost.read_self", "agents.cost.read_store",
    "skills.submit", "skills.evaluate", "skills.approve", "skills.activate",
    "skills.patterns.discover", "skills.patterns.propose",
    "knowledge.submit", "knowledge.review", "knowledge.read",
    "pricing.quote", "pricing.approve", "pricing.cost.read",
    "sales.draft", "sales.read", "sales.confirm", "sales.handoff",
    "analytics.read",
    "erp.command.propose", "erp.command.approve", "erp.command.stage", "erp.command.read",
    "erp.sandbox.dispatch", "erp.sandbox.reconcile",
    "erp.sales.read", "erp.sales.propose", "erp.sales.approve", "erp.sales.dispatch", "erp.sales.reconcile",
    "erp.receiving.read", "erp.receiving.propose", "erp.receiving.approve", "erp.receiving.dispatch", "erp.receiving.reconcile",
    "erp.catalog.read", "erp.catalog.propose", "erp.catalog.approve", "erp.catalog.dispatch", "erp.catalog.reconcile", "erp.catalog.cancel",
    "erp.catalog.text_update", "erp.catalog.image_upload", "erp.catalog.image_bind",
    "erp.settlement.read", "erp.settlement.propose", "erp.settlement.approve", "erp.settlement.dispatch", "erp.settlement.reconcile",
    "erp.settlement.receipt_allocation", "erp.settlement.payment_allocation",
    "office.workflows.read", "office.workflows.manage", "office.workflows.advance",
    "office.schedules.read", "office.schedules.manage", "office.schedules.approve",
    "office.exceptions.read", "office.exceptions.manage", "office.exceptions.claim",
    "office.exceptions.takeover", "office.exceptions.transfer", "office.exceptions.resolve",
    "office.intake.read", "office.intake.write",
    "office.extraction.read", "office.extraction.submit", "office.extraction.review",
    "office.rules.read", "office.rules.manage", "office.rules.review", "office.documents.read", "office.documents.write", "office.documents.review", "office.documents.export",
    "office.finance.read", "office.finance.write",
    "office.review.read", "office.review.claim", "office.review.decide", "office.review.export", "office.review.batch",
    "purchasing.read", "purchasing.write", "receiving.read", "receiving.write", "receiving.review", "stocktake.read", "stocktake.write",
    "notifications.read", "notifications.ack", "notifications.manage",
    "media.read", "media.write", "locations.read", "locations.write", "memory.read", "memory.write",
    "memory.promote", "memory.restricted.read",
})


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("permission maintenance times must include a timezone")
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _account(session: Session, account_id: str):
    account = session.get(EmployeeAccount, account_id)
    if account is None or not account.active:
        raise ValueError("permission maintenance requires an active employee account")
    organization, store, person, roles = load_scope(session, account.organization_id, account.store_id, account.subject_id)
    if not (organization and organization.active and store and store.active and person and person.active
            and person.subject_type == "person" and store.organization_id == account.organization_id
            and person.organization_id == account.organization_id):
        raise ValueError("permission maintenance requires an active employee scope")
    return account, roles


def change_personal_permission(
    session: Session, *, account_id: str, permission: str, mode: str, reason: str,
    operator_id: str, request_id: str, expires_at: datetime | None = None,
    now: datetime | None = None,
) -> dict:
    """Replace a personal override and its audit in the caller's transaction."""
    current = _utc(now) if now is not None else datetime.now(timezone.utc)
    permission, reason, request_id = permission.strip(), reason.strip(), request_id.strip()
    if permission not in KNOWN_PERMISSIONS or mode not in ("allow", "deny", "clear"):
        raise ValueError("unsupported permission operation")
    if not 1 <= len(reason) <= 1000 or not 1 <= len(request_id) <= 200:
        raise ValueError("permission reason or request identifier is invalid")
    try:
        reason.encode("utf-8")
        request_bytes = request_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("permission parameters must contain valid Unicode") from exc
    deadline = _utc(expires_at) if expires_at is not None else None
    if mode == "clear" and deadline is not None:
        raise ValueError("clearing an override cannot set an expiry")
    account, _roles = _account(session, account_id)
    expected_operator = str(uuid5(NAMESPACE_URL, f"urn:retail:host-account-admin:{account.organization_id}"))
    operator = session.get(Subject, operator_id)
    if not (operator and operator.active and operator.subject_type == "service"
            and operator.organization_id == account.organization_id and operator.id == expected_operator):
        raise ValueError("permission maintenance requires the host account operator")

    # The scope security fence serializes this change with every other
    # revocation-capable change in the same organization/store. Snapshot rows
    # from before the lock are dropped and re-read after taking it.
    try:
        lock_scope_security_fence(session, account.organization_id, account.store_id,
                                  exclusive=True, now=current)
    except ScopeSecurityFenceError as exc:
        raise ValueError("permission scope security fence is unavailable") from exc
    session.expire_all()
    account, _roles = _account(session, account_id)
    operator = session.get(Subject, operator_id, populate_existing=True)
    if not (operator and operator.active and operator.subject_type == "service"
            and operator.organization_id == account.organization_id and operator.id == expected_operator):
        raise ValueError("permission maintenance requires the host account operator")
    session.refresh(account)
    key = f"permission:{account.id}:{hashlib.sha256(request_bytes).hexdigest()}"
    payload = {"permission": permission, "mode": mode, "reason": reason,
               "expires_at": deadline.isoformat() if deadline else None}
    existing = get_by_idempotency_key(session, account.organization_id, account.store_id, key)
    if existing is not None:
        if (existing.actor_id != operator_id or existing.action != f"permission.{mode}"
                or existing.target_type != "employee_account" or existing.target_id != account.id
                or existing.details.get("request") != payload
                or not isinstance(existing.details.get("result"), dict)):
            raise ValueError("permission request identifier conflicts with an earlier operation")
        return {**existing.details["result"], "replayed": True}
    if deadline is not None and deadline <= current:
        raise ValueError("permission expiry must be in the future")

    scope = (
        PermissionGrant.organization_id == account.organization_id,
        PermissionGrant.store_id == account.store_id,
        PermissionGrant.subject_id == account.subject_id,
        PermissionGrant.role_key.is_(None),
        PermissionGrant.permission_key == permission,
        PermissionGrant.active.is_(True),
    )
    previous_ids = list(session.scalars(select(PermissionGrant.id).where(*scope)))
    session.execute(update(PermissionGrant).where(*scope).values(active=False)
        .execution_options(synchronize_session=False))
    grant_id = None
    if mode != "clear":
        grant_id = str(uuid4())
        session.add(PermissionGrant(id=grant_id, organization_id=account.organization_id,
            store_id=account.store_id, subject_id=account.subject_id, role_key=None,
            permission_key=permission, granted=(mode == "allow"), expires_at=deadline,
            active=True, created_at=current))
    result = {"permission": permission, "mode": mode, "grant_id": grant_id}
    if mode != "clear" or previous_ids:
        advance_scope_security_fence(session, account.organization_id, account.store_id, now=current)
    record_event(session, actor_id=operator_id, organization_id=account.organization_id,
        store_id=account.store_id, action=f"permission.{mode}", target_type="employee_account",
        target_id=account.id, idempotency_key=key, occurred_at=current,
        details={"request": payload, "result": result, "superseded_grant_ids": previous_ids})
    session.flush()
    return {**result, "replayed": False}


def change_agent_permission(
    session: Session, *, organization_id: str, store_id: str, account_id: str,
    agent_id: str, permission: str, mode: str, reason: str,
    operator_id: str, request_id: str, expires_at: datetime | None = None,
    now: datetime | None = None,
) -> dict:
    """Host-only sales Agent override, anchored to an active employee store.

    The caller owns the transaction, including rollback on any raised error.
    Integrity/config pins do not call this service or confer these grants.
    """
    current = _utc(now) if now is not None else datetime.now(timezone.utc)
    permission, reason, request_id = permission.strip(), reason.strip(), request_id.strip()
    if permission != "agents.guanjia.sales.read" or mode not in ("allow", "deny", "clear"):
        raise ValueError("unsupported permission operation")
    if not 1 <= len(reason) <= 1000 or not 1 <= len(request_id) <= 200:
        raise ValueError("permission reason or request identifier is invalid")
    try:
        reason.encode("utf-8")
        request_bytes = request_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("permission parameters must contain valid Unicode") from exc
    deadline = _utc(expires_at) if expires_at is not None else None
    if mode == "clear" and deadline is not None:
        raise ValueError("clearing an override cannot set an expiry")
    account, _roles = _account(session, account_id)
    agent = session.get(Subject, agent_id, populate_existing=True)
    if ((account.organization_id, account.store_id) != (organization_id, store_id)
            or agent is None or not agent.active or agent.subject_type != 'agent'
            or agent.organization_id != organization_id):
        raise ValueError('permission maintenance requires an active same-store Agent scope')
    expected_operator = str(uuid5(NAMESPACE_URL, f"urn:retail:host-account-admin:{account.organization_id}"))
    operator = session.get(Subject, operator_id)
    if not (operator and operator.active and operator.subject_type == "service"
            and operator.organization_id == account.organization_id and operator.id == expected_operator):
        raise ValueError("permission maintenance requires the host account operator")

    # The scope security fence serializes this change with every other
    # revocation-capable change in the same organization/store. Snapshot rows
    # from before the lock are dropped and re-read after taking it.
    try:
        lock_scope_security_fence(session, account.organization_id, account.store_id,
                                  exclusive=True, now=current)
    except ScopeSecurityFenceError as exc:
        raise ValueError("permission scope security fence is unavailable") from exc
    session.expire_all()
    account, _roles = _account(session, account_id)
    agent = session.get(Subject, agent_id, populate_existing=True)
    if ((account.organization_id, account.store_id) != (organization_id, store_id)
            or agent is None or not agent.active or agent.subject_type != 'agent'
            or agent.organization_id != organization_id):
        raise ValueError('permission maintenance requires an active same-store Agent scope')
    operator = session.get(Subject, operator_id, populate_existing=True)
    if not (operator and operator.active and operator.subject_type == "service"
            and operator.organization_id == account.organization_id and operator.id == expected_operator):
        raise ValueError("permission maintenance requires the host account operator")
    session.refresh(account)
    key = f"agent-permission:{agent.id}:{hashlib.sha256(request_bytes).hexdigest()}"
    payload = {"permission": permission, "mode": mode, "reason": reason,
               "expires_at": deadline.isoformat() if deadline else None, "account_id": account.id}
    existing = get_by_idempotency_key(session, account.organization_id, account.store_id, key)
    if existing is not None:
        if (existing.actor_id != operator_id or existing.action != f"agent_permission.{mode}"
                or existing.target_type != "agent_subject" or existing.target_id != agent.id
                or existing.details.get("request") != payload
                or not isinstance(existing.details.get("result"), dict)):
            raise ValueError("permission request identifier conflicts with an earlier operation")
        return {**existing.details["result"], "replayed": True}
    if deadline is not None and deadline <= current:
        raise ValueError("permission expiry must be in the future")

    if mode == 'allow' and (deadline is None or (deadline-current).total_seconds() > 86400):
        raise ValueError('Agent sales allow requires an expiry within 24 hours')
    if mode == 'allow':
        agent_roles = load_scope(session, organization_id, store_id, agent.id)[3]
        grants = grants_for(session, organization_id, store_id, agent.id, agent_roles, permission)
        selected = effective_permission_grants(grants, agent.id, now=current)
        if any(not grant.granted for grant in selected):
            raise ValueError('an explicit Agent denial requires separate reconciliation')

    scope = (
        PermissionGrant.organization_id == account.organization_id,
        PermissionGrant.store_id == account.store_id,
        PermissionGrant.subject_id == agent.id,
        PermissionGrant.role_key.is_(None),
        PermissionGrant.permission_key == permission,
        PermissionGrant.active.is_(True),
    )
    previous_ids = list(session.scalars(select(PermissionGrant.id).where(*scope)))
    session.execute(update(PermissionGrant).where(*scope).values(active=False)
        .execution_options(synchronize_session=False))
    grant_id = None
    if mode != "clear":
        grant_id = str(uuid4())
        session.add(PermissionGrant(id=grant_id, organization_id=account.organization_id,
            store_id=account.store_id, subject_id=agent.id, role_key=None,
            permission_key=permission, granted=(mode == "allow"), expires_at=deadline,
            active=True, created_at=current))
    result = {"permission": permission, "mode": mode, "grant_id": grant_id}
    if mode != "clear" or previous_ids:
        advance_scope_security_fence(session, account.organization_id, account.store_id, now=current)
    record_event(session, actor_id=operator_id, organization_id=account.organization_id,
        store_id=account.store_id, action=f"agent_permission.{mode}", target_type="agent_subject",
        target_id=agent.id, idempotency_key=key, occurred_at=current,
        details={"request": payload, "result": result, "superseded_grant_ids": previous_ids})
    session.flush()
    return {**result, "replayed": False}


def permission_report(session: Session, *, account_id: str, now: datetime | None = None) -> list[dict]:
    current = _utc(now) if now is not None else datetime.now(timezone.utc)
    account, roles = _account(session, account_id)
    report = []
    for permission in sorted(KNOWN_PERMISSIONS):
        grants = grants_for(session, account.organization_id, account.store_id, account.subject_id, roles, permission)
        selected = effective_permission_grants(grants, account.subject_id, now=current)
        selected_ids = {grant.id for grant in selected}
        selected_layer = ("personal" if selected[0].subject_id else "role") if selected else None
        sources = []
        for grant in grants:
            deadline = _stored_utc(grant.expires_at)
            sources.append({"grant_id": grant.id, "layer": "personal" if grant.subject_id else "role",
                "role_key": grant.role_key, "granted": grant.granted,
                "expires_at": deadline.isoformat() if deadline else None,
                "currently_valid": deadline is None or deadline > current,
                "participates_in_decision": grant.id in selected_ids})
        report.append({"permission": permission,
            "allowed": permission_allowed(grants, account.subject_id, now=current),
            "selected_layer": selected_layer,
            "sources": sorted(sources, key=lambda source: source["grant_id"])})
    return report
