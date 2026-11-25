"""Transactional HTTP idempotency; never replay effects from an unknown receipt."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Callable
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.knowledge.models import KnowledgeCommand


def execute_command(session: Session, actor: AuthenticatedSubject, *, operation: str,
                    key: str, payload: dict, execute: Callable[[], dict],
                    receipt_model=KnowledgeCommand) -> dict:
    KnowledgeCommand = receipt_model
    # Authorization belongs to the command handler BEFORE this function, including
    # receipt replay. The caller owns commit/rollback of the outer transaction.
    if not key or not key.strip() or len(key) > 200:
        raise HTTPException(400, "idempotency_key_invalid")
    key = key.strip()
    digest = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    query = select(KnowledgeCommand).where(
        KnowledgeCommand.organization_id == actor.organization_id,
        KnowledgeCommand.store_id == actor.store_id,
        KnowledgeCommand.actor_id == actor.subject_id,
        KnowledgeCommand.operation == operation,
        KnowledgeCommand.idempotency_key == key,
    )

    def replay(receipt: KnowledgeCommand) -> dict:
        if receipt.payload_digest != digest:
            raise HTTPException(409, "idempotency_conflict")
        if not receipt.response:
            raise HTTPException(409, "knowledge_command_incomplete")
        return receipt.response

    existing = session.scalar(query)
    if existing is not None:
        return replay(existing)
    # sqlite3 legacy transaction mode otherwise releases a first SAVEPOINT as
    # a real commit. Keep reservation, effect, audit and receipt in one unit.
    connection = session.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
    try:
        with session.begin_nested():
            receipt = KnowledgeCommand(
                id=str(uuid4()), organization_id=actor.organization_id,
                store_id=actor.store_id, actor_id=actor.subject_id,
                operation=operation, idempotency_key=key, payload_digest=digest,
                response={}, created_at=datetime.now(timezone.utc),
            )
            session.add(receipt)
            session.flush()  # reserve before the domain effect
    except IntegrityError:
        existing = session.scalar(query)
        if existing is not None:
            return replay(existing)
        raise HTTPException(409, "knowledge_command_conflict") from None
    # The route's outer transaction rolls this reservation back on any exception.
    result = execute()
    receipt.response = result
    session.flush()
    return result
