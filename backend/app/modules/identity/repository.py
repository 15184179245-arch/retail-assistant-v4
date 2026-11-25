from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.modules.identity.models import Organization, PermissionGrant, RoleAssignment, Store, Subject


def load_scope(session: Session, organization_id: str, store_id: str, subject_id: str):
    organization = session.get(Organization, organization_id)
    store = session.get(Store, store_id)
    subject = session.get(Subject, subject_id)
    roles = session.scalars(select(RoleAssignment.role_key).where(
        RoleAssignment.organization_id == organization_id,
        RoleAssignment.store_id == store_id,
        RoleAssignment.subject_id == subject_id,
        RoleAssignment.active.is_(True),
    )).all()
    return organization, store, subject, tuple(roles)


def grants_for(session: Session, organization_id: str, store_id: str, subject_id: str, roles: tuple[str, ...], permission: str):
    statement = select(PermissionGrant).where(
        PermissionGrant.organization_id == organization_id,
        PermissionGrant.store_id == store_id,
        PermissionGrant.permission_key == permission,
        PermissionGrant.active.is_(True),
    )
    grants = session.scalars(statement).all()
    return [grant for grant in grants if (grant.subject_id == subject_id or (grant.subject_id is None and grant.role_key in roles))]
