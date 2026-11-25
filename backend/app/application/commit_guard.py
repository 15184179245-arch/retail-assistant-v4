"""Final read-only checks for commands enlisted in one real Session root.

This module owns neither transactions nor authorization policy. Fixed domain
handlers acquire their authority locks first, then enlist internal checks. The
locks and the obligations live until the caller's root transaction ends, even
when an intermediate SAVEPOINT is released. Only this participating Session
receives listeners; ordinary Sessions retain SQLAlchemy's normal behavior.

Checks must perform only reads and must not flush, commit, roll back, obtain
new locks, or issue DML. The caller must roll back after a failed final check.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import event, text
from sqlalchemy.orm import Session, SessionTransaction
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import ColumnClause, TextClause
from sqlalchemy.sql.selectable import TextualSelect

_STATE_KEY = "retail.application.command_commit_guard.root"
_LISTENERS_KEY = "retail.application.command_commit_guard.listeners"

ReadCheck = Callable[[Session, datetime], None]


class CommitGuardError(RuntimeError):
    """The root must be rolled back; no command or caller work was committed."""


def rejected_before_commit(session: Session, captured_root, error: BaseException) -> bool:
    """Prove this exact root rejected a check, before rollback removes evidence.

    This is lifecycle provenance only, never an authorization or receipt proof.
    Flush/driver/after-commit failures do not become policy rejections here.
    """
    state = session.info.get(_STATE_KEY)
    if (captured_root is None or session.get_transaction() is not captured_root
            or state is None or state.root is not captured_root or state.failure is None):
        return False
    return state.failure is error or (
        isinstance(error, CommitGuardError) and str(error) == "command_transaction_aborted"
        and error.__cause__ is state.failure)


@dataclass
class _Obligation:
    action: str
    check: ReadCheck
    frame: SessionTransaction


@dataclass
class _RootChecks:
    root: SessionTransaction
    obligations: list[_Obligation] = field(default_factory=list)
    released: set[SessionTransaction] = field(default_factory=set)
    failure: BaseException | None = None
    validating: bool = False
    checking: bool = False


def _db_clock(session: Session) -> datetime:
    """Read a fresh database instant, never PostgreSQL's transaction timestamp."""
    sql = (
        "SELECT clock_timestamp()"
        if session.get_bind().dialect.name == "postgresql"
        else "SELECT strftime('%Y-%m-%d %H:%M:%f','now')"
    )
    value = session.execute(text(sql)).scalar_one()
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(instant, datetime):
        raise CommitGuardError("command_clock_invalid")
    return instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant.astimezone(timezone.utc)


def enlist(session: Session, *, action: str, check: ReadCheck) -> None:
    """Retain one internal domain check through the current root's commit.

    No root is created here. The handler must have already taken its real
    authority locks in this Session before any command effects are staged.
    Repeated action names intentionally retain every individual obligation.
    """
    root = session.get_transaction()
    if root is None:
        raise CommitGuardError("command_root_required")
    if not isinstance(action, str) or not action or not callable(check):
        raise CommitGuardError("command_check_invalid")
    state = session.info.get(_STATE_KEY)
    if state is None:
        state = _RootChecks(root)
        session.info[_STATE_KEY] = state
    if state.root is not root:
        raise CommitGuardError("command_root_changed")
    if state.validating:
        raise CommitGuardError("command_check_reentrant")
    if state.failure is not None:
        raise CommitGuardError("command_transaction_aborted") from state.failure
    if not session.info.get(_LISTENERS_KEY):
        # Instance listeners persist as no-ops after root cleanup. Removing a
        # listener while its event is dispatching would mutate that event list.
        event.listen(session, "before_commit", _before_commit)
        event.listen(session, "after_commit", _after_commit)
        event.listen(session, "after_transaction_end", _after_transaction_end)
        event.listen(session, "after_soft_rollback", _after_soft_rollback)
        event.listen(session, "before_flush", _before_flush)
        session.info[_LISTENERS_KEY] = True
    state.obligations.append(_Obligation(action, check, session.get_nested_transaction() or root))


