import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject, ensure_subject_active
from backend.app.modules.identity.models import PermissionGrant
from backend.app.modules.identity.repository import grants_for, load_scope


def effective_permission_grants(grants, subject_id: str, *, now: datetime | None = None) -> list:
    """Return the selected, unexpired personal or role layer."""
    current = now or datetime.now(timezone.utc)
    effective = []
    for grant in grants:
        expires = grant.expires_at
        if expires is not None:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= current:
                continue
        effective.append(grant)
    personal = [grant for grant in effective if grant.subject_id == subject_id]
    role_grants = [grant for grant in effective if grant.subject_id is None]
    return personal or role_grants


def permission_allowed(grants, subject_id: str, *, now: datetime | None = None) -> bool:
    """One policy shared by runtime authorization and trusted-host inspection."""
    decisions = effective_permission_grants(grants, subject_id, now=now)
    return bool(decisions) and all(grant.granted for grant in decisions)


def authorize_scope(session: Session, actor: AuthenticatedSubject, *, organization_id: str, store_id: str, permission: str | None = None, now: datetime | None = None) -> None:
    from backend.app.modules.assistant.principal import WorkPrincipal, check
    if type(actor) is WorkPrincipal:
        if (organization_id, store_id) != (actor.organization_id, actor.store_id):
            raise HTTPException(403, 'store_scope_denied')
        # Invalid work authority remains fatal; unavailable optional tool
        # capabilities use the registry's standard permission-denied contract.
        check(session, actor, now=now)
        if permission and permission != 'agents.run':
            try:
                check(session, actor, permission=permission, now=now)
            except HTTPException as error:
                if error.status_code != 403:
                    raise
                raise HTTPException(403, 'permission_denied') from error
        return
    ensure_subject_active(actor, now)
    if actor.account_id is not None or actor.session_id is not None:
        from backend.app.modules.auth.service import validate_actor_session
        validate_actor_session(session, actor, now=now)
    # Scope is sourced from the authenticated server context; caller inputs must match it.
    if (organization_id, store_id) != (actor.organization_id, actor.store_id):
        raise HTTPException(status_code=403, detail="store_scope_denied")
    organization, store, subject, roles = load_scope(session, actor.organization_id, actor.store_id, actor.subject_id)
    if not organization or not organization.active or not store or not store.active or not subject or not subject.active:
        raise HTTPException(status_code=403, detail="identity_inactive")
    if subject.organization_id != actor.organization_id or store.organization_id != actor.organization_id:
        raise HTTPException(status_code=403, detail="organization_scope_denied")
    if permission is None:
        return
    grants = grants_for(session, actor.organization_id, actor.store_id, actor.subject_id, roles, permission)
    # A valid personal layer overrides roles. Within the selected layer, any
    # denial wins; an absent allow is also a denial.
    if not permission_allowed(grants, actor.subject_id, now=now):
        raise HTTPException(status_code=403, detail="permission_denied")


def resolve_effective_capabilities(
    session: Session,
    actor: AuthenticatedSubject,
    *,
    now: datetime | None = None,
) -> tuple[list[str], str]:
    """Resolve active capabilities and a deterministic version for navigation rendering.

    Reuses existing permission_admin.KNOWN_PERMISSIONS, load_scope, and permission_allowed
    logic without creating a second authorization authority.
    """
    from backend.app.modules.identity.permission_admin import KNOWN_PERMISSIONS

    current = now or datetime.now(timezone.utc)
    organization, store, subject, roles = load_scope(session, actor.organization_id, actor.store_id, actor.subject_id)
    if not organization or not store or not subject or not organization.active or not store.active or not subject.active:
        return [], "inactive"

    statement = select(PermissionGrant).where(
        PermissionGrant.organization_id == actor.organization_id,
        PermissionGrant.store_id == actor.store_id,
        PermissionGrant.active.is_(True),
    )
    all_grants = session.scalars(statement).all()
    grants_by_perm: dict[str, list[PermissionGrant]] = {}
    for g in all_grants:
        grants_by_perm.setdefault(g.permission_key, []).append(g)

    effective_caps: list[str] = []
    deciding_signatures: list[str] = []

    for permission in sorted(KNOWN_PERMISSIONS):
        raw_grants = grants_by_perm.get(permission, [])
        scope_grants = [
            g for g in raw_grants
            if (g.subject_id == actor.subject_id or (g.subject_id is None and g.role_key in roles))
        ]
        if permission_allowed(scope_grants, actor.subject_id, now=current):
            effective_caps.append(permission)
            decisions = effective_permission_grants(scope_grants, actor.subject_id, now=current)
            deciding_signatures.extend(sorted(f"{d.id}:{d.granted}" for d in decisions))

    payload = f"{actor.subject_id}@{actor.organization_id}/{actor.store_id}:" + ",".join(effective_caps) + ":" + ";".join(deciding_signatures)
    cap_version = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return effective_caps, cap_version
