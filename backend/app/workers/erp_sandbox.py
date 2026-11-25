"""Explicit human-scoped Outbox handler; no default scheduler or machine grant.

The generic runner owns the lease. A handler receipt confirms durable control
processing, never ERP success. Redelivery encounters the already committed
dispatch fact and cannot issue another POST.
"""
from sqlalchemy import select

from backend.app.modules.erp_commands import erp_003_service as service
from backend.app.modules.erp_commands.erp_003_wire import canonical_digest
from backend.app.workers.outbox import OutboxEvent


def make_sandbox_handler(*, session_factory, actor, adapter=None, enabled=False):
    """Construct only on a trusted host with an actual current employee actor."""
    def handle(message):
        with session_factory.begin() as db:
            service._guard(db, actor, "erp.sandbox.dispatch")
            if (message.organization_id != actor.organization_id or message.store_id != actor.store_id
                    or message.event_type != service.EVENT_TYPE or not isinstance(message.payload, dict)):
                service._fail("erp_sandbox_event_invalid")
            ident = message.payload.get("batch_id")
            if not isinstance(ident, str):
                service._fail("erp_sandbox_event_invalid")
            batch = service._batch(db, actor, ident)
            expected = service.event_payload(batch)
            event = db.scalar(select(OutboxEvent).where(OutboxEvent.id == message.id,
                *service._scope(OutboxEvent, actor)))
            if (event is None or event.event_type != service.EVENT_TYPE
                    or event.idempotency_key != "erp-sandbox:" + batch.id
                    or message.idempotency_key != event.idempotency_key
                    or canonical_digest(message.payload) != canonical_digest(expected)
                    or canonical_digest(event.payload) != canonical_digest(expected)):
                service._fail("erp_sandbox_event_invalid")
        service.dispatch_execution(session_factory, actor, ident, adapter, enabled=enabled)
        return "synthetic-control-recorded:" + ident
    return handle