def _before_commit(session: Session) -> None:
    state = session.info.get(_STATE_KEY)
    if state is None:
        return
    if state.root is not session.get_transaction():
        raise CommitGuardError("command_root_changed")
    if state.failure is not None:
        raise CommitGuardError("command_transaction_aborted") from state.failure
    if not state.obligations:
        return
    if state.validating:
        raise CommitGuardError("command_check_reentrant")
    state.validating = True
    flushed = False
    try:
        # Finish every pending write/unique-key wait BEFORE choosing the final
        # clock. Checks cannot leave work for SQLAlchemy's implicit commit flush.
        session.flush()
        flushed = True
        if session.new or session.dirty or session.deleted:
            raise CommitGuardError("command_flush_unsettled")
        with session.no_autoflush:
            instant = _db_clock(session)
            connection = session.connection()
            # Scope the SQL guard to this already checked-out connection and
            # these read callbacks only, including direct Connection.execute.
            # The DB clock precedes it: callbacks need only structured Selects.
            def read_only_statement(*arguments):
                _read_only_statement(state, *arguments)

            event.listen(connection, "before_cursor_execute", read_only_statement)
            state.checking = True
            try:
                for obligation in tuple(state.obligations):
                    result = obligation.check(session, instant)
                    if state.failure is not None:
                        raise CommitGuardError("command_transaction_aborted") from state.failure
                    if result is not None:
                        _reject(state, "command_check_result_invalid")
                    if session.new or session.dirty or session.deleted:
                        raise CommitGuardError("command_check_mutated_session")
            finally:
                state.checking = False
                event.remove(connection, "before_cursor_execute", read_only_statement)
    except BaseException as error:
        # An ordinary database flush error belongs to its current SAVEPOINT.
        # SQLAlchemy can roll that layer back and keep earlier root effects.
        # Only final validation/readonly failures make the root abort-only.
        if flushed and state.failure is None:
            state.failure = error
        raise
    finally:
        state.validating = False


def _before_flush(session: Session, _context, _instances) -> None:
    state = session.info.get(_STATE_KEY)
    if state is not None and state.checking:
        _reject(state, "command_check_write_forbidden")


def _reject(state: _RootChecks, code: str) -> None:
    error = CommitGuardError(code)
    if state.failure is None:
        state.failure = error
    raise error


def _read_only_statement(state, _connection, _cursor, _sql, _parameters, context, _many) -> None:
    compiled = context.compiled
    statement = compiled.statement if compiled is not None else None
    if statement is None or not statement.is_select:
        _reject(state, "command_check_write_forbidden")
    # A top-level SELECT can still carry a writing CTE or a locking subquery.
    # Inspect the structured SQLAlchemy tree, never parse arbitrary SQL text.
    for node in visitors.iterate(statement):
        if (isinstance(node, (TextClause, TextualSelect))
                or (isinstance(node, ColumnClause) and node.is_literal)
                or getattr(node, "is_dml", False) or getattr(node, "is_ddl", False)
                or getattr(node, "_for_update_arg", None) is not None):
            _reject(state, "command_check_write_forbidden")


def _after_commit(session: Session) -> None:
    state = session.info.get(_STATE_KEY)
    nested = session.get_nested_transaction()
    if state is not None and nested is not None:
        # after_commit also fires for SAVEPOINT release; it is not evidence of
        # a root commit and must never discard the remaining obligations.
        state.released.add(nested)


def _after_transaction_end(session: Session, transaction: SessionTransaction) -> None:
    state = session.info.get(_STATE_KEY)
    if state is None:
        return
    if transaction is state.root:
        session.info.pop(_STATE_KEY, None)
    elif transaction.nested:
        if transaction in state.released:
            for obligation in state.obligations:
                if obligation.frame is transaction:
                    obligation.frame = transaction.parent
        else:
            state.obligations[:] = [
                obligation for obligation in state.obligations if obligation.frame is not transaction
            ]
        state.released.discard(transaction)
    # A flush's non-nested child is not a SAVEPOINT or root boundary.


def _after_soft_rollback(session: Session, transaction: SessionTransaction) -> None:
    # after_transaction_end normally ran first. Keep cleanup idempotent when
    # SQLAlchemy reports an already-deactivated root rollback as a soft event.
    state = session.info.get(_STATE_KEY)
    if state is not None and transaction is state.root:
        session.info.pop(_STATE_KEY, None)
