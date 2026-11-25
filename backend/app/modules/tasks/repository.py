from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from backend.app.modules.tasks.models import Task, TaskEvent, TaskIdempotency


def get_task(session: Session, organization_id: str, store_id: str, task_id: str) -> Task | None:
    return session.scalar(select(Task).where(
        Task.organization_id == organization_id,
        Task.store_id == store_id,
        Task.id == task_id,
    ))


def get_idempotency(session: Session, organization_id: str, store_id: str, key: str) -> TaskIdempotency | None:
    return session.scalar(select(TaskIdempotency).where(
        TaskIdempotency.organization_id == organization_id,
        TaskIdempotency.store_id == store_id,
        TaskIdempotency.idempotency_key == key,
    ))


def compare_and_set_task_state(
    session: Session, *, organization_id: str, store_id: str, task_id: str,
    expected_state: str, values: dict, expected_decision_state: str | None = None,
) -> bool:
    statement = update(Task).where(Task.organization_id == organization_id, Task.store_id == store_id,
                                   Task.id == task_id, Task.state == expected_state)
    if expected_decision_state is not None:
        statement = statement.where(Task.decision_state == expected_decision_state)
    result = session.execute(
        statement
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def list_tasks(session: Session, organization_id: str, store_id: str) -> list[Task]:
    return list(session.scalars(select(Task).where(
        Task.organization_id == organization_id, Task.store_id == store_id,
    ).order_by(Task.created_at.desc(), Task.id)))


def list_tasks_visible(session: Session, *, organization_id: str, store_id: str,
                       subject_id: str, read_self: bool, read_responsible: bool,
                       limit: int | None = None) -> list[Task]:
    visible = []
    if read_self:
        visible.extend((Task.created_by == subject_id, Task.assignee_id == subject_id))
    if read_responsible:
        visible.append(Task.reviewer_id == subject_id)
    query = select(Task).where(
        Task.organization_id == organization_id, Task.store_id == store_id,
        or_(*visible),
    ).order_by(Task.created_at.desc(), Task.id)
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query))


def list_events(session: Session, organization_id: str, store_id: str, task_id: str) -> list[TaskEvent]:
    return list(session.scalars(select(TaskEvent).where(
        TaskEvent.organization_id == organization_id,
        TaskEvent.store_id == store_id,
        TaskEvent.task_id == task_id,
    ).order_by(TaskEvent.occurred_at, TaskEvent.id)))
