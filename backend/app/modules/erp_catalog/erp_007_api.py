"""Person-only ERP catalog control. No caller-provided adapter, URL or catalog."""
from typing import Annotated
import os

from fastapi import APIRouter,Depends,Query,Request
from sqlalchemy.orm import sessionmaker

from backend.app.core.errors import ApiEnvelope
from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.security import AuthenticatedSubject,require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.erp_catalog import erp_007_service as service
from backend.app.modules.erp_catalog.erp_007_schemas import (
    Identifier,Digest,StrictDTO,CatalogProposeInput,CatalogDecideInput,CatalogStageInput,
    CatalogContextEnvelope,CatalogPreviewInput,CatalogCancelInput,CatalogCommandEnvelope,CatalogCommandStatusEnvelope,CatalogObservationPageEnvelope,
)

class EmptyCommandInput(StrictDTO):
    """An absent/empty body grants nothing; unknown fields are rejected."""


router=APIRouter(prefix='/erp/catalog-maintenance',tags=['ERP catalog sandbox'],
    responses={status:{'model':ApiEnvelope} for status in (400,401,403,404,409,422,500)})


def _response(value,envelope):
    return envelope(success=True,data=value,code='OK',error=None,
        user_message='合成商品维护命令记录已读取或保存；正式商品资料尚未核验。',hint=None)


def _run(db,function,actor,*args,envelope=CatalogCommandEnvelope,write=False,**kwargs):
    try:
        value=function(db,actor,*args,**kwargs)
        result=_response(value,envelope)
        if write:
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
        adapters=getattr(request.app.state,'erp_catalog_adapters',{})
        return adapters.get('api') if type(adapters) is dict else None


@router.post('/preview',response_model=CatalogContextEnvelope)
def preview_api(body:CatalogPreviewInput,actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.preview_catalog_command,actor,body,envelope=CatalogContextEnvelope)


@router.get('/commands/status',response_model=CatalogCommandStatusEnvelope)
def status_api(key:Annotated[str,Query(min_length=1,max_length=200)],proposal_digest:Digest,
               actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.command_status,actor,key,proposal_digest,envelope=CatalogCommandStatusEnvelope)


@router.post('',response_model=CatalogCommandEnvelope)
def propose_api(body:CatalogProposeInput,key:str=Depends(require_idempotency_key),
                actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.propose_catalog_command,actor,body,key,write=True)


@router.get('/{command_id}',response_model=CatalogCommandEnvelope)
def read_api(command_id:Identifier,actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.read_catalog_command,actor,command_id)


@router.post('/{command_id}/decide',response_model=CatalogCommandEnvelope)
def decide_api(command_id:Identifier,body:CatalogDecideInput,key:str=Depends(require_idempotency_key),
               actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.decide_catalog_command,actor,command_id,body,key,write=True)


@router.post('/{command_id}/stage',response_model=CatalogCommandEnvelope)
def stage_api(command_id:Identifier,body:CatalogStageInput,key:str=Depends(require_idempotency_key),
              actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.stage_catalog_command,actor,command_id,body,key,write=True)


@router.post('/{command_id}/dispatch',response_model=CatalogCommandEnvelope)
def dispatch_api(command_id:Identifier,request:Request,body:EmptyCommandInput|None=None,key:str=Depends(require_idempotency_key),
                 actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    factory=_factory(db)
    adapter=_host_adapter(factory,actor,command_id,request,'dispatch')
    try:
        return _response(service.dispatch_catalog_command(factory,actor,command_id,adapter,
            enabled=os.getenv('ERP_CATALOG_SANDBOX_ENABLED')=='1'),CatalogCommandEnvelope)
    except service.gateway.ERPCommandError as error:
        raise service.gateway._http(error) from None


@router.post('/{command_id}/reconcile',response_model=CatalogCommandEnvelope)
def reconcile_api(command_id:Identifier,request:Request,body:EmptyCommandInput|None=None,key:str=Depends(require_idempotency_key),
                  actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    factory=_factory(db)
    adapter=_host_adapter(factory,actor,command_id,request,'reconcile')
    try:
        return _response(service.reconcile_catalog_command(factory,actor,command_id,adapter),CatalogCommandEnvelope)
    except service.gateway.ERPCommandError as error:
        raise service.gateway._http(error) from None


@router.get('/{command_id}/observations',response_model=CatalogObservationPageEnvelope)
def observations_api(command_id:Identifier,limit:Annotated[int,Query(ge=1,le=100)]=50,
                     cursor:Annotated[str|None,Query(min_length=1,max_length=256)]=None,
                     actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.read_observations,actor,command_id,limit=limit,cursor=cursor,envelope=CatalogObservationPageEnvelope)


@router.post('/{command_id}/cancel',response_model=CatalogCommandEnvelope)
def cancel_api(command_id:Identifier,body:CatalogCancelInput,key:str=Depends(require_idempotency_key),
               actor:AuthenticatedSubject=Depends(require_authenticated_subject),db=Depends(get_session)):
    return _run(db,service.cancel_catalog_command,actor,command_id,body,key,write=True)
