"""Person-only ERP sales control. No caller-provided adapter, URL or receipt."""
from typing import Annotated
import os

from fastapi import APIRouter,Depends,Query,Request
from sqlalchemy.orm import sessionmaker

from backend.app.core.errors import ApiEnvelope
from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.security import AuthenticatedSubject,require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.erp_sales import erp_004_service as service
from backend.app.modules.erp_sales.erp_004_schemas import (
    Identifier,Digest,StrictDTO,SalesProposeInput,SalesDecideInput,SalesStageInput,
    SalesSourceEnvelope,SalesCommandEnvelope,SalesCommandStatusEnvelope,SalesObservationPageEnvelope,
)

class EmptyCommandInput(StrictDTO):
    """An absent/empty body grants nothing; unknown fields are rejected."""


router=APIRouter(prefix='/erp/sales',tags=['ERP sales sandbox'],
    responses={status:{'model':ApiEnvelope} for status in (400,401,403,404,409,422,500)})


def _response(value,envelope):
    return envelope(success=True,data=value,code='OK',error=None,
        user_message='合成销售命令记录已读取或保存；不代表正式开单或收款。',hint=None)


def _run(db,function,actor,*args,envelope=SalesCommandEnvelope,write=False,**kwargs):
    try:
        value=function(db,actor,*args,**kwargs)
        result=_response(value,envelope)
        if write:
            if function is service.propose_sales_command:
                from backend.app.application import commit_guard
                ident, new_effect = value['id'], not value.get('replayed', False)
                commit_guard.enlist(db, action='erp.sales.propose', check=lambda session, instant:
                    service.finalize_proposal(session, actor, ident, instant, new_effect=new_effect))
            db.commit()
        return result
    except service.gateway.ERPCommandError as error:
        db.rollback()
        raise service.gateway._http(error) from None
    except Exception:
        db.rollback()
        raise


def _factory(db):
    bind=db.get_bind()
    # Authentication may have opened a dependency read transaction. Never keep
    # its connection/fence around a separate durable prepare or HTTP exchange.
    db.rollback()
    return sessionmaker(bind=bind,autoflush=False,expire_on_commit=False)


def _host_adapter(factory,actor,ident,request,capability):
    with factory.begin() as db:
        service._guard(db,actor,capability)
        row,_,_=service._command(db,actor,ident)
        adapters=getattr(request.app.state,'erp_sales_adapters',{})
        return adapters.get(row.route) if type(adapters) is dict else None


@router.get('/source-context/{draft_id}',response_model=SalesSourceEnvelope)
def source_api(draft_id:Identifier,actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.read_source_context,actor,draft_id,envelope=SalesSourceEnvelope)


@router.get('/commands/status',response_model=SalesCommandStatusEnvelope)
def status_api(key:Annotated[str,Query(min_length=1,max_length=200)],proposal_digest:Digest,
               actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.command_status,actor,key,proposal_digest,envelope=SalesCommandStatusEnvelope)


@router.post('',response_model=SalesCommandEnvelope)
def propose_api(body:SalesProposeInput,key:str=Depends(require_idempotency_key),
                actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.propose_sales_command,actor,body,key,write=True)


@router.get('/{command_id}',response_model=SalesCommandEnvelope)
def read_api(command_id:Identifier,actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.read_sales_command,actor,command_id)


@router.post('/{command_id}/decide',response_model=SalesCommandEnvelope)
def decide_api(command_id:Identifier,body:SalesDecideInput,key:str=Depends(require_idempotency_key),
               actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.decide_sales_command,actor,command_id,body,key,write=True)


@router.post('/{command_id}/stage',response_model=SalesCommandEnvelope)
def stage_api(command_id:Identifier,body:SalesStageInput,key:str=Depends(require_idempotency_key),
              actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.stage_sales_command,actor,command_id,body,key,write=True)


@router.post('/{command_id}/dispatch',response_model=SalesCommandEnvelope)
def dispatch_api(command_id:Identifier,request:Request,body:EmptyCommandInput|None=None,key:str=Depends(require_idempotency_key),
                 actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    factory=_factory(db)
    adapter=_host_adapter(factory,actor,command_id,request,'dispatch')
    try:
        return _response(service.dispatch_sales_command(factory,actor,command_id,adapter,
            enabled=os.getenv('ERP_SALES_SANDBOX_ENABLED')=='1'),SalesCommandEnvelope)
    except service.gateway.ERPCommandError as error:
        raise service.gateway._http(error) from None


@router.post('/{command_id}/reconcile',response_model=SalesCommandEnvelope)
def reconcile_api(command_id:Identifier,request:Request,body:EmptyCommandInput|None=None,key:str=Depends(require_idempotency_key),
                  actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    factory=_factory(db)
    adapter=_host_adapter(factory,actor,command_id,request,'reconcile')
    try:
        return _response(service.reconcile_sales_command(factory,actor,command_id,adapter),SalesCommandEnvelope)
    except service.gateway.ERPCommandError as error:
        raise service.gateway._http(error) from None


@router.get('/{command_id}/observations',response_model=SalesObservationPageEnvelope)
def observations_api(command_id:Identifier,limit:Annotated[int,Query(ge=1,le=100)]=50,
                     cursor:Annotated[str|None,Query(min_length=1,max_length=256)]=None,
                     actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.read_observations,actor,command_id,limit=limit,cursor=cursor,envelope=SalesObservationPageEnvelope)
