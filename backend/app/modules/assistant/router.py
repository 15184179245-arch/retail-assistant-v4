from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from backend.app.core.errors import ApiEnvelope
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.core.idempotency import require_idempotency_key
from backend.app.db.session import get_session
from backend.app.modules.assistant import service
from backend.app.modules.assistant.core_adapter import DshCoreAdapter
from backend.app.modules.assistant.lifecycle import available_core

router = APIRouter(prefix='/assistant/work', tags=['assistant'])


class WorkRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(min_length=1)
    request_id: str = Field(min_length=36, max_length=36)


def envelope(data, message):
    return ApiEnvelope(success=True, data=data, code='OK', error=None, user_message=message, hint=None)


@router.post('', response_model=ApiEnvelope, status_code=202)
def submit(payload: WorkRequest, request: Request,
           actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
           session: Annotated[Session, Depends(get_session)],
           key: Annotated[str, Depends(require_idempotency_key)]):
    keys = [v for k, v in request.scope['headers'] if k.lower() == b'idempotency-key']
    if keys != [payload.request_id.encode('utf-8')] or key != payload.request_id:
        raise HTTPException(400, 'assistant_idempotency_required')
    core = available_core(request.app, actor)
    if type(core) is not DshCoreAdapter:
        raise HTTPException(503, 'assistant_intake_unavailable')
    with session.begin():
        from backend.app.modules.assistant.lifecycle import intake_work_policy
        data = service.submit_work(session, actor, text=payload.text, request_id=payload.request_id,
            core=core, authority_policy=intake_work_policy(request.app, session, actor))
    return envelope(data, '工作已保存，等待执行。')


@router.get('', response_model=ApiEnvelope)
def list_route(actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
               session: Annotated[Session, Depends(get_session)], before_work_id: str | None = None):
    return envelope(service.list_work(session, actor, before_work_id=before_work_id), '工作列表已读取。')


@router.get('/intake-status', response_model=ApiEnvelope)
def intake_status(request: Request, actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                  session: Annotated[Session, Depends(get_session)]):
    service._authorize(session, actor, 'agents.run')
    service._authorize(session, actor, 'tasks.read_self')
    from backend.app.modules.assistant.lifecycle import (available_turn_core, available_inbox_policy,
        inbox_enabled, unavailable_reason)
    available=available_core(request.app,actor) is not None
    return envelope({'intake_available':available,'followups_supported':True,
        'followups_available':available_turn_core(request.app,actor) is not None,
        'inbox_supported':True,'inbox_enabled':inbox_enabled(request.app,actor),
        'inbox_available':available_inbox_policy(request.app,session,actor) is not None,
        'unavailable_reason':None if available else unavailable_reason(request.app,actor)},
        '工作入口状态已读取。')


@router.get('/by-request/{request_id}', response_model=ApiEnvelope)
def read_request_route(request_id: str, actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                       session: Annotated[Session, Depends(get_session)]):
    return envelope(service.read_request(session, actor, request_id), '已核对原工作请求。')


@router.get('/{work_id}', response_model=ApiEnvelope)
def read_route(work_id: str, actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
               session: Annotated[Session, Depends(get_session)]):
    return envelope(service.read_work(session, actor, work_id), '工作状态已读取。')


@router.post('/{work_id}/stop', response_model=ApiEnvelope)
def stop_route(work_id: str, actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
               session: Annotated[Session, Depends(get_session)]):
    from backend.app.modules.assistant.control import stop_work
    return envelope(stop_work(session, actor, work_id), '已核对停止结果。')


class FollowupRequest(WorkRequest):
    previous_turn_id: str = Field(min_length=36,max_length=36)


@router.post('/{work_id}/turns',response_model=ApiEnvelope,status_code=202)
def submit_followup(work_id: str,payload: FollowupRequest,request: Request,
                    actor: Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
                    session: Annotated[Session,Depends(get_session)],
                    key: Annotated[str,Depends(require_idempotency_key)]):
    from backend.app.modules.assistant import turns
    from backend.app.modules.assistant.lifecycle import available_turn_core,intake_work_policy
    keys=[v for k,v in request.scope['headers'] if k.lower()==b'idempotency-key']
    if keys!=[payload.request_id.encode('utf-8')] or key!=payload.request_id:
        raise HTTPException(400,'assistant_idempotency_required')
    core=available_turn_core(request.app,actor)
    if core is None:raise HTTPException(503,'assistant_followup_unavailable')
    with session.begin():
        policy=intake_work_policy(request.app,session,actor)
        if policy is None:raise HTTPException(503,'assistant_followup_unavailable')
        data=turns.append_followup(session,actor,root_work_id=work_id,previous_turn_id=payload.previous_turn_id,
            text=payload.text,request_id=payload.request_id,core=core,authority_policy=policy)
    return envelope(data,'补充已保存，等待继续执行。')


