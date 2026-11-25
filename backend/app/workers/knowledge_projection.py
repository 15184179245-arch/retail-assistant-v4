"""Outbox runner adapter for the global knowledge projection rebuild.

The projection never owns a queue of its own: it reuses the ordinary outbox
lease/CAS machinery and the shared ``run_once`` runner, so an expired lease can
never publish and a lost receipt is retried through the same idempotent event.
"""
from __future__ import annotations

from datetime import datetime

from backend.app.modules.knowledge import projection
from backend.app.workers import runner

KNOWLEDGE_PROJECTION_WORKER_ID = "worker-knowledge-projection"
KNOWLEDGE_PROJECTION_LEASE_SECONDS = 900


def handlers(session_factory, *, worker_id: str | None = None,
             window_limit: int = projection.WINDOW_LIMIT,
             now=None) -> dict:
    return {
        projection.EVENT_TYPE: projection.KnowledgeProjectionHandler(
            session_factory, worker_id=worker_id, window_limit=window_limit, now=now,
        )
    }


def run_projection_once(
    session_factory,
    *,
    worker_id: str = KNOWLEDGE_PROJECTION_WORKER_ID,
    now: datetime | None = None,
    max_attempts: int = 8,
    window_limit: int = projection.WINDOW_LIMIT,
    organization_id: str | None = None,
    store_id: str | None = None,
) -> str:
    """Claim at most one rebuild event and run it through the shared runner."""
    return runner.run_once(
        session_factory=session_factory,
        handlers=handlers(session_factory, worker_id=worker_id,
                          window_limit=window_limit, now=now),
        worker_id=worker_id,
        lease_seconds=KNOWLEDGE_PROJECTION_LEASE_SECONDS,
        max_attempts=max_attempts,
        now=now,
        event_types=(projection.EVENT_TYPE,),
        organization_id=organization_id,
        store_id=store_id,
    )


__all__ = [
    "KNOWLEDGE_PROJECTION_LEASE_SECONDS",
    "KNOWLEDGE_PROJECTION_WORKER_ID",
    "handlers",
    "run_projection_once",
]
