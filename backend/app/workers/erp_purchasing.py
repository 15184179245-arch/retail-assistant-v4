"""Bounded host-only ERP receiving control; existing Outbox owns leases and ACKs."""
from sqlalchemy import select

from backend.app.modules.erp_purchasing import erp_005_service as service
from backend.app.modules.erp_purchasing.erp_005_models import ERPReceiptAttempt
from backend.app.workers.outbox import OutboxEvent


def make_receipt_handler(*,session_factory,actor,adapter=None,enabled=False):
    """Only trusted host code may supply the actual current sx1 actor/adapter."""
    def handle(message):
        with session_factory.begin() as db:
            service._guard(db,actor,'dispatch')
            if (message.organization_id,message.store_id,message.event_type)!=(
                    actor.organization_id,actor.store_id,service.EVENT_TYPE+'.'+actor.subject_id):
                service.source.fail('event_invalid')
            if type(message.payload) is not dict or set(message.payload)!={'attempt_id','input_digest'}:
                service.source.fail('event_invalid')
            ident=message.payload['attempt_id']
            if type(ident) is not str:
                service.source.fail('event_invalid')
            attempt=db.scalar(select(ERPReceiptAttempt).where(*service.source.scope(ERPReceiptAttempt,actor),
                ERPReceiptAttempt.id==ident,ERPReceiptAttempt.executor_id==actor.subject_id))
            if attempt is None:
                service.source.fail('event_invalid')
            expected=dict(attempt_id=attempt.id,input_digest=attempt.input_digest)
            event=db.scalar(select(OutboxEvent).where(*service.source.scope(OutboxEvent,actor),OutboxEvent.id==message.id))
            if (event is None or event.event_type!=message.event_type or event.payload!=expected
                    or message.payload!=expected or event.idempotency_key!='erp5:'+ident
                    or message.idempotency_key!=event.idempotency_key):
                service.source.fail('event_invalid')
            command_id=attempt.command_id
        service.dispatch_receipt_command(session_factory,actor,command_id,adapter,enabled=enabled)
        return 'synthetic-receipt-control:'+ident
    return handle


def run_receipt_once(*,session_factory,actor,adapter=None,enabled=False,worker_id='erp-receiving',lease_seconds=30):
    if type(enabled) is not bool:
        service.source.fail('input_invalid')
    if not enabled:
        return 'disabled'
    from backend.app.workers.runner import run_once
    with session_factory.begin() as db:
        service._guard(db,actor,'dispatch')
        service._configured(adapter,actor,'api')
    event_type=service.EVENT_TYPE+'.'+actor.subject_id
    return run_once(session_factory=session_factory,handlers={event_type:make_receipt_handler(
        session_factory=session_factory,actor=actor,adapter=adapter,enabled=True)},worker_id=worker_id,
        lease_seconds=lease_seconds,event_types=(event_type,),organization_id=actor.organization_id,store_id=actor.store_id)
