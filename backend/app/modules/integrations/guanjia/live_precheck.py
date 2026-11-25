"""Offline L2b composition. No CLI, real loader, credentials or network adapter.

Construct only in a protected Host with an isolated synthetic database. Runtime
objects are configuration, not signed authority: every use revalidates the real
employee session, Agent, confirmed mapping and security fence via the old Host.
Reservations survive rollback. Never remove a nonce to retry.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.workers import guanjia_precheck as host
from backend.app.modules.integrations.guanjia.live_call_ledger import (
    HostPermitBinding, LiveCallLedger,
)
from backend.app.modules.integrations.guanjia.live_structure import inspect_response


@dataclass(repr=False)
class SyntheticBoundary:
    """Inert anonymous fixture, deliberately not a transport protocol."""
    response: bytes = field(repr=False)
    failure: str | None = None
    calls: list = field(default_factory=list, init=False, repr=False)

    def send(self, operation):
        if operation in self.calls:
            raise ValueError('synthetic_boundary_consumed')
        self.calls.append(operation)
        if self.failure == operation:
            raise TimeoutError()
        return b'{"authenticated":true}' if operation == 'authenticate' else self.response


@dataclass(frozen=True, repr=False)
class ProtectedLiveRuntime:
    material: host.ProtectedSyntheticRuntime = field(repr=False)
    ledger_directory: Path = field(repr=False)
    nonce_sha256: str
    boundary: SyntheticBoundary = field(repr=False)


def _binding(runtime, operation):
    permit = runtime.material.permit
    scope = permit.scope
    scope_digest = sha256(json.dumps([scope.organization_id, scope.store_id,
        scope.tenant_fingerprint, scope.shop_id, scope.warehouse_id],
        separators=(',', ':')).encode()).hexdigest()
    return HostPermitBinding(scope_digest, permit.binding_evidence_sha256,
        runtime.nonce_sha256, permit.permission_epoch, permit.expires_at,
        frozenset({'authenticate', operation}))


def _receipt(command, operation, code, counts, committed=False):
    return dict(version='1', command=command, operation=operation,
        environment='synthetic', code=code, call_bounds={k: list(v) for k, v in counts.items()},
        call_bounds_scope='permit', real_network_calls=0,
        root_commit_succeeded=committed, disclosure_ready=False,
        real_source_status='HOLD_REAL_READ')


def run(command, operation, *, runtime=None):
    """One authentication plus one authorized action; no failure is retried."""
    operations = ('authenticate', 'live_prices', 'live_stock')
    counts = {key: (0, 0) for key in operations}
    ledger = binding = None
    committed = False
    entry_attempted = set()
    if command not in ('check', 'once') or operation not in operations[1:]:
        return _receipt('invalid', 'invalid', 'invalid_arguments', counts)
    try:
        if (type(runtime) is not ProtectedLiveRuntime
                or type(runtime.material) is not host.ProtectedSyntheticRuntime
                or type(runtime.boundary) is not SyntheticBoundary
                or type(runtime.boundary.response) is not bytes
                or runtime.boundary.failure not in (None, *operations)
                or not isinstance(runtime.ledger_directory, Path)):
            raise ValueError()
        # Resolve historical consumption before validating mutable material.
        # Invalid material is not evidence that a prior permit sent nothing.
        if host._digest_valid(runtime.nonce_sha256):
            counts = {key: (0, 1) for key in operations}
            try:
                (runtime.ledger_directory / runtime.nonce_sha256).lstat()
            except FileNotFoundError:
                counts = {key: (0, 0) for key in operations}
        else:
            raise ValueError()
        # This gate checks anonymous same-store material, evidence and isolated
        # SQLite. It never grants identity based on the permit's ID strings.
        binding = _binding(runtime, operation)
        binding.digest()
        # Preserve historical consumption even when this attempt is revoked.
        marker = runtime.ledger_directory / binding.nonce_sha256
        try:
            marker.lstat()
        except FileNotFoundError:
            pass
        else:
            counts = LiveCallLedger.inspect(runtime.ledger_directory, binding)
        host._material_gate(runtime.material, operation)
        permit = runtime.material.permit
        bound = host.read_service.ObservationHost(
            transport=host._SyntheticTransport(permit.scope, {}, None),
            scope=permit.scope, product=permit.product,
            permission_epoch=permit.permission_epoch, source_contract='synthetic-v1')
        payload = host.ObservationInput(version='1', operation=operation,
                                       product_id=permit.product.product_id)

        def check():
            host._material_gate(runtime.material, operation)
            return host._recheck(runtime.material, bound, payload)

        check()
        if command == 'check':
            return _receipt(command, operation, 'checked_synthetic', counts)
        ledger = LiveCallLedger.reserve(runtime.ledger_directory, binding)
        counts = {key: (0, 0) for key in operations}
        for action in ('authenticate', operation):
            check()
            ledger.record(action, 'send_intent')
            check()
            # Persist BEFORE calling the boundary, unlike old simulated events.
            entry_attempted.add(action)
            ledger.record(action, 'network_entered')
            counts[action] = (0, 1)
            check()  # fsync is not an authorization lease.
            raw = runtime.boundary.send(action)
            check()
            ledger.record(action, 'returned', response_sha256=sha256(raw).hexdigest())
            counts[action] = (1, 1)
        summary = inspect_response(operation, raw)
        if summary['response_sha256'] != sha256(raw).hexdigest():
            raise ValueError()
        requester, agent = check()
        with Session(runtime.material.engine, autoflush=False, expire_on_commit=False) as root:
            try:
                host.read_service.audit_structure_precheck(root, requester, agent,
                    bound, payload, summary, run_id=binding.nonce_sha256)
                check()
                host._commit_root(root)
                committed = True
            except BaseException:
                root.rollback()
                raise
        check()
        result = _receipt(command, operation, 'structure_captured', counts, committed)
        result.update(summary)
        return result
    except BaseException:
        # Keep all partial records. No dynamic upstream/DB exception is exposed.
        if ledger is not None:
            try:
                durable = LiveCallLedger.inspect(runtime.ledger_directory, binding)
                for action in entry_attempted:
                    # record() may persist and then fail before returning. Local
                    # assignments cannot erase that durable or uncertain entry.
                    counts[action] = (max(counts[action][0], durable[action][0]),
                                      max(1, counts[action][1], durable[action][1]))
            except BaseException:
                counts = {key: (0, 1) for key in operations}
        elif binding is not None:
            try:
                # Reservation may have partially persisted, or a prior caller
                # consumed this nonce. An unreadable marker never proves zero.
                (runtime.ledger_directory / binding.nonce_sha256).lstat()
                counts = LiveCallLedger.inspect(runtime.ledger_directory, binding)
            except FileNotFoundError:
                pass
            except BaseException:
                counts = {key: (0, 1) for key in operations}
        return _receipt(command, operation, 'denied_or_failed', counts, committed)
    finally:
        if ledger is not None:
            try:
                ledger.close()
            except OSError:
                pass
