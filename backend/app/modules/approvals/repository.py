from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from backend.app.modules.approvals.models import ApprovalIdempotency, ApprovalRequest


def get_approval(session: Session, organization_id: str, store_id: str, approval_id: str, *, lock: bool = False) -> ApprovalRequest | None:
    statement = select(ApprovalRequest).where(
        ApprovalRequest.organization_id == organization_id,
        ApprovalRequest.store_id == store_id,
        ApprovalRequest.id == approval_id,
    )
    if lock:
        statement = statement.with_for_update()
    return session.scalar(statement)


def get_idempotency(session: Session, organization_id: str, store_id: str, key: str) -> ApprovalIdempotency | None:
    statement = select(ApprovalIdempotency).where(
        ApprovalIdempotency.organization_id == organization_id,
        ApprovalIdempotency.store_id == store_id,
        ApprovalIdempotency.idempotency_key == key,
    )
    return session.scalar(statement)


def compare_and_set_approval_state(
    session: Session,
    *,
    organization_id: str,
    store_id: str,
    approval_id: str,
    expected_state: str,
    values: dict,
) -> bool:
    result = session.execute(
        update(ApprovalRequest)
        .where(
            ApprovalRequest.organization_id == organization_id,
            ApprovalRequest.store_id == store_id,
            ApprovalRequest.id == approval_id,
            ApprovalRequest.state == expected_state,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def list_for_scope(session: Session, organization_id: str, store_id: str) -> list[ApprovalRequest]:
    return list(session.scalars(select(ApprovalRequest).where(
        ApprovalRequest.organization_id == organization_id,
        ApprovalRequest.store_id == store_id,
    ).order_by(ApprovalRequest.requested_at.desc())))


def list_page_for_scope(
    session: Session,
    organization_id: str,
    store_id: str,
    *,
    state: str | None,
    cursor: ApprovalRequest | None,
    limit: int,
) -> tuple[list[ApprovalRequest], str | None]:
    statement = select(ApprovalRequest).where(
        ApprovalRequest.organization_id == organization_id,
        ApprovalRequest.store_id == store_id,
    )
    if state is not None:
        statement = statement.where(ApprovalRequest.state == state)
    if cursor is not None:
        statement = statement.where(or_(
            ApprovalRequest.requested_at < cursor.requested_at,
            and_(ApprovalRequest.requested_at == cursor.requested_at,
                 ApprovalRequest.id < cursor.id),
        ))
    rows = list(session.scalars(statement.order_by(
        ApprovalRequest.requested_at.desc(), ApprovalRequest.id.desc()
    ).limit(limit + 1)))
    has_more = len(rows) > limit
    items = rows[:limit]
    return items, items[-1].id if has_more and items else None
