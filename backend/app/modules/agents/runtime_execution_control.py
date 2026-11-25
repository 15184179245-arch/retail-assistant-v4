"""SQLite Host execution control; a phase owns connections, never business truth.

The application keeps its own Session factory, guards and transactions. Only
heartbeat uses the separate NullPool engine. A phase spans Session close so an
owner's SQLite writer cannot race its own heartbeat. Long work still needs the
separate materialize/compute/current-authority result contract.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import math
from pathlib import Path
import sqlite3
import threading
import time

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from backend.app.modules.agents import repository
from backend.app.workers.agent_guard import guard_execution

SAFETY_MARGIN = 2.0
COMMIT_MARGIN = 5.0
MAX_DECLARED_SYNC_SECONDS = 15.0
_LABELS = frozenset(('start_payload', 'model_begin', 'model_complete',
    'provider_send_intent', 'tool_dispatch_intent', 'source_preflight',
    'tool_result', 'observation_readback'))


class ExecutionControlError(RuntimeError):
    pass


class _Busy(Exception):
    def __init__(self, code):
        self.code = code


@dataclass(frozen=True, repr=False)
class ControlHandle:
    """Host-only capability. Settlement never grants execution authority."""
    stop_event: threading.Event
    deadline: float
    finished_event: threading.Event
    _coordinator: object = field(default=None, repr=False, compare=False)

    def check(self):
        if self._coordinator is not None:
            with self._coordinator._condition:
                self._coordinator._check()
        elif (self.stop_event.is_set() or self.finished_event.is_set()
                or time.monotonic() >= self.deadline):
            raise ExecutionControlError('pi_execution_control_stopped')

    def _phase(self, kind, label=None, required_seconds=0):
        if self._coordinator is None:
            raise ExecutionControlError('pi_execution_control_phase_unsupported')
        return self._coordinator._transaction(kind, label, required_seconds)

    def execute_transaction(self, label, *, required_seconds=0):
        return self._phase('execute', label, required_seconds)

    def accounting_transaction(self):
        return self._phase('accounting')

    def recovery_transaction(self):
        return self._phase('recovery')


class _Phase:
    def __init__(self, control, kind, label=None):
        self.control, self.kind, self.label = control, kind, label
        self.thread = threading.get_ident()
        self.closed = False
        self.committed = False

    def commit(self, session, *, terminal=False):
        c = self.control
        with c._condition:
            if (self.closed or self.committed or c._committing_thread is not None or c._phase is not self
                    or self.thread != threading.get_ident()
                    or session.get_bind() is not c._application_engine):
                raise ExecutionControlError('pi_execution_control_phase_foreign')
            if type(terminal) is not bool or (terminal and self.label != 'model_complete'):
                raise ExecutionControlError('pi_execution_control_terminal_invalid')
            if self.kind == 'execute':
                c._check()
                if min(c._safe_until, c.handle.deadline)-time.monotonic() < COMMIT_MARGIN:
                    raise ExecutionControlError('pi_execution_control_commit_margin')
            # Accounting/recovery retains its original guard_recovery. A stop
            # cannot erase known usage; neither phase renews or grants a permit.
            c._committing_thread = threading.get_ident()
            try:
                session.commit()
            finally:
                c._committing_thread = None
            self.committed = True
            if terminal:
                c._terminal = True
                c._condition.notify_all()


class ExecutionControl:
    PERIOD_SECONDS = 20.0
    LEASE_SECONDS = 90
    DB_SECONDS = 1.0

    def __init__(self, *, session_factory, machine_token, claim, deadline):
        if (type(session_factory) is not sessionmaker or type(claim) is not repository.AgentClaim
                or claim.runtime_identity != repository.SDK_RUNTIME
                or not time.monotonic() < deadline < float('inf')):
            raise ExecutionControlError('pi_execution_control_factory_unsupported')
        original = session_factory.kw.get('bind')
        if (type(original) is not Engine or original.dialect.name != 'sqlite'
                or original.dialect.driver != 'pysqlite' or original.url.query
                or not original.url.database or original.url.database == ':memory:'
                or not Path(original.url.database).is_absolute()
                or not Path(original.url.database).is_file()):
            raise ExecutionControlError('pi_execution_control_factory_unsupported')
        self._application_engine = original
        self._engine = create_engine(original.url, poolclass=NullPool,
            connect_args={'timeout': self.DB_SECONDS, 'check_same_thread': False})
        @event.listens_for(self._engine, 'connect')
        def configure(connection, record):
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=1000')
            record.info['raw'] = connection
        @event.listens_for(self._engine, 'before_cursor_execute')
        def bounded_statement(connection, cursor, statement, parameters, context, many):
            end = connection.info.get('control_deadline', 0)
            if time.monotonic() >= end:
                raise ExecutionControlError('pi_execution_control_db_deadline')
            connection.info['raw'].set_progress_handler(
                lambda: int(time.monotonic() >= end), 1000)
        self._factory = sessionmaker(bind=self._engine, expire_on_commit=False)
        self._claim, self._machine_token = claim, machine_token
        self._condition = threading.Condition(threading.RLock())
        self._finished, self._cleanup_done, self._start_done = (threading.Event() for _ in range(3))
        self.handle = ControlHandle(threading.Event(), deadline, self._finished, self)
        self._state, self._phase, self._thread = 'new', None, None
        self._starting_thread = self._cleanup_owner = self._committing_thread = None
        self._cleanup_errors, self._cleanup_exception = [], None
        self._thread_terminated = False
        self._start_failed, self._start_error_type = False, None
        self._terminal = False
        self._safe_until = self._last_lease = self._last_db_clock = None
        self._beats = self._busy_retries = self._deferred = 0
        self._last_busy_code = self._reason = None

    @property
    def report(self):
        with self._condition:
            return {'beats': self._beats, 'reason': self._reason,
                'database_profile': 'sqlite-file-nullpool-v2-phase', 'state': self._state,
                'busy_retries': self._busy_retries, 'last_sqlite_code': self._last_busy_code,
                'deferred_owner_transaction': self._deferred,
                'terminal_committed': self._terminal,
                'last_confirmed_lease_until': self._last_lease.isoformat() if self._last_lease else None,
                'safe_until': self._safe_until, 'cleanup_done': self._cleanup_done.is_set(),
                'thread_termination_confirmed': self._thread_terminated,
                'start_error_type': self._start_error_type,
                'cleanup_errors': list(self._cleanup_errors)}

    def _check(self, *, starting=False):
        now = time.monotonic()
        if (self._finished.is_set() or self.handle.stop_event.is_set() or self._terminal
                or self._state not in (('starting', 'active') if starting else ('active',))):
            raise ExecutionControlError('pi_execution_control_stopped')
        if now >= self.handle.deadline:
            raise ExecutionControlError('pi_execution_control_deadline')
        if self._safe_until is not None and now >= self._safe_until:
            raise ExecutionControlError('pi_execution_control_lease_budget_exhausted')
        if self._safe_until is None and not starting:
            raise ExecutionControlError('pi_execution_control_no_lease_proof')

    def _publish_failure(self, reason):
        with self._condition:
            if not self._finished.is_set() and not self._terminal and self._reason is None:
                self._reason = reason
                self.handle.stop_event.set()
                self._condition.notify_all()

    def _failure_reason(self, error):
        if time.monotonic() >= self.handle.deadline:
            return 'deadline'
        if type(error) is ExecutionControlError and error.args == ('pi_execution_control_busy_exhausted',):
            return 'busy_exhausted'
        if type(error) is ExecutionControlError and error.args == ('pi_execution_control_lease_budget_exhausted',):
            return 'lease_budget_exhausted'
        if isinstance(error, (DBAPIError, sqlite3.Error)):
            return 'database_failure'
        if isinstance(error, repository.AgentPersistenceError):
            return 'authority_lost'
        return 'authority_or_database'

    @contextmanager
    def _transaction(self, kind, label=None, required_seconds=0):
        if kind == 'execute':
            if label not in _LABELS or type(required_seconds) not in (int, float) or not math.isfinite(required_seconds):
                raise ExecutionControlError('pi_execution_control_phase_invalid')
            if (label == 'tool_result' and not 0 <= required_seconds <= MAX_DECLARED_SYNC_SECONDS
                    or label != 'tool_result' and required_seconds != 0):
                raise ExecutionControlError('pi_execution_control_sync_declared_invalid')
        record = _Phase(self, kind, label)
        with self._condition:
            while True:
                if self._phase is not None and self._phase.thread == record.thread:
                    raise ExecutionControlError('pi_execution_control_nested_phase')
                if kind in ('execute', 'heartbeat'):
                    self._check(starting=kind == 'heartbeat')
                # Settlement after finish is intentional, but it cannot start
                # while this thread's original execute Session is still open.
                if self._phase is None:
                    self._phase = record
                    break
                if kind == 'heartbeat':
                    self._deferred += 1
                self._condition.wait(.05)
        try:
            if kind == 'execute':
                # Recheck AFTER waiting for the phase; no admission TOCTOU.
                need = required_seconds + COMMIT_MARGIN
                if min(self._safe_until, self.handle.deadline) - time.monotonic() < need:
                    self._renew()
                with self._condition:
                    self._check()
                    if min(self._safe_until, self.handle.deadline) - time.monotonic() < need:
                        raise ExecutionControlError('pi_execution_control_sync_budget_insufficient')
            yield record
        finally:
            with self._condition:
                record.closed = True
                self._phase = None
                self._condition.notify_all()

    def _renew_attempt(self):
        """One fresh full guard/CAS root. No proof survives an uncertain commit."""
        m0 = time.monotonic()
        with self._factory() as session:
            connection = session.connection()
            raw = connection.connection.driver_connection
            end = min(self.handle.deadline, m0 + self.DB_SECONDS)
            if self._safe_until is not None:
                end = min(end, self._safe_until)
            connection.info['control_deadline'] = end
            committed = False
            try:
                guard_execution(session, self._machine_token, self._claim,
                                expected_runtime=repository.SDK_RUNTIME)
                repository.heartbeat_worker_execution(session, self._claim,
                    lease_seconds=self.LEASE_SECONDS, expected_runtime=repository.SDK_RUNTIME)
                row = repository.require_worker_claim(session, self._claim, expected_runtime=repository.SDK_RUNTIME)
                d0, lease = repository._db_clock(session), row.lease_until
                if (type(d0) is not datetime or type(lease) is not datetime
                        or d0.tzinfo is None or lease.tzinfo is None
                        or self._last_db_clock is not None and d0 < self._last_db_clock):
                    raise ExecutionControlError('pi_execution_control_lease_proof_invalid')
                remaining = min(self.LEASE_SECONDS, max(0, (lease - d0).total_seconds()))
                safe = min(self.handle.deadline, m0 + remaining - SAFETY_MARGIN)
                with self._condition:
                    self._check(starting=True)
                    if safe <= time.monotonic():
                        raise ExecutionControlError('pi_execution_control_lease_budget_exhausted')
                    self._committing_thread = threading.get_ident()
                    try:
                        session.commit()
                    finally:
                        self._committing_thread = None
                    committed = True
                    self._safe_until, self._last_lease, self._last_db_clock = safe, lease, d0
                    self._beats += 1
            except DBAPIError as error:
                original = error.orig
                code = getattr(original, 'sqlite_errorcode', None)
                # A native failed SQLite root that is STILL in a transaction
                # can be rolled back. An after_commit failure cannot retry.
                if (not committed and not error.connection_invalidated
                        and isinstance(original, sqlite3.OperationalError)
                        and type(code) is int and code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                        and raw.in_transaction):
                    raise _Busy(code) from error
                raise

    def _renew(self):
        while True:
            try:
                self._renew_attempt()
                return
            except _Busy as error:
                with self._condition:
                    self._last_busy_code = error.code
                    # No initial proof means no invented local retry budget.
                    if self._safe_until is None:
                        raise ExecutionControlError('pi_execution_control_busy_no_proof') from error
                    self._check(starting=True)
                    self._busy_retries += 1
                    self._condition.wait(min(.1, max(0, self._safe_until-time.monotonic())))
                    if time.monotonic() >= min(self._safe_until, self.handle.deadline):
                        raise ExecutionControlError('pi_execution_control_busy_exhausted') from error

    def _beat(self):
        with self._transaction('heartbeat'):
            self._renew()

    def start(self):
        with self._condition:
            if self._state != 'new':
                raise ExecutionControlError('pi_execution_control_already_started')
            self._state, self._starting_thread = 'starting', threading.get_ident()
        try:
            self._beat()
            with self._condition:
                self._check(starting=True)
                self._state = 'active'
                self._thread = threading.Thread(target=self._run, name='pi-execution-heartbeat')
                self._thread.start()
                self._check()  # a reentrant start hook may have requested finish
        except BaseException as primary:
            with self._condition:
                self._start_failed, self._start_error_type = True, type(primary).__name__
                self.handle.stop_event.set()
                if not self._cleanup_done.is_set():
                    self._state = 'start_failed'
                self._start_done.set()
                self._condition.notify_all()
            try:
                self.finish()
            except BaseException:
                pass  # Cleanup metadata is retained; never replace the primary.
            raise
        self._start_done.set()
        return self.handle

    def _run(self):
        while not self._finished.wait(min(self.PERIOD_SECONDS,
                max(0, self.handle.deadline-time.monotonic()))):
            try:
                self._beat()
            except BaseException as error:
                self._publish_failure(self._failure_reason(error))
                return
            with self._condition:
                if self._terminal or self.handle.stop_event.is_set():
                    return

    def finish(self):
        """One cleanup owner; no state/phase/DB lock is held while joining."""
        me = threading.get_ident()
        with self._condition:
            if self._committing_thread == me:
                raise ExecutionControlError('pi_execution_control_reentrant_finish')
            self._finished.set()
            if self._state not in ('start_failed', 'cleanup_done'):
                self._state = 'finish_requested'
            self._condition.notify_all()
            # A guard callback may request finish inside initial admission.
            # Its Session must exit before startup performs cleanup.
            if (self._phase is not None and self._phase.thread == me
                    or self._starting_thread == me and not self._start_done.is_set()):
                return
            if self._cleanup_owner == me and not self._cleanup_done.is_set():
                raise ExecutionControlError('pi_execution_control_reentrant_cleanup')
            if self._cleanup_owner is None:
                self._cleanup_owner = me
                owner = True
            else:
                owner = False
            wait_start = self._starting_thread is not None and not self._start_done.is_set()
        if not owner:
            self._cleanup_done.wait()
            if self._cleanup_exception is not None:
                raise self._cleanup_exception
            return
        if wait_start:
            self._start_done.wait()
        errors = []
        try:
            if self._thread is not None and (self._thread.ident is not None
                    or self._thread.is_alive() or self._thread._started.is_set()):
                try:
                    self._thread.join()
                    self._thread_terminated = not self._thread.is_alive()
                except BaseException as error:
                    errors.append(('join', error))
            else:
                self._thread_terminated = True
        finally:
            try:
                self._engine.dispose()
            except BaseException as error:
                errors.append(('dispose', error))
            with self._condition:
                self._cleanup_errors = [{'stage': stage, 'type':type(error).__name__} for stage,error in errors]
                self._cleanup_exception = errors[0][1] if errors else None
                self._state = 'start_failed' if self._start_failed else 'cleanup_done'
                self._cleanup_done.set()
                self._condition.notify_all()
        if errors:
            raise errors[0][1]
