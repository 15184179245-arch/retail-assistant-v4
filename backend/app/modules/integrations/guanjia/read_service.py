"""Business boundary for registered upstream reads. No public generic proxy/tool."""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from uuid import uuid4
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool, SingletonThreadPool
from sqlalchemy.orm import Session
from fastapi import HTTPException
from pydantic import ValidationError
from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot
from backend.app.modules.identity.models import ScopeSecurityFence
from backend.app.modules.integrations.guanjia.client import GuanJiaError, PageError, RateLimited
from backend.app.modules.integrations.guanjia.read_schemas import (
    ObservationInput, ObservationOutput, SourceResponse, aware_instant,
)
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.integrations.guanjia.api_catalog import read_endpoint
from backend.app.modules.integrations.guanjia.client import ReadOnlyViolation


@dataclass(frozen=True)
class SourceScope:
    """Trusted-host binding; never accepted from a browser or Agent argument."""
    organization_id: str
    store_id: str
    tenant_fingerprint: str
    shop_id: str | None = None
    warehouse_id: str | None = None


def query(session, actor, transport, *, action, params, binding: SourceScope, now=None):
    endpoint = read_endpoint(action, params)
    if endpoint.group == 'auth':
        raise ReadOnlyViolation('guanjia_auth_not_business_query')
    if (not isinstance(binding, SourceScope) or
            (binding.organization_id, binding.store_id) != (actor.organization_id, actor.store_id) or
            binding.tenant_fingerprint != transport.scope_identity):
        raise ReadOnlyViolation('guanjia_source_scope_denied')
    if any(key in params for key in ('tenantId', 'tenancyName', 'organizationId', 'password', 'accessToken')):
        raise ReadOnlyViolation('guanjia_context_override_denied')
    if action == 'live_stock' and (not binding.shop_id or not binding.warehouse_id or
            params['shopId'] != binding.shop_id or params['whsId'] != binding.warehouse_id):
        raise ReadOnlyViolation('guanjia_store_scope_denied')
    session.flush()
    session.expire_all()
    authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                    permission=f'guanjia.{endpoint.group}.read', now=now)
    if action != 'live_stock':
        # Most historical reports are tenant-wide; a store read grant alone
        # must not silently become company-wide financial/customer visibility.
        authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                        permission='guanjia.company.read', now=now)
    digest = sha256(json.dumps(params, sort_keys=True, ensure_ascii=False,
                              separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    result = transport.query(action, params)
    record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
        store_id=actor.store_id, action='guanjia.read', target_type='guanjia_endpoint',
        target_id=action, idempotency_key='guanjia.read:'+str(uuid4()),
        details={'operation': action, 'request_digest': digest}, occurred_at=now)
    return result


# ERP-009 is deliberately separate from query(): old caller and permission
# semantics stay intact. No model argument may supply any of these Host facts.


@dataclass(frozen=True)
class ConfirmedProduct:
    """An expiring employee confirmation, injected by trusted Host code only."""
    product_id: str
    upstream_product_id: str
    snapshot_id: str
    snapshot_sha256: str
    source_system: str
    source_version: str
    confirmed_by: str
    expires_at: datetime


@dataclass(frozen=True)
class ObservationHost:
    transport: object = field(repr=False)
    scope: SourceScope
    product: ConfirmedProduct
    permission_epoch: int
    # There is no verified live wire adapter yet. Anything else is HOLD, not a
    # permission to reinterpret live_prices fields or assume a tenant is a shop.
    source_contract: str = 'unverified-live'
    max_age_seconds: int = 60


class ObservationDenied(ReadOnlyViolation):
    """Only fixed, non-sensitive codes cross the observation boundary."""


class ObservationAuditUnavailable(ReadOnlyViolation):
    """Caller work is retained; no observation is disclosed without staging audit."""


def _observation_clean_session(session):
    # This MUST precede shared authorization helpers and audit lookups/flushes.
    # Do not erase staged caller work via expire_all, or commit/rollback it.
    if session.new or session.dirty or session.deleted:
        raise ObservationDenied('guanjia_caller_changes_pending')
    if not isinstance(session.get_bind(), Engine):
        raise ObservationDenied('guanjia_connection_bound_session_denied')
    if isinstance(session.get_bind().pool, (StaticPool, SingletonThreadPool)):
        # A fresh Session on these pools can reuse the caller's physical
        # connection; closing it could roll back the caller's transaction.
        raise ObservationDenied('guanjia_shared_connection_pool_denied')


def _observation_scope(actor, host):
    if not isinstance(host, ObservationHost) or not isinstance(host.scope, SourceScope):
        raise ObservationDenied('guanjia_host_binding_required')
    scope = host.scope
    labels = (scope.organization_id, scope.store_id, scope.tenant_fingerprint,
              scope.shop_id, scope.warehouse_id)
    if (any(type(v) is not str or not 1 <= len(v) <= 120 or
            any(ord(c) < 32 for c in v) for v in labels)
            or (scope.organization_id, scope.store_id) != (actor.organization_id, actor.store_id)
            or scope.tenant_fingerprint != getattr(host.transport, 'scope_identity', None)):
        raise ObservationDenied('guanjia_source_scope_denied')
    if (type(host.permission_epoch) is not int or host.permission_epoch < 1
            or type(host.max_age_seconds) is not int or not 1 <= host.max_age_seconds <= 86400):
        raise ObservationDenied('guanjia_host_binding_invalid')


def _observation_authority(session, requester, agent, host, operation):
    """Fresh owned read transaction; never refresh/commit/rollback the caller.

    A caller's transaction may have an old snapshot. Using its identity map for
    the post-transport check would not prove that a concurrent revoke was seen.
    The Engine is derived from the caller, not from model input or new config.
    """
    from backend.app.modules.agents.tool_registry import _person_scope, _agent_scope
    permission = 'guanjia.pricing.read' if operation == 'live_prices' else 'guanjia.inventory.read'
    agent_permission = 'agents.guanjia.prices.read' if operation == 'live_prices' else 'agents.guanjia.stock.read'
    with Session(bind=session.get_bind(), autoflush=False, expire_on_commit=False) as check:
        instant = datetime.now(timezone.utc)
        _person_scope(check, requester, now=instant)
        _agent_scope(check, requester, agent, agent_permission, now=instant)
        authorize_scope(check, requester, organization_id=requester.organization_id,
                        store_id=requester.store_id, permission=permission, now=instant)
        if operation == 'live_prices':
            # Preserve the old tenant-wide pricing permission requirement.
            # A future verified per-shop wire contract needs its own approval.
            authorize_scope(check, requester, organization_id=requester.organization_id,
                            store_id=requester.store_id, permission='guanjia.company.read', now=instant)
        epoch = check.scalar(select(ScopeSecurityFence.version).where(
            ScopeSecurityFence.organization_id == requester.organization_id,
            ScopeSecurityFence.store_id == requester.store_id))
        if epoch != host.permission_epoch:
            raise ObservationDenied('guanjia_permission_version_changed')


def _confirmed_mapping(session, requester, host, payload):
    product = host.product
    instant = datetime.now(timezone.utc)
    if (not isinstance(product, ConfirmedProduct) or product.product_id != payload.product_id
            or product.confirmed_by != requester.subject_id
            or not isinstance(product.expires_at, datetime) or product.expires_at.tzinfo is None
            or product.expires_at.utcoffset() is None or product.expires_at <= instant
            or type(product.upstream_product_id) is not str or not product.upstream_product_id):
        return False
    with Session(bind=session.get_bind(), autoflush=False) as check:
        row = check.scalar(select(CatalogProduct).where(
            CatalogProduct.id == payload.product_id,
            CatalogProduct.organization_id == requester.organization_id,
            CatalogProduct.store_id == requester.store_id,
            CatalogProduct.active.is_(True)))
        if (row is None or row.current_snapshot_id != product.snapshot_id
                or row.source_system != product.source_system or row.source_version != product.source_version):
            return False
        snapshot = check.scalar(select(CatalogSnapshot).where(
            CatalogSnapshot.id == product.snapshot_id,
            CatalogSnapshot.product_id == row.id,
            CatalogSnapshot.organization_id == row.organization_id,
            CatalogSnapshot.store_id == row.store_id))
        if (snapshot is None or snapshot.payload_sha256 != product.snapshot_sha256
                or snapshot.source_version != product.source_version):
            return False
        # Catalog stores the upstream object under raw. Never equate its local
        # UUID, barcode or external code with an upstream product ID.
        raw = snapshot.payload.get('raw')
        return isinstance(raw, dict) and type(raw.get('id')) is str and raw['id'] == product.upstream_product_id


def _observation_audit(session, requester, agent, payload, host, status, result=None):
    _observation_clean_session(session)
    content = {'operation': payload.operation, 'product_id': payload.product_id,
               'scope': [host.scope.organization_id, host.scope.store_id,
                         host.scope.tenant_fingerprint, host.scope.shop_id, host.scope.warehouse_id],
               'requester': requester.subject_id, 'agent': agent.subject_id,
               'permission_epoch': host.permission_epoch}
    digest = sha256(json.dumps(content, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    summary = ({key: getattr(result, key) for key in ('source_contract', 'source_version',
                'source_updated_at', 'observed_started_at', 'observed_finished_at', 'status')}
               if result is not None else {'status': status})
    summary_digest = sha256(json.dumps(summary, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    # Keep audit failure inside a savepoint so a failed audit flush cannot
    # roll back unrelated work the caller already flushed. SQLite legacy
    # transaction mode needs a real BEGIN before SAVEPOINT: otherwise RELEASE
    # could commit the audit independently of the caller's later rollback.
    connection = session.connection()
    if connection.dialect.name == 'sqlite':
        driver = connection.connection.driver_connection
        if not driver.in_transaction:
            connection.exec_driver_sql('BEGIN')
    try:
        with session.begin_nested():
            record_event(session, actor_id=requester.subject_id, organization_id=requester.organization_id,
                         store_id=requester.store_id, action='guanjia.observation',
                         target_type='guanjia_endpoint', target_id=payload.operation,
                         idempotency_key='guanjia.observation:' + str(uuid4()),
                         details={'operation': payload.operation, 'version': payload.version,
                                  'request_digest': digest, 'source_summary_digest': summary_digest, 'status': status,
                                  'stage': 'observed_before_disclosure',
                                  'agent_id': agent.subject_id, 'permission_epoch': host.permission_epoch,
                                  'source_contract': 'synthetic-v1' if host.source_contract == 'synthetic-v1' else 'unverified-live'})
    except Exception:
        raise ObservationAuditUnavailable('guanjia_audit_unavailable') from None


def _observation_result(payload, host, start, finish, status, row=None, source_version=None):
    fields = ('raw_value', 'unit', 'price_type', 'currency', 'tax_basis', 'stock_kind',
              'source_updated_at', 'effective_until')
    return ObservationOutput(operation=payload.operation, product_id=payload.product_id,
        organization_id=host.scope.organization_id, store_id=host.scope.store_id,
        tenant_fingerprint=host.scope.tenant_fingerprint, shop_id=host.scope.shop_id,
        warehouse_id=host.scope.warehouse_id,
        source_contract='synthetic-v1' if host.source_contract == 'synthetic-v1' else 'unverified-live',
        status=status, usable=status == 'usable', source_version=source_version,
        observed_started_at=start.isoformat(), observed_finished_at=finish.isoformat(),
        **{key: getattr(row, key) if row is not None else None for key in fields})


def _interpret_observation(raw, payload, host, finish):
    try:
        response = SourceResponse.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        return 'invalid_response', None, None
    if response.operation != payload.operation or not response.complete:
        return 'invalid_response', None, None
    if not response.rows:
        if response.empty_reason is not None and response.reason_evidence_sha256 is not None:
            return response.empty_reason, None, response.source_version
        return 'empty_unclassified', None, response.source_version
    if response.empty_reason is not None or response.reason_evidence_sha256 is not None:
        return 'invalid_response', None, None
    row = response.rows[0]
    if (row.upstream_product_id, row.shop_id, row.warehouse_id) != (
            host.product.upstream_product_id, host.scope.shop_id, host.scope.warehouse_id):
        return 'invalid_response', None, None
    required = (row.unit, row.source_updated_at)
    if payload.operation == 'live_prices':
        required += (row.price_type, row.currency, row.tax_basis, row.effective_until)
    else:
        required += (row.stock_kind,)
    if any(v is None for v in required):
        return 'missing_metadata', row, response.source_version
    source_time = aware_instant(row.source_updated_at)
    if source_time > finish:
        return 'invalid_response', None, None
    if ((finish - source_time).total_seconds() > host.max_age_seconds
            or row.effective_until is not None and aware_instant(row.effective_until) <= finish):
        return 'stale', row, response.source_version
    if row.raw_value is None:
        return 'missing_value', row, response.source_version
    return 'usable', row, response.source_version


def observe(session, requester, agent, *, payload: ObservationInput, host: ObservationHost):
    """One independently authorized observation. Caller owns audit durability.

    The returned value is Host-internal and explicitly NOT disclosure-ready.
    Its audit is only staged; the caller must commit successfully and a later
    approved public adapter must revalidate before disclosure. Commit failure
    or rollback must discard the provisional value. This is not a receipt.

    No retries, root commit/rollback, business write, or live wire inference. A
    staged caller write is rejected before auth/audit. A rejected post-flight
    check returns no raw values, including when the transport also failed.
    """
    _observation_clean_session(session)
    if not isinstance(payload, ObservationInput):
        raise ObservationDenied('guanjia_input_invalid')
    _observation_scope(requester, host)
    from backend.app.modules.agents.tool_registry import ToolRegistryError
    start = datetime.now(timezone.utc)
    try:
        _observation_authority(session, requester, agent, host, payload.operation)
        row = None
        source_version = None
        captured_at = None
        if not _confirmed_mapping(session, requester, host, payload):
            status = 'mapping_unconfirmed'
        elif (host.source_contract != 'synthetic-v1'
              or getattr(host.transport, 'observation_contract', None) != 'synthetic-v1'):
            status = 'hold_real_read'
        else:
            params = {'ProductIds': [host.product.upstream_product_id]}
            if payload.operation == 'live_prices':
                params['billModule'] = 2
            else:
                params.update(shopId=host.scope.shop_id, whsId=host.scope.warehouse_id)
            read_endpoint(payload.operation, params)
            try:
                raw = host.transport.query(payload.operation, params)
                response_at = datetime.now(timezone.utc)
                status, row, source_version = _interpret_observation(raw, payload, host, response_at)
                # Report the source capture time used for freshness, not the later recheck time.
                captured_at = response_at
            except RateLimited:
                status = 'rate_limited'
            except TimeoutError:
                status = 'timeout'
            except PageError:
                status = 'invalid_response'
            except (GuanJiaError, OSError):
                status = 'upstream_unavailable'
            except Exception:
                # Transport exception text may contain credentials or a full
                # upstream response. It must never become an audit/error field.
                status = 'upstream_unavailable'
        _observation_clean_session(session)
        _observation_scope(requester, host)
        _observation_authority(session, requester, agent, host, payload.operation)
        if status not in ('mapping_unconfirmed', 'hold_real_read') and not _confirmed_mapping(session, requester, host, payload):
            status, row, source_version = 'mapping_unconfirmed', None, None
            captured_at = None
        result = _observation_result(payload, host, start, captured_at or datetime.now(timezone.utc),
                                     status, row, source_version)
        _observation_audit(session, requester, agent, payload, host, status, result)
        # Audit append may block on the DB; recheck after that last I/O before
        # disclosure. A denied call may have an observed-source audit, never an
        # assertion that the user received it. No success result escapes.
        _observation_authority(session, requester, agent, host, payload.operation)
        return result
    except (ObservationDenied, ToolRegistryError, HTTPException):
        # A fixed refusal code; neither auth internals nor upstream text escape.
        # Never audit a dirty caller, since record_event intentionally flushes.
        _observation_clean_session(session)
        _observation_audit(session, requester, agent, payload, host, 'authorization_denied')
        raise ObservationDenied('guanjia_observation_denied') from None


def audit_structure_precheck(session, requester, agent, host, payload, summary, *, run_id):
    """Independent structural audit; no live or normalized observation claim."""
    _observation_clean_session(session)
    _observation_scope(requester, host)
    _observation_authority(session, requester, agent, host, payload.operation)
    if not _confirmed_mapping(session, requester, host, payload):
        raise ObservationDenied('guanjia_mapping_unconfirmed')
    # Keep payloads/lexemes/field keys outside the audit, including unknown
    # upstream keys. The digest covers the already-whitelisted safe summary.
    if (type(run_id) is not str or len(run_id) != 64
            or any(character not in '0123456789abcdef' for character in run_id)):
        raise ObservationDenied('guanjia_run_binding_required')
    run_digest = sha256(run_id.encode()).hexdigest()
    request = {'operation': payload.operation, 'product_id': payload.product_id,
               'scope': [host.scope.organization_id, host.scope.store_id,
                         host.scope.tenant_fingerprint, host.scope.shop_id, host.scope.warehouse_id],
               'requester': requester.subject_id, 'agent': agent.subject_id,
               'permission_epoch': host.permission_epoch, 'run_sha256': run_digest}
    request_digest = sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    digest = sha256(json.dumps(summary, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    record_event(session, actor_id=requester.subject_id,
        organization_id=requester.organization_id, store_id=requester.store_id,
        action='guanjia.structure_precheck', target_type='guanjia_endpoint',
        target_id=payload.operation, idempotency_key='guanjia.structure_precheck:' + run_digest,
        details={'operation': payload.operation, 'summary_digest': digest,
                 'agent_id': agent.subject_id, 'permission_epoch': host.permission_epoch,
                 'request_digest': request_digest, 'run_sha256': run_digest,
                 'status': 'semantics_unknown', 'disclosure_ready': False})
