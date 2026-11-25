"""Explicit, synthetic-only ERP-009 Host. No production input loader or network.

``python -m backend.app.workers.guanjia_precheck check|once --operation ...``
Without a protected in-process synthetic runtime both commands return HOLD.
CLI arguments never supply identities, credentials, source binding or a transport.
A future real Host loader/transport requires a separately approved increment.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import sys

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool, SingletonThreadPool

from backend.app.modules.agents import service as agent_service
from backend.app.modules.agents import tool_registry
from backend.app.modules.auth import service as auth_service
from backend.app.modules.identity.models import ScopeSecurityFence
from backend.app.modules.integrations.guanjia import read_service
from backend.app.modules.integrations.guanjia.client import PageError, RateLimited
from backend.app.modules.integrations.guanjia.read_schemas import ObservationInput, SourceResponse

_OPERATIONS = ("live_prices", "live_stock")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = ("raw_value", "unit", "price_type", "currency", "tax_basis", "stock_kind",
           "source_updated_at", "effective_until")


@dataclass(frozen=True, repr=False)
class SyntheticPermit:
    """Protected Host input, not a model/CLI DTO or a proof of real source truth.

    Evidence references belong to a controlled synthetic dataset. They do not
    verify real shop applicability and cannot lift HOLD_REAL_READ.
    """
    scope: read_service.SourceScope
    product: read_service.ConfirmedProduct
    agent_subject_id: str
    permission_epoch: int
    binding_evidence_sha256: str
    time_evidence_sha256: str
    price_evidence_sha256: str | None = None
    stock_evidence_sha256: str | None = None
    expires_at: datetime | None = None
    source_contract: str = "synthetic-v1"


@dataclass(frozen=True, repr=False)
class ProtectedSyntheticRuntime:
    """Only trusted in-process composition supplies an isolated synthetic DB.

    No function loads this object from JSON, environment, files or CLI flags.
    Production DBs and real credentials must never be passed here. SQL engines
    other than file SQLite and shared-connection pools are rejected before I/O.
    """
    engine: Engine
    employee_token: str = field(repr=False)
    permit: SyntheticPermit = field(repr=False)
    response: dict = field(repr=False)
    source_error: str | None = None


class _Denied(Exception):
    pass


class _InvalidArguments(Exception):
    pass


class _SyntheticTransport:
    observation_contract = "synthetic-v1"

    def __init__(self, scope, response, error):
        self.scope_identity = scope.tenant_fingerprint
        self._response = response
        self._error = error
        self.calls = 0

    def query(self, operation, params):
        if self.calls:
            raise _Denied()
        self.calls += 1
        if self._error == "timeout":
            raise TimeoutError()
        if self._error == "rate_limited":
            raise RateLimited()
        if self._error == "invalid_response":
            raise PageError()
        if self._error == "unknown":
            raise OSError()
        return deepcopy(self._response)


def _instant_valid(value):
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None and value > datetime.now(timezone.utc))


def _digest_valid(value):
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def binding_digest(permit):
    """Synthetic material integrity only, never a signature or live proof."""
    scope = permit.scope
    product = permit.product
    facts = [scope.organization_id, scope.store_id, scope.tenant_fingerprint,
             scope.shop_id, scope.warehouse_id, product.product_id,
             product.upstream_product_id, product.snapshot_id, product.snapshot_sha256,
             product.source_system, product.source_version, product.confirmed_by,
             product.expires_at.isoformat(), permit.agent_subject_id, permit.permission_epoch]
    return sha256(json.dumps(facts, separators=(",", ":")).encode()).hexdigest()


def _material_gate(runtime, operation):
    # Exact runtime/transport construction: no live or user-supplied transport.
    if type(runtime) is not ProtectedSyntheticRuntime or type(runtime.permit) is not SyntheticPermit:
        raise _Denied()
    engine, permit = runtime.engine, runtime.permit
    if (not isinstance(engine, Engine) or engine.dialect.name != "sqlite"
            or engine.url.database in (None, "", ":memory:")
            or isinstance(engine.pool, (StaticPool, SingletonThreadPool))):
        raise _Denied()
    if (permit.source_contract != "synthetic-v1" or not _instant_valid(permit.expires_at)
            or type(permit.scope) is not read_service.SourceScope
            or type(permit.product) is not read_service.ConfirmedProduct
            or type(permit.permission_epoch) is not int or permit.permission_epoch < 1
            or not _digest_valid(permit.binding_evidence_sha256)
            or not _digest_valid(permit.time_evidence_sha256)
            or permit.binding_evidence_sha256 != binding_digest(permit)):
        raise _Denied()
    evidence = permit.price_evidence_sha256 if operation == "live_prices" else permit.stock_evidence_sha256
    if (not _digest_valid(evidence) or type(runtime.employee_token) is not str
            or type(runtime.response) is not dict
            or runtime.source_error not in (None, "timeout", "rate_limited", "invalid_response", "unknown")):
        raise _Denied()
    # Copy/validate before opening a session. Floats and extra fields cannot
    # silently become normalized live facts. Wrong row scope is checked below.
    response = SourceResponse.model_validate(deepcopy(runtime.response))
    if response.operation != operation:
        raise _Denied()
    return response.model_dump()


def _identities(runtime):
    with Session(runtime.engine, autoflush=False, expire_on_commit=False) as db:
        requester = auth_service.authenticate_token(db, runtime.employee_token)
        agent = agent_service._agent_actor(db, requester, requester.auth_expires_at)
        if agent.subject_id != runtime.permit.agent_subject_id:
            raise _Denied()
        epoch = db.scalar(select(ScopeSecurityFence.version).where(
            ScopeSecurityFence.organization_id == requester.organization_id,
            ScopeSecurityFence.store_id == requester.store_id))
        if epoch != runtime.permit.permission_epoch:
            raise _Denied()
    return requester, agent


def _recheck(runtime, host, payload):
    # Re-authenticate the real sx1 and configured Agent, never reconstruct a
    # principal from the material's ID strings. All reads own their sessions.
    requester, agent = _identities(runtime)
    read_service._observation_scope(requester, host)
    with Session(runtime.engine, autoflush=False, expire_on_commit=False) as db:
        read_service._observation_clean_session(db)
        read_service._observation_authority(db, requester, agent, host, payload.operation)
        if not read_service._confirmed_mapping(db, requester, host, payload):
            raise _Denied()
    if not _instant_valid(runtime.permit.expires_at):
        raise _Denied()
    return requester, agent


def _commit_root(session):
    session.commit()


def _base(command, operation, *, code, calls=0, committed=False):
    return {"version": "1", "command": command, "operation": operation,
            "environment": "synthetic", "code": code,
            "authentication_network_calls": 0, "synthetic_business_calls": calls,
            "real_network_calls": 0, "root_commit_succeeded": committed,
            "disclosure_ready": False, "real_source_status": "HOLD_REAL_READ"}


def run(command, operation, *, runtime=None):
    """Run a single protected synthetic Host command; always return safe codes.

    Only this function owns its newly created audit root. It never borrows or
    commits another caller's session. Even a committed receipt is internal-only.
    """
    if type(runtime) is ProtectedStructureRuntime:
        return run_structure(command, operation, runtime=runtime)
    if command not in ("check", "once") or operation not in _OPERATIONS:
        return _base("invalid", "invalid", code="invalid_arguments")
    if runtime is None:
        return _base(command, operation, code="protected_runtime_required")
    transport = None
    committed = False
    try:
        response = _material_gate(runtime, operation)
        permit = runtime.permit
        transport = _SyntheticTransport(permit.scope, response, runtime.source_error)
        host = read_service.ObservationHost(transport=transport, scope=permit.scope,
            product=permit.product, permission_epoch=permit.permission_epoch,
            source_contract="synthetic-v1")
        payload = ObservationInput(version="1", operation=operation,
                                   product_id=permit.product.product_id)
        requester, agent = _recheck(runtime, host, payload)
        if command == "check":
            return _base(command, operation, code="checked_synthetic")
        with Session(runtime.engine, autoflush=False, expire_on_commit=False) as root:
            try:
                result = tool_registry.invoke_guanjia_observation(root, requester=requester,
                    agent=agent, tool_key=("guanjia.observe_prices" if operation == "live_prices"
                                         else "guanjia.observe_stock"), version="1",
                    arguments=payload.model_dump(), host=host)
            except tool_registry.ToolRegistryError as exc:
                if exc.code == "guanjia_audit_unavailable":
                    root.rollback()
                    return _base(command, operation, code="audit_failed", calls=transport.calls)
                # The old boundary may have staged a sanitized denial audit.
                # Commit only this owned root, never pretend an observation exists.
                try:
                    _commit_root(root)
                    committed = True
                except Exception:
                    root.rollback()
                    return _base(command, operation, code="audit_or_commit_failed", calls=transport.calls)
                return _base(command, operation, code="denied", calls=transport.calls, committed=committed)
            except Exception:
                root.rollback()
                return _base(command, operation, code="audit_or_execution_failed", calls=transport.calls)
            try:
                _commit_root(root)
                committed = True
            except Exception:
                root.rollback()
                return _base(command, operation, code="commit_failed", calls=transport.calls)
        _recheck(runtime, host, payload)
        receipt = _base(command, operation, code=("observed_synthetic" if result["usable"] else "source_unusable"), calls=transport.calls,
                        committed=True)
        # Fixed keys/types only; no ids, source values, arbitrary upstream text,
        # source_version strings, credentials, DSNs or raw exception content.
        row_status = result["status"] in ("usable", "stale", "missing_metadata", "missing_value")
        empty_status = result["status"] in ("empty_unclassified", "source_not_found", "index_miss", "permission_filtered")
        row_count = len(response["rows"]) if row_status else 0 if empty_status else None
        receipt.update(source_status=result["status"], row_count=row_count,
            field_types={name: ("null" if response["rows"][0][name] is None else
                               "integer" if type(response["rows"][0][name]) is int else "string")
                         for name in _FIELDS} if row_count == 1 else {},
            observed_started_at=result["observed_started_at"],
            observed_finished_at=result["observed_finished_at"], same_upstream_snapshot=False)
        return receipt
    except (_Denied, read_service.ObservationDenied, tool_registry.ToolRegistryError,
            agent_service.AgentRuntimeError, HTTPException):
        return _base(command, operation, code="denied", calls=transport.calls if transport else 0,
                     committed=committed)
    except (KeyboardInterrupt, SystemExit):
        return _base(command, operation, code="interrupted", calls=transport.calls if transport else 0,
                     committed=committed)
    except Exception:
        return _base(command, operation, code="invalid_material_or_storage", calls=transport.calls if transport else 0,
                     committed=committed)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _InvalidArguments()


def main(argv=None, *, runtime=None):
    parser = _Parser(prog="guanjia_precheck", description="Synthetic-only Host; real reads remain HOLD.")
    parser.add_argument("command", choices=("check", "once"))
    parser.add_argument("--operation", choices=_OPERATIONS, required=True)
    try:
        args = parser.parse_args(argv)
    except _InvalidArguments:
        print(json.dumps({"code": "invalid_arguments", "real_network_calls": 0}))
        return 2
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    result = run(args.command, args.operation, runtime=runtime)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["code"] in ("checked_synthetic", "observed_synthetic", "checked_structure", "structure_captured") else 6



# This sibling runtime is exclusively for fake-I/O structural prechecks. There
# is deliberately no real material loader, credential input or production path.
import os
from pathlib import Path
from backend.app.modules.integrations.guanjia.http_transport import (
    HttpTransport, SyntheticStructureOpener,
)


@dataclass(frozen=True, repr=False)
class ProtectedStructureRuntime:
    engine: Engine
    employee_token: str = field(repr=False)
    permit: SyntheticPermit = field(repr=False)
    opener: SyntheticStructureOpener = field(repr=False)
    ledger_directory: Path = field(repr=False)
    run_id: str
    source_version_evidence_sha256: str


class _RunAlreadyReserved(Exception):
    pass


class _SyntheticLedger:
    """Append-only temporary test ledger, NOT approved production storage.

    Reservation is O_EXCL-equivalent mkdir plus fsync of its parent. A crash at
    any later point permanently consumes this run ID. Events never carry raw
    data, credentials, source IDs, dynamic keys or exception messages.
    """
    def __init__(self, directory, run_id):
        self.directory = directory / run_id
        self.events = []
        self.reserved = False
        if directory.is_symlink() or not directory.is_dir() or directory.resolve() != directory:
            raise _Denied()

    @staticmethod
    def _sync_directory(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def reserve(self):
        try:
            self.directory.mkdir(mode=0o700)
        except FileExistsError:
            raise _RunAlreadyReserved() from None
        self.reserved = True
        self._sync_directory(self.directory.parent)
        self.record('run', 'reserved')

    def record(self, phase, kind):
        allowed = {'run': ('reserved', 'sealed'),
                   'authentication': ('send_intent', 'network_entered', 'returned', 'failed', 'entry_refused'),
                   'business': ('send_intent', 'network_entered', 'returned', 'failed', 'entry_refused')}
        if phase not in allowed or kind not in allowed[phase]:
            raise _Denied()
        event = {'phase': phase, 'event': kind}
        if (phase, kind) == ('run', 'reserved'):
            event.update(authentication_budget=1, business_budget=1, environment='synthetic',
                         run_sha256=sha256(self.directory.name.encode()).hexdigest())
        data = (json.dumps(event, sort_keys=True) + '\n').encode()
        path = self.directory / ('%03d.json' % len(self.events))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._sync_directory(self.directory)
        except BaseException:
            # A possibly-written record is retained. Never overwrite/retry it.
            raise
        self.events.append(event)

    def counts(self):
        result = {}
        for phase in ('authentication', 'business'):
            kinds = {event['event'] for event in self.events if event['phase'] == phase}
            # A generic failure says nothing about admission to the I/O
            # boundary. Only positive entry/return evidence raises the floor.
            lower = int(bool(kinds & {'network_entered', 'returned'}))
            upper = (lower if 'entry_refused' in kinds else
                     int(bool(kinds & {'send_intent', 'network_entered', 'returned', 'failed'})))
            result[phase] = {'lower': lower, 'upper': upper,
                             'state': 'exact' if lower == upper else 'unknown'}
        return result


def _structure_material_gate(runtime, operation):
    if type(runtime) is not ProtectedStructureRuntime or type(runtime.permit) is not SyntheticPermit:
        raise _Denied()
    engine, permit = runtime.engine, runtime.permit
    if (not isinstance(engine, Engine) or engine.dialect.name != 'sqlite'
            or engine.url.database in (None, '', ':memory:')
            or isinstance(engine.pool, (StaticPool, SingletonThreadPool))
            or type(runtime.employee_token) is not str
            or not isinstance(runtime.ledger_directory, Path)
            or not runtime.ledger_directory.is_absolute()
            or not _digest_valid(runtime.run_id)
            or not _digest_valid(runtime.source_version_evidence_sha256)
            or type(runtime.opener) is not SyntheticStructureOpener):
        raise _Denied()
    if (operation != 'live_stock' or permit.source_contract != 'synthetic-v1'
            or not _instant_valid(permit.expires_at)
            or type(permit.scope) is not read_service.SourceScope
            or type(permit.product) is not read_service.ConfirmedProduct
            or type(permit.permission_epoch) is not int or permit.permission_epoch < 1
            or not _digest_valid(permit.binding_evidence_sha256)
            or not _digest_valid(permit.time_evidence_sha256)
            or not _digest_valid(permit.stock_evidence_sha256)
            or permit.binding_evidence_sha256 != binding_digest(permit)):
        raise _Denied()


def _run_history_may_exist(runtime):
    """Check only the protected run marker, never disclose prior event content.

    Authorization/material failure does not erase a run's old effects. An
    inaccessible, malformed or uncertain ledger reference is not proof of zero.
    This check grants no permission to reserve, send, audit or disclose.
    """
    try:
        if (type(runtime) is not ProtectedStructureRuntime
                or not isinstance(runtime.ledger_directory, Path)
                or not runtime.ledger_directory.is_absolute()
                or not _digest_valid(runtime.run_id)
                or type(runtime.opener) is not SyntheticStructureOpener):
            return True
        if any(runtime.opener.calls.values()):
            return True
        directory = runtime.ledger_directory
        if (directory.is_symlink() or not directory.is_dir()
                or directory.resolve(strict=True) != directory):
            return True
        try:
            (directory / runtime.run_id).lstat()
        except FileNotFoundError:
            return not directory.is_dir()
        return True
    except Exception:
        return True


def _structure_receipt(command, operation, code, ledger=None, committed=False, *, runtime=None):
    zero = {'lower': 0, 'upper': 0, 'state': 'exact'}
    unknown = {'lower': 0, 'upper': 1, 'state': 'unknown'}
    own_reservation = ledger is not None and ledger.reserved
    attempt = ledger.counts() if own_reservation else {
        'authentication': dict(zero), 'business': dict(zero)}
    # Own exclusive reservation proves that this invocation is the first for
    # the run. Otherwise inspect its marker conservatively even after a denial.
    bounds = attempt if own_reservation or not _run_history_may_exist(runtime) else {
        'authentication': dict(unknown), 'business': dict(unknown)}
    return {'version': '1', 'command': command, 'operation': operation,
            'environment': 'synthetic', 'code': code, 'real_network_calls': 0,
            'call_bounds_scope': 'run', 'call_bounds': bounds,
            'attempt_call_bounds': attempt, 'root_commit_scope': 'current_attempt',
            'root_commit_succeeded': committed, 'disclosure_ready': False,
            'real_source_status': 'HOLD_REAL_READ'}


def run_structure(command, operation, *, runtime=None):
    """Single stock structure capture with a permanently consumed run budget."""
    ledger = None
    committed = False
    if command not in ('check', 'once') or operation not in _OPERATIONS:
        return _structure_receipt('invalid', 'invalid', 'invalid_arguments', runtime=runtime)
    try:
        _structure_material_gate(runtime, operation)
        permit = runtime.permit
        bound = read_service.ObservationHost(
            transport=_SyntheticTransport(permit.scope, {}, None), scope=permit.scope,
            product=permit.product, permission_epoch=permit.permission_epoch,
            source_contract='synthetic-v1')
        payload = ObservationInput(version='1', operation=operation, product_id=permit.product.product_id)
        def check():
            _structure_material_gate(runtime, operation)
            return _recheck(runtime, bound, payload)
        requester, agent = check()
        if command == 'check':
            return _structure_receipt(command, operation, 'checked_structure', runtime=runtime)
        ledger = _SyntheticLedger(runtime.ledger_directory, runtime.run_id)
        ledger.reserve()
        started = datetime.now(timezone.utc).isoformat()
        transport = HttpTransport(username='synthetic', password='synthetic', tenancy_name='synthetic')
        summary = transport.structure_once(operation,
            {'ProductIds': [permit.product.upstream_product_id],
             'shopId': permit.scope.shop_id, 'whsId': permit.scope.warehouse_id},
            opener=runtime.opener, event=ledger.record, recheck=check)
        finished = datetime.now(timezone.utc).isoformat()
        requester, agent = check()
        with Session(runtime.engine, autoflush=False, expire_on_commit=False) as root:
            try:
                read_service.audit_structure_precheck(root, requester, agent, bound, payload, summary, run_id=runtime.run_id)
                check()  # Audit I/O can race a revocation or epoch ABA.
                _commit_root(root)
                committed = True
            except BaseException:
                root.rollback()
                raise
        check()
        ledger.record('run', 'sealed')
        check()  # Last durable I/O is not an authorization lease.
        receipt = _structure_receipt(command, operation, 'structure_captured', ledger, committed, runtime=runtime)
        receipt.update(summary, observed_started_at=started, observed_finished_at=finished,
            source_version_evidence_sha256=runtime.source_version_evidence_sha256,
            same_upstream_snapshot=False)
        return receipt
    except _RunAlreadyReserved:
        # Do not return an exact zero for a run whose old I/O may be unknown.
        return _structure_receipt(command, operation, 'run_already_reserved',
                                  committed=committed, runtime=runtime)
    except (_Denied, read_service.ObservationDenied, tool_registry.ToolRegistryError,
            agent_service.AgentRuntimeError, HTTPException):
        return _structure_receipt(command, operation, 'denied', ledger, committed, runtime=runtime)
    except BaseException:
        # This boundary never formats or exposes external exception content.
        # Any failure after intent remains fail-closed and cannot retry this ID.
        return _structure_receipt(command, operation, 'structure_or_storage_failed', ledger, committed, runtime=runtime)


if __name__ == '__main__':
    raise SystemExit(main())
