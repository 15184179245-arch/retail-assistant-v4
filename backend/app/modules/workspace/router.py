"""FastAPI HTTP routes for WSP-001 Workspace Artifacts & ThreadWorkspace and WSP-002 UI Blocks."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from backend.app.core.errors import ApiEnvelope
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.workspace import blocks as block_service
from backend.app.modules.workspace import service
from backend.app.modules.workspace.schemas import (
    ArtifactCreateRequest,
    ArtifactDetailEnvelope,
    ArtifactListData,
    ArtifactListEnvelope,
    ArtifactLockRequest,
    ArtifactMutationEnvelope,
    ArtifactPatchRequest,
    ArtifactSubmitRequest,
    ArtifactSuggestionDecisionRequest,
    ArtifactUndoRequest,
    ArtifactUnlockRequest,
    BlockPinEnvelope,
    BlockPinRequest,
    BlockResolveData,
    BlockResolveEnvelope,
    BlockResolveRequest,
    ThreadEventsEnvelope,
    ThreadWorkspaceEnvelope,
    ThreadWorkspaceUpsertRequest,
)

router = APIRouter(prefix="/workspace", tags=["workspace"])


def _error_response(exc: service.WorkspaceError) -> JSONResponse:
    envelope = ApiEnvelope(
        success=False,
        data=exc.data,
        code=exc.code,
        error=exc.error,
        user_message=exc.user_message,
        hint=exc.hint,
    )
    return JSONResponse(status_code=exc.status_code, content=envelope.model_dump())


def _ok(cls: type[ApiEnvelope], data: Any, message: str, hint: str | None = None):
    return cls(
        success=True,
        data=data,
        code="OK",
        error=None,
        user_message=message,
        hint=hint,
    )


def _run_read(session: Session, fn, envelope_cls: type[ApiEnvelope], message: str):
    try:
        data = fn()
        return _ok(envelope_cls, data, message)
    except service.WorkspaceError as exc:
        return _error_response(exc)


def _run_write(session: Session, fn, envelope_cls: type[ApiEnvelope], message_fn):
    try:
        data = fn()
        session.commit()
    except service.WorkspaceError as exc:
        session.rollback()
        return _error_response(exc)
    except Exception:
        session.rollback()
        raise
    msg = message_fn(data) if callable(message_fn) else message_fn
    return _ok(envelope_cls, data, msg)


@router.get("/artifacts", response_model=ArtifactListEnvelope)
def list_artifacts_route(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    thread_id: Annotated[str | None, Query(max_length=64)] = None,
):
    return _run_read(
        session,
        lambda: ArtifactListData(items=service.list_artifacts(session, actor, thread_id=thread_id)),
        ArtifactListEnvelope,
        "已加载工作区对象列表。",
    )


@router.post("/artifacts", response_model=ArtifactMutationEnvelope, status_code=201)
def create_artifact_route(
    payload: ArtifactCreateRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    return _run_write(
        session,
        lambda: service.create_artifact(
            session,
            actor,
            payload,
            header_idempotency_key=idempotency_key,
        ),
        ArtifactMutationEnvelope,
        lambda d: d.message or "工作区对象已创建。",
    )


@router.get("/artifacts/{artifact_id}", response_model=ArtifactDetailEnvelope)
def get_artifact_route(
    artifact_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
):
    return _run_read(
        session,
        lambda: service.get_artifact_detail(session, actor, artifact_id),
        ArtifactDetailEnvelope,
        "已加载工作区对象详情。",
    )


@router.patch("/artifacts/{artifact_id}", response_model=ArtifactMutationEnvelope)
def patch_artifact_route(
    artifact_id: str,
    payload: ArtifactPatchRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    return _run_write(
        session,
        lambda: service.patch_artifact(
            session,
            actor,
            artifact_id,
            payload,
            header_idempotency_key=idempotency_key,
        ),
        ArtifactMutationEnvelope,
        lambda d: d.message or "工作区对象已更新。",
    )


@router.post("/artifacts/{artifact_id}/lock", response_model=ArtifactMutationEnvelope)
def lock_artifact_fields_route(
    artifact_id: str,
    payload: ArtifactLockRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
):
    return _run_write(
        session,
        lambda: service.lock_fields(session, actor, artifact_id, payload),
        ArtifactMutationEnvelope,
        lambda d: d.message or "已锁定字段。",
    )


@router.post("/artifacts/{artifact_id}/unlock", response_model=ArtifactMutationEnvelope)
def unlock_artifact_field_route(
    artifact_id: str,
    payload: ArtifactUnlockRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
):
    return _run_write(
        session,
        lambda: service.unlock_fields(session, actor, artifact_id, payload),
        ArtifactMutationEnvelope,
        lambda d: d.message or "已解除字段锁定。",
    )


@router.post(
    "/artifacts/{artifact_id}/suggestions/{op_id}/accept",
    response_model=ArtifactMutationEnvelope,
)
def accept_suggestion_route(
    artifact_id: str,
    op_id: str,
    payload: ArtifactSuggestionDecisionRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
):
    return _run_write(
        session,
        lambda: service.accept_suggestion(session, actor, artifact_id, op_id, payload),
        ArtifactMutationEnvelope,
        lambda d: d.message or "已采纳 AI 建议并保留字段锁。",
    )


@router.post(
    "/artifacts/{artifact_id}/suggestions/{op_id}/ignore",
    response_model=ArtifactMutationEnvelope,
)
def ignore_suggestion_route(
    artifact_id: str,
    op_id: str,
    payload: ArtifactSuggestionDecisionRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
):
    return _run_write(
        session,
        lambda: service.ignore_suggestion(session, actor, artifact_id, op_id, payload),
        ArtifactMutationEnvelope,
        lambda d: d.message or "已忽略 AI 建议并保留字段锁。",
    )


@router.post("/artifacts/{artifact_id}/undo", response_model=ArtifactMutationEnvelope)
def undo_artifact_op_route(
    artifact_id: str,
    payload: ArtifactUndoRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    return _run_write(
        session,
        lambda: service.undo_op(
            session,
            actor,
            artifact_id,
            payload,
            header_idempotency_key=idempotency_key,
        ),
        ArtifactMutationEnvelope,
        lambda d: d.message or "已执行部分撤销。",
    )


@router.post("/artifacts/{artifact_id}/submit", response_model=ArtifactMutationEnvelope)
def submit_artifact_route(
    artifact_id: str,
    payload: ArtifactSubmitRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    return _run_write(
        session,
        lambda: service.submit_artifact(
            session,
            actor,
            artifact_id,
            payload,
            header_idempotency_key=idempotency_key,
        ),
        ArtifactMutationEnvelope,
        lambda d: d.message or "已保存核对结果，正式单据未提交。",
    )


@router.get("/threads/{thread_id}", response_model=ThreadWorkspaceEnvelope)
def get_thread_workspace_route(
    thread_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
):
    return _run_read(
        session,
        lambda: service.get_thread_workspace(session, actor, thread_id),
        ThreadWorkspaceEnvelope,
        "已加载线程工作区状态。",
    )


@router.put("/threads/{thread_id}", response_model=ThreadWorkspaceEnvelope)
def upsert_thread_workspace_route(
    thread_id: str,
    payload: ThreadWorkspaceUpsertRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
):
    return _run_write(
        session,
        lambda: service.upsert_thread_workspace(session, actor, thread_id, payload),
        ThreadWorkspaceEnvelope,
        "线程工作区状态已保存，可跨端接续。",
    )


@router.get("/threads/{thread_id}/events", response_model=ThreadEventsEnvelope)
def list_thread_events_route(
    thread_id: str,
    request: Request,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    stream: Annotated[bool, Query()] = False,
):
    try:
        data = service.list_thread_events(session, actor, thread_id, cursor=cursor, limit=limit)
    except service.WorkspaceError as exc:
        return _error_response(exc)

    accept = request.headers.get("accept", "")
    if stream or "text/event-stream" in accept.lower():
        def _sse_frames():
            for event in data.events:
                frame_payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
                yield f"id: {event.seq}\nevent: {event.event_type}\ndata: {frame_payload}\n\n"
            yield f": cursor={data.next_cursor}\n\n"

        return StreamingResponse(_sse_frames(), media_type="text/event-stream")

    return _ok(ThreadEventsEnvelope, data, "已获取线程工作区增量事件。")


@router.post("/blocks/resolve", response_model=BlockResolveEnvelope)
def resolve_blocks_route(
    payload: BlockResolveRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
):
    raw_blocks = payload.extract_blocks()
    if not raw_blocks:
        return _error_response(
            service.WorkspaceError(
                status_code=422,
                code="MISSING_BLOCK",
                error="missing_block_payload",
                user_message="请提供待解析的 UI 块 block 或 blocks",
            )
        )

    def _resolve_all() -> BlockResolveData:
        resolved = [block_service.resolve_block(session, actor, item) for item in raw_blocks]
        return BlockResolveData(block=resolved[0], blocks=resolved)

    return _run_read(
        session,
        _resolve_all,
        BlockResolveEnvelope,
        "已完成 UI 块契约校验与服务端权威数据解析。",
    )


@router.post("/blocks/pin", response_model=BlockPinEnvelope, status_code=201)
def pin_block_route(
    payload: BlockPinRequest,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    return _run_write(
        session,
        lambda: block_service.pin_block_to_artifact(
            session,
            actor,
            payload,
            header_idempotency_key=idempotency_key,
        ),
        BlockPinEnvelope,
        "已将 UI 块固定为工作区协同对象。",
    )
