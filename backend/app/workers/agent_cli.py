"""Foreground command entry for the real Agent worker (AGT-026 B2e-1).

``python -m backend.app.workers.agent_cli once|start|recover``

The CLI is a thin, explicitly-invoked foreground front end: it never installs a
daemon, never creates a PID file and never signals unrelated processes. ``once``
performs at most one real execution tick; ``start`` loops over the same real
tick until a foreground SIGINT/SIGTERM stop event; ``recover --run-id`` reconciles
an existing Run instead of executing it.

Failure reporting is deliberately opaque: only stable machine-readable codes and
bounded counters are printed. Environment variables, tokens, DSNs/SQL, provider
payloads and raw exception text are never echoed (including for mistyped
arguments, where ``argparse`` would normally repeat the offending value).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import signal
import sys

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_INVALID = 2
EXIT_CONFIG = 3
EXIT_AUTH = 4
EXIT_STORAGE = 5
EXIT_ATTENTION = 6

_DB_ENV = "DATABASE_URL"
_TOKEN_ENV = "AGENT_WORKER_MACHINE_TOKEN"
_RUN_FLAGS = ("AGENT_WORKER_DISPATCH_ENABLED", "AGENT_WORKER_RUNTIME_ENABLED")
_EXECUTE_ACTION = "worker.agent.execute"
_RECOVER_ACTION = "worker.agent.recover"
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}\Z")
# A machine token must never be echoable through ``recover --run-id``; real Run
# ids are UUIDs, so any ``sx<n>_`` credential shape is rejected before DB access.
_TOKEN_SHAPE_RE = re.compile(r"sx[0-9]+_")

_COMMANDS = ("once", "start", "recover")


class _InvalidArguments(Exception):
    """Argument problem: reported as a code, never by echoing the value."""


class _ConfigError(Exception):
    """Required environment configuration is missing; DB is untouched."""


class _AuthDenied(Exception):
    """Machine credential/scope/action was denied at a DB boundary."""


class _StorageError(Exception):
    """The database could not be read/written; fail closed with no facts lost."""


class _SignalError(Exception):
    """A foreground loop must not claim work without its stop handlers."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # noqa: D401 - argparse hook
        # Never forward ``message``: argparse embeds the offending value, which
        # could be a mistyped secret.
        raise _InvalidArguments()


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="agent_cli", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True)
    once = commands.add_parser("once")
    once.add_argument("--lease-seconds", type=int, default=30)
    start = commands.add_parser("start")
    start.add_argument("--lease-seconds", type=int, default=30)
    start.add_argument("--poll-seconds", type=float, default=1.0)
    start.add_argument("--max-ticks", type=int, default=None)
    recover = commands.add_parser("recover")
    recover.add_argument("--run-id", required=True)
    return parser


def _validate(options: argparse.Namespace) -> None:
    if options.command in ("once", "start"):
        lease = options.lease_seconds
        if type(lease) is not int or not 1 <= lease <= 300:
            raise _InvalidArguments()
    if options.command == "start":
        poll = options.poll_seconds
        if (isinstance(poll, bool) or not isinstance(poll, (int, float))
                or not math.isfinite(float(poll))
                or not 0.05 <= float(poll) <= 60.0):
            raise _InvalidArguments()
        if options.max_ticks is not None and (
                type(options.max_ticks) is not int or options.max_ticks < 1):
            raise _InvalidArguments()
    if options.command == "recover":
        run_id = options.run_id
        if (type(run_id) is not str or not _RUN_ID_RE.fullmatch(run_id)
                or _TOKEN_SHAPE_RE.match(run_id)):
            raise _InvalidArguments()


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def _runtime_enabled() -> bool:
    # Read only; the CLI never enables a worker switch on the caller's behalf.
    return all(os.environ.get(flag) == "1" for flag in _RUN_FLAGS)


def _dispose_engine(engine) -> None:
    """Attempt local cleanup without replacing the command's outcome."""
    if engine is None:
        return
    try:
        engine.dispose()
    except Exception:
        # A driver cleanup failure is not permission to retry the command.
        pass


