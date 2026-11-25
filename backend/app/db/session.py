from functools import lru_cache

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.config import database_url


# SQLite pool hygiene is engine-local and installed before first checkout.
# It preserves the bounded wait through the transaction, not a total deadline.
_TIMEOUT_ENGINE_TOKEN = "_retail_busy_timeout_engine_v1"
_TIMEOUT_OWNER = "_retail_busy_timeout_owner_v1"
_TIMEOUT_ARM = "_retail_busy_timeout_arm_v1"
_TIMEOUT_SESSION_OWNER = "_retail_busy_timeout_session_owner_v1"


class SQLiteTimeoutOwnershipError(RuntimeError):
    """The bounded transaction is not owned by a supported Session lifecycle."""


class SQLiteTimeoutHealthError(RuntimeError):
    """The physical connection must not be returned for reuse."""


class _SQLiteTimeoutArm:
    __slots__ = ("connection", "fairy", "record", "raw", "prior", "token", "poisoned")

    def __init__(self, connection, fairy, record, raw, prior):
        self.connection = connection
        self.fairy = fairy
        self.record = record
        self.raw = raw
        self.prior = prior
        self.token = object()
        self.poisoned = False


def _sqlite_timeout(raw) -> int:
    cursor = raw.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout")
        return int(cursor.fetchone()[0])
    finally:
        cursor.close()


def _set_sqlite_timeout(raw, value: int) -> None:
    cursor = raw.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout = {value}")
    finally:
        cursor.close()


def _install_sqlite_timeout_guard(engine) -> None:
    if getattr(engine, _TIMEOUT_ENGINE_TOKEN, None) is not None:
        return
    token = object()

    @event.listens_for(engine, "connect")
    def identify_sqlite_record(raw, record):
        record.info[_TIMEOUT_OWNER] = (record, raw, token)

    @event.listens_for(engine, "reset")
    def restore_sqlite_record(raw, record, _state):
        # Detached close has no record. Raising here can prevent SQLAlchemy's
        # close_detached path. Leave its rollback/close untouched.
        if record is None:
            return
        arm = record.info.get(_TIMEOUT_ARM)
        if arm is None:
            return
        owner = record.info.get(_TIMEOUT_OWNER)
        if (not isinstance(arm, _SQLiteTimeoutArm)
                or owner is None or owner[0] is not record
                or owner[1] is not raw or owner[2] is not token
                or arm.record is not record or arm.raw is not raw):
            raise SQLiteTimeoutHealthError("sqlite_timeout_record_mismatch")
        if arm.poisoned:
            raise SQLiteTimeoutHealthError("sqlite_timeout_poisoned")
        # COMMIT failure can leave raw active despite an inactive SQLAlchemy
        # root / transaction_was_reset=True. Do not restore a longer wait then.
        try:
            if raw.in_transaction:
                raise SQLiteTimeoutHealthError("sqlite_timeout_transaction_not_ended")
        except BaseException:
            # Identity is already proven. If invalidation itself is interrupted,
            # a later idle/reset must not rehabilitate this rejected checkout.
            arm.poisoned = True
            raise
        try:
            _set_sqlite_timeout(raw, arm.prior)
            if _sqlite_timeout(raw) != arm.prior:
                raise SQLiteTimeoutHealthError("sqlite_timeout_restore_mismatch")
        except BaseException as error:
            arm.poisoned = True
            if isinstance(error, Exception):
                raise SQLiteTimeoutHealthError("sqlite_timeout_restore_failed") from error
            # Cancellation / process-control signals retain their identity;
            # poison must survive even if subsequent invalidation is interrupted.
            raise
        del record.info[_TIMEOUT_ARM]
        # Ordinary reset errors are logged/discarded by SQLAlchemy, not made
        # into a retryable error for a business commit that already succeeded.

    setattr(engine, _TIMEOUT_ENGINE_TOKEN, token)


def _timeout_session_owner(session):
    owner = getattr(session, "info", {}).get(_TIMEOUT_SESSION_OWNER)
    if (type(owner) is tuple and len(owner) == 2
            and isinstance(owner[0], _SQLiteTimeoutArm)):
        return owner
    return None


def has_sqlite_timeout_owner(session) -> bool:
    return _timeout_session_owner(session) is not None


def _same_timeout_checkout(arm, token) -> bool:
    """Only non-reconnecting identity reads; never connection.connection."""
    fairy, record, raw = arm.fairy, arm.record, arm.raw
    reference = record.fairy_ref
    return (
        arm.token is token
        and arm.connection._dbapi_connection is fairy
        and fairy._connection_record is record
        and fairy.dbapi_connection is raw
        and record.dbapi_connection is raw
        and reference is not None and reference() is fairy
        and record.info.get(_TIMEOUT_ARM) is arm
    )


