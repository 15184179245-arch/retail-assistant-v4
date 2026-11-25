"""Transactional outbox worker runner.

Handlers must pass ``event.id`` or ``event.idempotency_key`` to the external
system as its idempotency key. A crash after external success but before the
receipt commit can cause redelivery; the local lease cannot provide exactly-once
delivery across that boundary.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Protocol
from threading import Event
import asyncio

from sqlalchemy.orm import Session

from backend.app.workers.outbox import (
    OutboxEvent,
    acknowledge_receipt,
    claim_one,
    record_failure,
    validate_claim_filters,
)
from backend.app.workers.agent_runner import recover_one as recover_agent_once
from backend.app.workers.agent_runner import tick_agent_worker
from backend.app.workers.agent_runner import tick_real_agent_worker

# ``recover_agent_once`` is the controlled Agent reconciliation entry. It is not
# a persistent lease: each call reconciles at most one expired Run and relies on
# Run/Step version CAS plus the database deadline recorded on the Run.
__all__ = ["OutboxMessage", "Handler", "run_once", "recover_agent_once",
           "tick_agent_worker", "run_agent_worker_until_stopped",
           "run_real_agent_worker_until_stopped", "REAL_WORKER_TICK_STATES"]

REAL_WORKER_TICK_STATES = ("finished", "waiting", "idle", "unknown",
                           "cancelled", "failed", "preserved", "disabled",
                           "denied")


@dataclass(frozen=True)
class OutboxMessage:
    id: str
    idempotency_key: str
    organization_id: str
    store_id: str
    event_type: str
    payload: dict
    # Frozen original claim identity, never reconstructed from a later DB row.
    # Defaults only preserve handler-only synthetic message construction.
    lease_attempt: int | None = None
    lease_until: datetime | None = None


class Handler(Protocol):
    def __call__(self, event: OutboxMessage) -> str: ...


def _message(row: OutboxEvent) -> OutboxMessage:
    return OutboxMessage(
        id=row.id,
        idempotency_key=row.idempotency_key,
        organization_id=row.organization_id,
        store_id=row.store_id,
        event_type=row.event_type,
        payload=row.payload,
        lease_attempt=row.attempts,
        lease_until=row.lease_until,
    )


def run_once(
    *,
    session_factory: Callable[[], Session],
    handlers: Mapping[str, Handler],
    worker_id: str,
    lease_seconds: int = 30,
    max_attempts: int = 8,
    now: datetime | None = None,
    event_types: tuple[str, ...] | None = None,
    organization_id: str | None = None,
    store_id: str | None = None,
) -> str:
    """Claim and process at most one event; return idle/delivered/retry/dead.

    The lease is committed before a handler runs. Handler execution happens
    without a database transaction. A receipt is acknowledged only after the
    handler returns a non-empty string, and both outcome paths are committed.

    ``event_types``/``organization_id``/``store_id`` are passed through to the
    claim boundary unchanged, so a dedicated worker cannot lease or dead-letter
    another store's or another type's event. ``None`` keeps the original
    all-event behaviour; an empty ``event_types`` tuple returns ``idle`` without
    touching the database, so claim-level argument validation is not reached on
    that path.
    """
    validate_claim_filters(event_types=event_types, organization_id=organization_id, store_id=store_id)
    if event_types is not None and not event_types:
        return "idle"
    with session_factory() as session:
        row = claim_one(
            session,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
            now=now,
            event_types=event_types,
            organization_id=organization_id,
            store_id=store_id,
        )
        message = _message(row) if row is not None else None
        session.commit()
    if message is None:
        return "idle"

    handler = handlers.get(message.event_type)
    error_code = "UNKNOWN_EVENT_TYPE"
    receipt = None
    if handler is not None:
        try:
            receipt = handler(message)
            if not isinstance(receipt, str) or not receipt.strip():
                error_code = "MISSING_EXTERNAL_RECEIPT"
                receipt = None
        except Exception:
            # Do not persist exception text: it may contain credentials,
            # provider payloads, or personal data.
            error_code = "HANDLER_ERROR"

    with session_factory() as session:
        if receipt is not None:
            acknowledge_receipt(
                session,
                event_id=message.id,
                worker_id=worker_id,
                expected_attempt=message.lease_attempt,
                expected_lease_until=message.lease_until,
                external_receipt=receipt,
                now=now,
            )
        else:
            failed = record_failure(
                session,
                event_id=message.id,
                worker_id=worker_id,
                expected_attempt=message.lease_attempt,
                expected_lease_until=message.lease_until,
                error=error_code,
                max_attempts=max_attempts,
                now=now,
            )
            outcome = failed.status
        session.commit()
    return "delivered" if receipt is not None else outcome


def run_agent_worker_until_stopped(*, session_factory: Callable[[], Session],
                                   machine_token: str, stop_event: Event,
                                   synthetic_executor: Callable[[str], str] | None = None,
                                   poll_seconds: float = 1.0,
                                   max_ticks: int | None = None) -> list[str]:
    """A cooperative, bounded stop-aware Worker loop for isolated trials.

    This is not a daemon installed by default. Without both explicit feature
    flags and a trusted synthetic callback it returns disabled and stops.
    """
    if not 0.05 <= poll_seconds <= 60.0:
        raise ValueError("poll_seconds must be between 0.05 and 60")
    if max_ticks is not None and (type(max_ticks) is not int or max_ticks < 1):
        raise ValueError("max_ticks must be positive")
    statuses: list[str] = []
    while not stop_event.is_set() and (max_ticks is None or len(statuses) < max_ticks):
        state = tick_agent_worker(session_factory=session_factory, machine_token=machine_token,
                                  executor=synthetic_executor)
        statuses.append(state)
        if state == "disabled":
            break
        if state == "idle":
            stop_event.wait(poll_seconds)
    return statuses


_TERMINAL_REAL_TICK_STATES = frozenset({"disabled", "denied", "preserved"})
_REAL_WORKER_COUNT_KEYS = ("ticks",) + REAL_WORKER_TICK_STATES


def _empty_real_counts() -> dict[str, int]:
    """Fixed-size summary: one counter per finite tick state, never a log list."""
    return {key: 0 for key in _REAL_WORKER_COUNT_KEYS}


async def _stop_wait(stop_event: asyncio.Event, timeout: float) -> None:
    """Sleep up to ``timeout`` but wake immediately when the stop event is set."""
    if timeout <= 0:
        await asyncio.sleep(0)
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


async def run_real_agent_worker_until_stopped(
    *,
    session_factory: Callable[[], Session],
    machine_token: str,
    stop_event: asyncio.Event,
    provider_factory=None,
    lease_seconds: int = 30,
    poll_seconds: float = 1.0,
    max_ticks: int | None = None,
) -> dict:
    """Foreground, cooperative loop over the REAL B2d executor entry.

    Every iteration calls :func:`tick_real_agent_worker` (the only bridge to the
    real executor); no synthetic callback or replay of a previous Run is
    involved. Only bounded summary counters are kept. ``provider_factory`` is
    forwarded untouched so callers/tests may replace just the Provider network.
    """
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
        raise ValueError("lease_seconds must be an integer between 1 and 300")
    if (isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or not 0.05 <= float(poll_seconds) <= 60.0):
        raise ValueError("poll_seconds must be between 0.05 and 60")
    if max_ticks is not None and (type(max_ticks) is not int or max_ticks < 1):
        raise ValueError("max_ticks must be positive")

    counts = _empty_real_counts()
    status = "stopped"
    reason = "max_ticks"
    while not stop_event.is_set() and (max_ticks is None or counts["ticks"] < max_ticks):
        state = await tick_real_agent_worker(
            session_factory=session_factory,
            machine_token=machine_token,
            provider_factory=provider_factory,
            lease_seconds=lease_seconds,
        )
        counts["ticks"] += 1
        if state in counts:
            counts[state] += 1
        if state in _TERMINAL_REAL_TICK_STATES:
            status, reason = state, "terminal"
            break
        if max_ticks is None or counts["ticks"] < max_ticks:
            # A tick can return without any await (e.g. a pre-dispatch failure).
            # Yield on every iteration so signal callbacks can stop a busy queue
            # before another claim, and repeated failures never busy-spin.
            await _stop_wait(stop_event, float(poll_seconds))
    if reason == "max_ticks" and stop_event.is_set():
        reason = "signal"
    return {"status": status, "reason": reason, "counts": counts}