def _prepare(action: str):
    """Config + DB-time machine pre-authentication before any claim/execution."""
    database_url = _env(_DB_ENV)
    token = _env(_TOKEN_ENV)
    if database_url is None or token is None:
        raise _ConfigError()
    from backend.app.db.session import make_engine
    from backend.app.modules.agents import repository
    from backend.app.modules.identity import machine_service

    engine = None
    try:
        engine = make_engine(database_url)
        factory = sessionmaker(bind=engine, class_=Session, autoflush=False,
                               expire_on_commit=False)
        with factory() as session:
            now = repository._db_clock(session)
            principal = machine_service.authenticate_machine_token(
                session, token, now=now)
            machine_service.authorize_machine_action(
                session, principal,
                organization_id=principal.organization_id,
                store_id=principal.store_id, action=action, now=now)
    except HTTPException as error:
        _dispose_engine(engine)
        raise _AuthDenied() from error
    except Exception as error:
        # Engine construction or any DB failure: stable code only, never the
        # raw URL/traceback (a malformed DATABASE_URL is still a storage error).
        _dispose_engine(engine)
        raise _StorageError() from error
    return engine, factory, principal, token


def _exit_for_recover(outcome: str) -> int:
    if outcome == "denied":
        return EXIT_AUTH
    if outcome == "preserved":
        return EXIT_ATTENTION
    return EXIT_OK


def _exit_for_tick(state: str) -> int:
    if state == "denied":
        return EXIT_AUTH
    if state in ("preserved", "unknown"):
        return EXIT_ATTENTION
    return EXIT_OK


def _run_once(*, factory, token: str, lease_seconds: int) -> dict:
    from backend.app.workers.agent_runner import tick_real_agent_worker

    state = asyncio.run(tick_real_agent_worker(
        session_factory=factory, machine_token=token,
        lease_seconds=lease_seconds))
    return {"command": "once", "status": state, "ticks": 1,
            "exit": _exit_for_tick(state)}


def _install_stop_handlers(stop_event: asyncio.Event) -> dict[int, object]:
    loop = asyncio.get_running_loop()
    previous: dict[int, object] = {}

    def _stop(signum, frame):  # noqa: ANN001 - signal handler signature
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            previous.pop(sig, None)
    if len(previous) != 2:
        _restore_stop_handlers(previous)
        raise _SignalError()
    return previous


def _restore_stop_handlers(previous: dict[int, object]) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, TypeError):
            pass


def _run_start(*, factory, token: str, lease_seconds: int,
               poll_seconds: float, max_ticks: int | None) -> dict:
    from backend.app.workers.runner import run_real_agent_worker_until_stopped

    async def _foreground() -> dict:
        stop_event = asyncio.Event()
        previous = _install_stop_handlers(stop_event)
        try:
            # Handlers are in place and config/identity were validated before
            # anyone may act on this "started" marker.
            _emit({"command": "start", "status": "started",
                   "lease_seconds": lease_seconds,
                   "poll_seconds": poll_seconds, "max_ticks": max_ticks})
            return await run_real_agent_worker_until_stopped(
                session_factory=factory, machine_token=token,
                stop_event=stop_event, lease_seconds=lease_seconds,
                poll_seconds=poll_seconds, max_ticks=max_ticks)
        finally:
            _restore_stop_handlers(previous)

    summary = asyncio.run(_foreground())
    status = summary["status"]
    if status == "denied":
        code = EXIT_AUTH
    elif status == "preserved" or summary["counts"].get("unknown", 0) > 0:
        code = EXIT_ATTENTION
    else:
        code = EXIT_OK
    return {"command": "start", "status": status,
            "reason": summary["reason"], "counts": summary["counts"],
            "exit": code}