def discard_failed_sqlite_timeout_owner(session, primary: BaseException) -> None:
    """Exceptional owner cleanup; never reborrow, replay or invalidate a new owner."""
    owner = _timeout_session_owner(session)
    if owner is None:
        return
    arm, token = owner
    if not _same_timeout_checkout(arm, token):
        # A successful reset may have removed the marker and B may own the raw.
        # Missing/changed identity is NOT authority to poison or invalidate it.
        primary.add_note("sqlite_timeout_owner_changed: not invalidated; cleanup not inferred")
        return
    # Poison first: invalidate itself can be interrupted after a completed
    # rollback whose acknowledgement was lost (raw=False). Later reset must
    # not rehabilitate this uncertain arm.
    arm.poisoned = True
    try:
        arm.connection.invalidate(primary)
    except BaseException as cleanup:
        primary.add_note("sqlite_timeout_invalidation_uncertain: " + repr(cleanup))
        return
    if _same_timeout_checkout(arm, token):
        primary.add_note("sqlite_timeout_owner_still_held: HOLD")
    else:
        primary.add_note("sqlite_timeout_owner_discarded: physical close not inferred")


def forget_released_sqlite_timeout_owner(session) -> None:
    owner = _timeout_session_owner(session)
    if owner is not None and not _same_timeout_checkout(*owner):
        session.info.pop(_TIMEOUT_SESSION_OWNER, None)


def close_sqlite_timeout_owned_session(session, primary: BaseException | None = None) -> None:
    """Close only a caller-owned Session, keeping its first failure authoritative."""
    if not has_sqlite_timeout_owner(session):
        session.close()  # unarmed lifecycle/exception behavior is unchanged
        return
    try:
        session.close()
    except BaseException as close_error:
        original = primary if primary is not None else close_error
        discard_failed_sqlite_timeout_owner(session, original)
        if primary is not None:
            primary.add_note("sqlite_timeout_session_close_failed: " + repr(close_error))
            # Caller is already unwinding this primary. Do not mask it.
            return
        raise
    finally:
        forget_released_sqlite_timeout_owner(session)


def arm_sqlite_busy_timeout(session: Session, timeout_ms: int) -> None:
    """Arm a fresh normal SQLite Engine Session; preserve its physical owner."""
    from sqlalchemy.engine import Engine

    bind = session.get_bind()
    if not isinstance(bind, Engine):
        raise SQLiteTimeoutOwnershipError("sqlite_timeout_external_bind")
    token = getattr(bind, _TIMEOUT_ENGINE_TOKEN, None)
    if (bind.dialect.name != "sqlite" or bind.dialect.driver != "pysqlite"
            or bind.dialect.is_async or token is None):
        raise SQLiteTimeoutOwnershipError("sqlite_timeout_unregistered_engine")
    if session.in_nested_transaction():
        raise SQLiteTimeoutOwnershipError("sqlite_timeout_nested_transaction")
    if session.in_transaction():
        raise SQLiteTimeoutOwnershipError("sqlite_timeout_existing_session_transaction")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 500:
        raise ValueError("sqlite_timeout_invalid_budget")
    connection = session.connection()
    fairy = connection.connection
    record = fairy._connection_record
    raw = fairy.driver_connection
    if fairy.is_detached or record is None:
        raise SQLiteTimeoutOwnershipError("sqlite_timeout_detached_connection")
    owner = record.info.get(_TIMEOUT_OWNER)
    if (owner is None or owner[0] is not record or owner[1] is not raw
            or owner[2] is not token or record.dbapi_connection is not raw):
        raise SQLiteTimeoutOwnershipError("sqlite_timeout_owner_mismatch")
    if _TIMEOUT_ARM in record.info:
        raise SQLiteTimeoutOwnershipError("sqlite_timeout_already_armed")
    if connection.in_nested_transaction() or raw.in_transaction:
        raise SQLiteTimeoutOwnershipError("sqlite_timeout_existing_transaction")
    prior = _sqlite_timeout(raw)
    arm = _SQLiteTimeoutArm(connection, fairy, record, raw, prior)
    record.info[_TIMEOUT_ARM] = arm
    session.info[_TIMEOUT_SESSION_OWNER] = (arm, arm.token)
    try:
        _set_sqlite_timeout(raw, timeout_ms)
        if _sqlite_timeout(raw) != timeout_ms:
            raise SQLiteTimeoutHealthError("sqlite_timeout_set_mismatch")
    except BaseException as error:
        # SET may have happened before its acknowledgement failed. No business
        # transaction exists yet; discard the same proven owner, do not reuse.
        discard_failed_sqlite_timeout_owner(session, error)
        raise


def make_engine(url: str | None = None):
    resolved = url or database_url()
    kwargs = {"check_same_thread": False} if resolved.startswith("sqlite") else {}
    engine = create_engine(resolved, future=True, pool_pre_ping=True, connect_args=kwargs)
    if resolved.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
        _install_sqlite_timeout_guard(engine)
    return engine


@lru_cache(maxsize=1)
def _configured_engine():
    # Resolve DATABASE_URL only when a database-backed endpoint is used.
    return make_engine()


def get_session():
    factory = sessionmaker(bind=_configured_engine(), class_=Session, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


def get_auth_session():
    """Keep credential reads separate from the business route transaction."""
    yield from get_session()