@router.get('/{work_id}/turns',response_model=ApiEnvelope)
def list_followups(work_id: str,actor: Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
                   session: Annotated[Session,Depends(get_session)]):
    from backend.app.modules.assistant import turns
    return envelope(turns.list_turns(session,actor,work_id),'事项跟进已读取。')


@router.get('/{work_id}/turns/by-request/{request_id}',response_model=ApiEnvelope)
def recover_followup(work_id: str,request_id: str,actor: Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
                     session: Annotated[Session,Depends(get_session)]):
    from backend.app.modules.assistant import turns
    return envelope(turns.read_request(session,actor,work_id,request_id),'已核对原补充请求。')


@router.get('/{work_id}/turns/{turn_id}',response_model=ApiEnvelope)
def read_followup(work_id: str,turn_id: str,actor: Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
                  session: Annotated[Session,Depends(get_session)]):
    from backend.app.modules.assistant import turns
    data=turns.read_turn(session,actor,turn_id)
    if data['root_work_id']!=work_id:raise HTTPException(404,'assistant_turn_not_found')
    return envelope(data,'跟进状态已读取。')


@router.post('/{work_id}/turns/{turn_id}/stop',response_model=ApiEnvelope)
def stop_followup(work_id: str,turn_id: str,actor: Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
                  session: Annotated[Session,Depends(get_session)]):
    from backend.app.modules.assistant.turn_execution import stop_turn
    return envelope(stop_turn(session,actor,work_id,turn_id),'已核对停止结果。')


class InboxRequest(WorkRequest):
    parent_id: str = Field(min_length=36,max_length=36)


@router.post('/{work_id}/inbox',response_model=ApiEnvelope,status_code=202)
def receive_message(work_id: str, payload: InboxRequest, request: Request,
                    actor: Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
                    session: Annotated[Session,Depends(get_session)],
                    key: Annotated[str,Depends(require_idempotency_key)]):
    from backend.app.modules.assistant import inbox
    from backend.app.modules.assistant.lifecycle import available_inbox_policy
    keys=[v for k,v in request.scope['headers'] if k.lower()==b'idempotency-key']
    if keys!=[payload.request_id.encode('utf-8')] or key!=payload.request_id:
        raise HTTPException(400,'assistant_idempotency_required')
    with session.begin():
        policy=available_inbox_policy(request.app,session,actor)
        if policy is None:raise HTTPException(503,'assistant_inbox_unavailable')
        value=inbox.receive(session,actor,root_work_id=work_id,parent_id=payload.parent_id,
            text=payload.text,request_id=payload.request_id,policy=policy)
    return envelope(value,'补充已保存。')


@router.get('/{work_id}/conversation',response_model=ApiEnvelope)
def conversation(work_id: str, actor: Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
                 session: Annotated[Session,Depends(get_session)]):
    from backend.app.modules.assistant.inbox import conversation_snapshot
    with session.begin():value=conversation_snapshot(session,actor,work_id)
    return envelope(value,'对话已读取。')


@router.get('/{work_id}/inbox/by-request/{request_id}',response_model=ApiEnvelope)
def recover_message(work_id: str, request_id: str,
                    actor: Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
                    session: Annotated[Session,Depends(get_session)]):
    from backend.app.modules.assistant.inbox import read_request
    return envelope(read_request(session,actor,work_id,request_id),'已核对原补充请求。')


@router.get('/{work_id}/inbox/{message_id}',response_model=ApiEnvelope)
def get_message(work_id: str, message_id: str,
                actor: Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
                session: Annotated[Session,Depends(get_session)]):
    from backend.app.modules.assistant.inbox import read_message
    return envelope(read_message(session,actor,work_id,message_id),'补充状态已读取。')


@router.post('/{work_id}/inbox/{message_id}/stop',response_model=ApiEnvelope)
def stop_message(work_id: str, message_id: str,
                 actor: Annotated[AuthenticatedSubject,Depends(require_authenticated_subject)],
                 session: Annotated[Session,Depends(get_session)]):
    from backend.app.modules.assistant import inbox
    # Scope is checked before any command; the service repeats owner checking
    # after its lock. A wrong path cannot cancel a same-owner unrelated receipt.
    inbox.read_message(session,actor,work_id,message_id)
    return envelope(inbox.stop_message(session,actor,message_id),'已核对补充停止结果。')