def _run_recover(*, factory, token: str, principal, run_id: str) -> dict:
    """Exclusive-fence reaper of exactly one Run, then public recover facts."""
    from backend.app.modules.agents import repository
    from backend.app.modules.agents.repository import reap_worker_executions
    from backend.app.modules.identity import machine_service
    from backend.app.modules.identity.command_guard import (
        ScopeSecurityFenceError, lock_scope_security_fence,
    )
    from backend.app.workers.agent_executor import recover_attempt

    with factory() as session:
        try:
            now = repository._db_clock(session)
            machine_service.authenticate_machine_token(session, token, now=now)
            # Exclusive scope fence BEFORE any store/run lock; the reaper takes
            # the same fence (no store held yet, never an upgrade after it).
            lock_scope_security_fence(
                session, principal.organization_id, principal.store_id,
                exclusive=True)
            now = repository._db_clock(session)
            again = machine_service.authenticate_machine_token(
                session, token, now=now)
            machine_service.authorize_machine_action(
                session, again,
                organization_id=principal.organization_id,
                store_id=principal.store_id, action=_RECOVER_ACTION, now=now)
            reap_worker_executions(
                session, organization_id=principal.organization_id,
                store_id=principal.store_id, run_id=run_id)
            session.commit()
        except (HTTPException, ScopeSecurityFenceError) as error:
            session.rollback()
            raise _AuthDenied() from error
        except SQLAlchemyError as error:
            session.rollback()
            raise _StorageError() from error
        except BaseException:
            session.rollback()
            raise
    outcome = recover_attempt(
        session_factory=factory, machine_token=token,
        organization_id=principal.organization_id,
        store_id=principal.store_id, run_id=run_id)
    return {"command": "recover", "run_id": run_id, "status": outcome,
            "exit": _exit_for_recover(outcome)}


def _command_hint(argv: list[str]) -> str:
    for token in argv:
        if token in _COMMANDS:
            return token
        if not token.startswith("-"):
            return "unknown"
    return "unknown"


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        options = _build_parser().parse_args(raw)
        _validate(options)
    except _InvalidArguments:
        _emit({"command": _command_hint(raw), "status": "error",
               "code": "invalid_arguments", "exit": EXIT_INVALID})
        return EXIT_INVALID

    command = options.command
    if command in ("once", "start") and not _runtime_enabled():
        _emit({"command": command, "status": "disabled",
               "code": "worker_runtime_disabled", "exit": EXIT_OK})
        return EXIT_OK

    action = _RECOVER_ACTION if command == "recover" else _EXECUTE_ACTION
    try:
        engine, factory, principal, token = _prepare(action)
    except _ConfigError:
        _emit({"command": command, "status": "error",
               "code": "configuration_missing", "exit": EXIT_CONFIG})
        return EXIT_CONFIG
    except _AuthDenied:
        _emit({"command": command, "status": "error",
               "code": "machine_auth_denied", "exit": EXIT_AUTH})
        return EXIT_AUTH
    except _StorageError:
        _emit({"command": command, "status": "error",
               "code": "database_unavailable", "exit": EXIT_STORAGE})
        return EXIT_STORAGE

    try:
        if command == "once":
            payload = _run_once(factory=factory, token=token,
                                lease_seconds=options.lease_seconds)
        elif command == "start":
            payload = _run_start(factory=factory, token=token,
                                 lease_seconds=options.lease_seconds,
                                 poll_seconds=options.poll_seconds,
                                 max_ticks=options.max_ticks)
        else:
            payload = _run_recover(factory=factory, token=token,
                                   principal=principal, run_id=options.run_id)
    except _AuthDenied:
        _emit({"command": command, "status": "error",
               "code": "machine_auth_denied", "exit": EXIT_AUTH})
        return EXIT_AUTH
    except _StorageError:
        _emit({"command": command, "status": "error",
               "code": "database_unavailable", "exit": EXIT_STORAGE})
        return EXIT_STORAGE
    except _SignalError:
        _emit({"command": command, "status": "error",
               "code": "signal_handlers_unavailable", "exit": EXIT_INTERNAL})
        return EXIT_INTERNAL
    except SQLAlchemyError:
        _emit({"command": command, "status": "error",
               "code": "database_unavailable", "exit": EXIT_STORAGE})
        return EXIT_STORAGE
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except BaseException:
        # Stable code only: raw exception text may carry DSNs or payloads.
        _emit({"command": command, "status": "error",
               "code": "internal_error", "exit": EXIT_INTERNAL})
        return EXIT_INTERNAL
    finally:
        _dispose_engine(engine)

    _emit(payload)
    return payload["exit"]


if __name__ == "__main__":
    sys.exit(main())
