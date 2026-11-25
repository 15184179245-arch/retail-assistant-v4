from datetime import datetime
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.orm import Session

from backend.app.core.errors import ApiEnvelope
from backend.app.core.idempotency import require_idempotency_key
from backend.app.modules.memory.schemas import (MemoryCorrect, MemoryEnvelope,
                                                MemoryListEnvelope, MemoryOut)
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.memory import service, recall, markdown_notes
from backend.app.modules.memory.schemas import MemorySearchEnvelope, MemoryNoteEnvelope, MemoryNoteImport
from backend.app.modules.memory.schemas import MemoryCommandStatusEnvelope
from backend.app.modules.memory.command_status import read_status

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=5000)
    purpose: str | None = Field(default=None, max_length=200)
    persist: bool = False
    retain_until_withdrawn: StrictBool = False
    expires_at: str | None = None


def _http(error: service.MemoryError) -> HTTPException:
    status = 404 if error.code == "memory_not_found" else (
        403 if error.code == "memory_permission_denied" else (
            409 if error.code in ("memory_idempotency_conflict",
                                  "memory_version_conflict", "memory_command_conflict",
                                  "memory_markdown_metadata_conflict") else 400))
    return HTTPException(status_code=status, detail=error.code)


_EXPIRY_OFFSET = re.compile(
    r"(?P<hour>[0-9]{2})(?:(?P<separator>:?)(?P<minute>[0-9]{2})"
    r"(?:(?P=separator)(?P<second>[0-9]{2}))?)?(?=[.,]|\Z)"
)


def _parse_expiry(value: str | None) -> datetime | None:
    if value is None:
        return None
    wire = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(wire)
    except ValueError:
        raise HTTPException(status_code=400, detail="memory_expiry_invalid")
    if parsed.utcoffset() is not None:
        # Inspect only the offset of a recognized aware value, without reinterpreting it.
        start = max(wire.rfind("+"), wire.rfind("-")) + 1
        offset = _EXPIRY_OFFSET.match(wire, start) if start else None
        if (offset is None or int(offset["hour"]) > 23
                or int(offset["minute"] or 0) > 59 or int(offset["second"] or 0) > 59):
            raise HTTPException(status_code=400, detail="memory_expiry_invalid")
    return parsed


@router.post("", response_model=MemoryEnvelope, status_code=201)
def save_route(payload: MemoryCreate,
               actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
               session: Annotated[Session, Depends(get_session)],
               idempotency_key: Annotated[str, Depends(require_idempotency_key)]) -> MemoryEnvelope:
    expires = _parse_expiry(payload.expires_at)
    try:
        result = service.save_memory(session, actor, content=payload.content,
                                     purpose=payload.purpose, persist=payload.persist,
                                     expires_at=expires, retain_until_withdrawn=payload.retain_until_withdrawn,
                                     idempotency_key=idempotency_key)
        session.commit()
    except service.MemoryError as error:
        session.rollback()
        raise _http(error) from error
    return MemoryEnvelope(success=True, data=MemoryOut(**result), code="OK", error=None,
                          user_message="个人记忆已保存。", hint=None)


@router.get("/export", response_model=MemoryListEnvelope)
def export_route(actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                 session: Annotated[Session, Depends(get_session)]) -> MemoryListEnvelope:
    try:
        items = service.list_memories(session, actor)
    except service.MemoryError as error:
        raise _http(error) from error
    return MemoryListEnvelope(success=True, data={"items": items}, code="OK", error=None,
                               user_message="个人记忆已导出。", hint=None)


@router.get("", response_model=MemoryListEnvelope)
def list_route(actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
               session: Annotated[Session, Depends(get_session)]) -> MemoryListEnvelope:
    try:
        items = service.list_memories(session, actor)
    except service.MemoryError as error:
        raise _http(error) from error
    return MemoryListEnvelope(success=True, data={"items": items}, code="OK", error=None,
                               user_message="个人记忆已读取。", hint=None)


