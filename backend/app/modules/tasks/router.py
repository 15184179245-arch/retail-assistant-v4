from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.application.contracts import CommandIntent
from backend.app.application.dispatch import execute_http
from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.tasks.schemas import (
    TaskAssignment, TaskCreate, TaskDecision, TaskEnvelope, TaskEventsEnvelope,
    TaskEventResponse, TaskListEnvelope, TaskManagedCreate, TaskReason, TaskResponse,
    TaskSuggestion,
)
from backend.app.modules.tasks.service import (
    accept_task, assign_task, close_task, create_managed_task, create_task, decide_task, get_task_for_scope,
    list_tasks_for_scope, start_task, task_events_for_scope, verify_task,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _envelope(task, message: str = "任务状态已更新。") -> TaskEnvelope:
    return TaskEnvelope(success=True, data=TaskResponse.model_validate(task), code="OK",
                        error=None, user_message=message, hint=None)


def _reason(payload: TaskReason) -> str:
    return payload.reason


@router.get("", response_model=TaskListEnvelope)
def list_route(actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
               session: Annotated[Session, Depends(get_session)]) -> TaskListEnvelope:
    tasks = list_tasks_for_scope(session, actor)
    return TaskListEnvelope(success=True, data=[TaskResponse.model_validate(task) for task in tasks],
                            code="OK", error=None, user_message="任务列表已读取。", hint=None)


@router.post("", response_model=TaskEnvelope, status_code=201)
def create_route(payload: TaskCreate,
                 actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                 session: Annotated[Session, Depends(get_session)],
                 key: Annotated[str, Depends(require_idempotency_key)]) -> TaskEnvelope:
    with session.begin():
        task = execute_http(session, actor, CommandIntent(action="tasks.create", payload=payload.model_dump(), idempotency_key=key)).data
    return _envelope(task, "任务建议已记录，尚未成为员工任务。")


@router.post("/managed", response_model=TaskEnvelope, status_code=201)
def create_managed_route(payload: TaskManagedCreate,
                         actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                         session: Annotated[Session, Depends(get_session)],
                         key: Annotated[str, Depends(require_idempotency_key)]) -> TaskEnvelope:
    with session.begin():
        task = execute_http(session, actor, CommandIntent(action="tasks.manage", payload=payload.model_dump(), idempotency_key=key)).data
    return _envelope(task, "人工管理任务已创建。")


@router.post("/suggestions", response_model=TaskEnvelope, status_code=201)
def create_suggestion_route(payload: TaskSuggestion,
                            actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                            session: Annotated[Session, Depends(get_session)],
                            key: Annotated[str, Depends(require_idempotency_key)]) -> TaskEnvelope:
    with session.begin():
        task = execute_http(session, actor, CommandIntent(action="tasks.suggest_review", payload=payload.model_dump(), idempotency_key=key)).data
    return _envelope(task, "Agent 建议已提交指定负责人。")


@router.get("/{task_id}", response_model=TaskEnvelope)
def get_route(task_id: str, actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
              session: Annotated[Session, Depends(get_session)]) -> TaskEnvelope:
    return _envelope(get_task_for_scope(session, actor, task_id), "任务已读取。")


@router.get("/{task_id}/events", response_model=TaskEventsEnvelope)
def events_route(task_id: str, actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                 session: Annotated[Session, Depends(get_session)]) -> TaskEventsEnvelope:
    events = task_events_for_scope(session, actor, task_id)
    return TaskEventsEnvelope(success=True, data=[TaskEventResponse.model_validate(event) for event in events],
                              code="OK", error=None, user_message="任务事件已读取。", hint=None)


@router.post("/{task_id}/accept", response_model=TaskEnvelope)
def accept_route(task_id: str, payload: TaskReason,
                 actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                 session: Annotated[Session, Depends(get_session)],
                 key: Annotated[str, Depends(require_idempotency_key)]) -> TaskEnvelope:
    with session.begin():
        task = execute_http(session, actor, CommandIntent(action="tasks.accept", payload=payload.model_dump(), idempotency_key=key, target_id=task_id)).data
    return _envelope(task, "任务建议已接受，等待分派。")


@router.post("/{task_id}/decision", response_model=TaskEnvelope)
def decision_route(task_id: str, payload: TaskDecision,
                   actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                   session: Annotated[Session, Depends(get_session)],
                   key: Annotated[str, Depends(require_idempotency_key)]) -> TaskEnvelope:
    with session.begin():
        task = execute_http(session, actor, CommandIntent(action="tasks.decide", payload=payload.model_dump(), idempotency_key=key, target_id=task_id)).data
    return _envelope(task, "负责人裁决已记录。")


@router.post("/{task_id}/assign", response_model=TaskEnvelope)
def assign_route(task_id: str, payload: TaskAssignment,
                 actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                 session: Annotated[Session, Depends(get_session)],
                 key: Annotated[str, Depends(require_idempotency_key)]) -> TaskEnvelope:
    with session.begin():
        task = execute_http(session, actor, CommandIntent(action="tasks.assign", payload=payload.model_dump(), idempotency_key=key, target_id=task_id)).data
    return _envelope(task, "任务已分派。")


@router.post("/{task_id}/start", response_model=TaskEnvelope)
def start_route(task_id: str, payload: TaskReason,
                actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                session: Annotated[Session, Depends(get_session)],
                key: Annotated[str, Depends(require_idempotency_key)]) -> TaskEnvelope:
    with session.begin():
        task = execute_http(session, actor, CommandIntent(action="tasks.start", payload=payload.model_dump(), idempotency_key=key, target_id=task_id)).data
    return _envelope(task, "任务已开始。")


@router.post("/{task_id}/verify", response_model=TaskEnvelope)
def verify_route(task_id: str, payload: TaskReason,
                 actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                 session: Annotated[Session, Depends(get_session)],
                 key: Annotated[str, Depends(require_idempotency_key)]) -> TaskEnvelope:
    with session.begin():
        task = execute_http(session, actor, CommandIntent(action="tasks.verify", payload=payload.model_dump(), idempotency_key=key, target_id=task_id)).data
    return _envelope(task, "任务已由其他获权人员核验。")


@router.post("/{task_id}/close", response_model=TaskEnvelope)
def close_route(task_id: str, payload: TaskReason,
                actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                session: Annotated[Session, Depends(get_session)],
                key: Annotated[str, Depends(require_idempotency_key)]) -> TaskEnvelope:
    with session.begin():
        task = execute_http(session, actor, CommandIntent(action="tasks.close", payload=payload.model_dump(), idempotency_key=key, target_id=task_id)).data
    return _envelope(task, "任务已关闭。")
