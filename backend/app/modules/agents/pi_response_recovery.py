"""Host-only archived response accounting. No HTTP/Agent tool or execution path.

An authenticated recovery worker may account for a completed physical response
after a parser upgrade. The failed Run and unknown Step remain unchanged.
This module owns its transaction; any failure rolls back receipt, cost and audit.
"""
from sqlalchemy import select, update

from backend.app.application import commit_guard
from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents import cost_ledger, pi_probe_artifacts, repository
from backend.app.modules.agents.models import AgentPiProviderReceipt, AgentStep
from backend.app.modules.agents.pi_online_response import normalize_response
from backend.app.modules.audit.models import AuditEvent
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.machine_service import (
    authenticate_machine_token, authorize_machine_action,
)
from backend.app.workers.agent_guard import guard_recovery


class ResponseRecoveryError(RuntimeError):
    pass


def _deny():
    raise ResponseRecoveryError('pi_response_recovery_conflict')


def reconcile_archived_response(*, session_factory, machine_token, directory,
                                organization_id, store_id, run_id, model_step_id,
                                receipt_id, request_sha256, response_sha256):
    """Recover only an exact sent, hash-bound receipt under current authority.

    The original requester is billing attribution, not a fabricated login. All
    authority comes from guard_recovery and the final machine revalidation.
    Returned metadata contains no text, reasoning, credentials or tool inputs.
    """
    with session_factory() as session:
        ctx = guard_recovery(session, machine_token, organization_id=organization_id,
            store_id=store_id, run_id=run_id, expected_runtime=repository.SDK_RUNTIME)
        run = ctx.run
        receipt = session.scalar(select(AgentPiProviderReceipt).where(
            AgentPiProviderReceipt.organization_id == organization_id,
            AgentPiProviderReceipt.store_id == store_id,
            AgentPiProviderReceipt.run_id == run_id,
            AgentPiProviderReceipt.model_step_id == model_step_id,
            AgentPiProviderReceipt.id == receipt_id).with_for_update())
        step = session.scalar(select(AgentStep).where(
            AgentStep.organization_id == organization_id, AgentStep.store_id == store_id,
            AgentStep.run_id == run_id, AgentStep.id == model_step_id).with_for_update())
        if (receipt is None or step is None or run.state not in ('failed', 'cancelled', 'expired')
                or step.kind != 'model' or step.state != 'unknown'
                or receipt.send_started is not True or receipt.state not in ('unknown', 'responded')
                or receipt.request_digest != request_sha256 or receipt.response_digest != response_sha256):
            _deny()
        envelope = pi_probe_artifacts.read_response(directory=directory,
            organization_id=organization_id, store_id=store_id, run_id=run_id,
            model_step_id=model_step_id, request_sha256=request_sha256,
            response_sha256=response_sha256)
        source = receipt.wire_protocol == 'v4.pi-source/2'
        discovery = source or receipt.wire_protocol == 'v4.pi-discovery/2'
        memory = discovery or receipt.wire_protocol == 'v4.pi-memory/2'
        if discovery and envelope['mode'] != 'real-assistant-trial':
            _deny()
        if (envelope['mode'] in ('real-memory-trial', 'real-assistant-trial')) != memory:
            _deny()
        normalized = normalize_response(envelope['response_body'], model=receipt.model, memory=memory, discovery=discovery, source=source)
        if normalized['raw_sha256'] != response_sha256 or normalized['accounting_complete'] is not True:
            _deny()
        usage = normalized['usage']
        values = dict(state='responded', error_code=None,
            message_digest=normalized['message_sha256'], usage_presence=normalized['usage_presence'],
            usage_basis=normalized['usage_basis'], usage_input=usage['input'],
            usage_output=usage['output'], cache_read=usage['cacheRead'], cache_write=usage['cacheWrite'])
        key = 'pi-response-recovery:' + receipt.id
        prior = session.scalar(select(AuditEvent).where(AuditEvent.organization_id == organization_id,
            AuditEvent.store_id == store_id, AuditEvent.idempotency_key == key))
        details = dict(run_id=run_id, model_step_id=model_step_id, receipt_id=receipt.id,
            request_sha256=request_sha256, response_sha256=response_sha256,
            message_sha256=normalized['message_sha256'], accounting_only=True)
        if receipt.state == 'responded':
            if (prior is None or prior.action != 'agent_provider.archive_reconciled'
                    or prior.target_type != 'agent_pi_provider_receipt' or prior.target_id != receipt.id
                    or prior.details != details
                    or any(getattr(receipt, k) != v for k, v in values.items())):
                _deny()
        else:
            if prior is not None:
                _deny()
            changed = session.execute(update(AgentPiProviderReceipt).where(
                AgentPiProviderReceipt.id == receipt.id,
                AgentPiProviderReceipt.organization_id == organization_id,
                AgentPiProviderReceipt.store_id == store_id,
                AgentPiProviderReceipt.state == 'unknown',
                AgentPiProviderReceipt.send_started.is_(True),
                AgentPiProviderReceipt.request_digest == request_sha256,
                AgentPiProviderReceipt.response_digest == response_sha256,
            ).values(**values, updated_at=ctx.now).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                _deny()
            session.expire(receipt)
            record_event(session, actor_id=ctx.principal.subject_id, organization_id=organization_id,
                store_id=store_id, action='agent_provider.archive_reconciled',
                target_type='agent_pi_provider_receipt', target_id=receipt_id,
                idempotency_key=key, details=details, occurred_at=ctx.now)
        actor = AuthenticatedSubject(subject_id=run.requester_id, organization_id=organization_id,
                                    store_id=store_id, auth_expires_at=ctx.now)
        ledger = cost_ledger.reconcile_pi_provider_cost(session, actor, run, step,
            receipt_id=receipt_id, response_digest=response_sha256, now=ctx.now)

        def final_authority(active, now):
            principal = authenticate_machine_token(active, machine_token, now=now)
            if principal.machine_id != ctx.principal.machine_id or principal.version != ctx.principal.version:
                _deny()
            authorize_machine_action(active, principal, organization_id=organization_id,
                store_id=store_id, action='worker.agent.recover', now=now)

        commit_guard.enlist(session, action='pi_response_archive_recovery', check=final_authority)
        result = dict(receipt_id=receipt_id, ledger_id=ledger.id, status=ledger.status,
            estimated_amount=ledger.estimated_amount, run_state=run.state, step_state=step.state,
            response_sha256=response_sha256, replay=prior is not None)
        session.commit()
        return result