@router.get("/search", response_model=MemorySearchEnvelope)
def search_route(actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                 session: Annotated[Session, Depends(get_session)],
                 query: Annotated[str, Query(min_length=1, max_length=256)],
                 limit: Annotated[int, Query(ge=1, le=20)] = 5) -> MemorySearchEnvelope:
    try:
        result = recall.search_memories(session, actor, query=query, limit=limit)
    except service.MemoryError as error:
        raise _http(error) from error
    return MemorySearchEnvelope(success=True, data=result, code="OK", error=None,
                               user_message="已检索当前可见的个人记忆。", hint=None)


@router.post("/notes/import", response_model=MemoryEnvelope)
def import_note_route(payload: MemoryNoteImport,
                      actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                      session: Annotated[Session, Depends(get_session)],
                      idempotency_key: Annotated[str, Depends(require_idempotency_key)]) -> MemoryEnvelope:
    try:
        item = markdown_notes.import_note(session, actor, markdown=payload.markdown,
                                          idempotency_key=idempotency_key)
        session.commit()
    except service.MemoryError as error:
        session.rollback()
        raise _http(error) from error
    return MemoryEnvelope(success=True, data=MemoryOut(**item), code='OK', error=None,
                          user_message='笔记更正已处理；返回本次操作回执。', hint=None)


@router.get("/commands/{operation}/{key}", response_model=MemoryCommandStatusEnvelope)
def command_status_route(operation: str, key: str,
                         actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                         session: Annotated[Session, Depends(get_session)]) -> MemoryCommandStatusEnvelope:
    try:
        result = read_status(session, actor, operation=operation, key=key)
    except service.MemoryError as error:
        raise _http(error) from error
    return MemoryCommandStatusEnvelope(success=True, data=result, code='OK', error=None,
        user_message='只读核对原操作；未观察到回执不表示可以重试。', hint=None)


@router.get("/{memory_id}/note", response_model=MemoryNoteEnvelope)
def export_note_route(memory_id: str,
                      actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                      session: Annotated[Session, Depends(get_session)]) -> MemoryNoteEnvelope:
    try:
        note = markdown_notes.export_note(session, actor, memory_id)
    except service.MemoryError as error:
        raise _http(error) from error
    return MemoryNoteEnvelope(success=True, data=note, code='OK', error=None,
                              user_message='已导出记忆笔记；编辑正文后可按原版本回导。', hint=None)


@router.patch("/{memory_id}", response_model=MemoryEnvelope)
def correct_route(memory_id: str, payload: MemoryCorrect,
                  actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                  session: Annotated[Session, Depends(get_session)],
                  idempotency_key: Annotated[str, Depends(require_idempotency_key)]) -> MemoryEnvelope:
    expires = _parse_expiry(payload.expires_at)
    try:
        item = service.correct_memory(session, actor, memory_id, content=payload.content,
                                      expected_version=payload.expected_version,
                                      idempotency_key=idempotency_key,
                                      purpose=payload.purpose, expires_at=expires)
        session.commit()
    except service.MemoryError as error:
        session.rollback()
        raise _http(error) from error
    return MemoryEnvelope(success=True, data=MemoryOut(**item), code="OK", error=None,
                          user_message="个人记忆已纠正。", hint=None)


@router.get("/{memory_id}", response_model=MemoryEnvelope)
def detail_route(memory_id: str,
                 actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                 session: Annotated[Session, Depends(get_session)]) -> MemoryEnvelope:
    try:
        item = service.get_memory(session, actor, memory_id)
    except service.MemoryError as error:
        raise _http(error) from error
    return MemoryEnvelope(success=True, data=MemoryOut(**item), code="OK", error=None,
                          user_message="个人记忆已读取。", hint=None)


@router.delete("/{memory_id}", response_model=MemoryEnvelope)
def withdraw_route(memory_id: str,
                   actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                   session: Annotated[Session, Depends(get_session)],
                   expected_version: int,
                   idempotency_key: Annotated[str, Depends(require_idempotency_key)]) -> MemoryEnvelope:
    try:
        item = service.withdraw_memory(session, actor, memory_id,
                                       expected_version=expected_version,
                                       idempotency_key=idempotency_key)
        session.commit()
    except service.MemoryError as error:
        session.rollback()
        raise _http(error) from error
    return MemoryEnvelope(success=True, data=MemoryOut(**item), code="OK", error=None,
                          user_message="个人记忆已撤回。", hint=None)
