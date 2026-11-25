"""Human-authorized purchasing evidence; no WeChat API, PO or inventory side effects."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.service import record_event
from backend.app.modules.catalog.models import CatalogProduct
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.knowledge.commands import execute_command
from backend.app.modules.purchasing.models import (PurchaseCommand, PurchaseEvidence,
                                                  PurchaseIntent, PurchaseIntentLine)
from backend.app.modules.purchasing.schemas import (PurchaseEvidenceOut, PurchaseIntentCreate,
                                                   PurchaseIntentOut, PurchaseLineConfirm,
                                                   PurchaseLineOut)

READ, WRITE = "purchasing.read", "purchasing.write"


class PurchaseError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise PurchaseError("purchase_time_invalid")
    return current.astimezone(timezone.utc)


def _require(session: Session, actor: AuthenticatedSubject, permission: str, now: datetime) -> None:
    session.flush()
    session.expire_all()
    # Keep the platform's actual session, organization, store and grant checks.
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission=permission, now=now)
    kind = session.scalar(select(Subject.subject_type).where(
        Subject.id == actor.subject_id, Subject.organization_id == actor.organization_id))
    if kind != "person":
        raise PurchaseError("purchase_human_required")


def _intent(session: Session, actor: AuthenticatedSubject, intent_id: str) -> PurchaseIntent:
    row = session.scalar(select(PurchaseIntent).where(
        PurchaseIntent.id == intent_id, PurchaseIntent.organization_id == actor.organization_id,
        PurchaseIntent.store_id == actor.store_id).execution_options(populate_existing=True))
    if row is None:
        raise PurchaseError("purchase_not_found")
    return row


def _out(session: Session, row: PurchaseIntent) -> dict:
    evidence = session.scalar(select(PurchaseEvidence).where(
        PurchaseEvidence.id == row.evidence_id,
        PurchaseEvidence.organization_id == row.organization_id,
        PurchaseEvidence.store_id == row.store_id))
    if evidence is None:
        raise PurchaseError("purchase_evidence_missing")
    lines = list(session.scalars(select(PurchaseIntentLine).where(
        PurchaseIntentLine.intent_id == row.id,
        PurchaseIntentLine.organization_id == row.organization_id,
        PurchaseIntentLine.store_id == row.store_id).order_by(PurchaseIntentLine.id)))
    return PurchaseIntentOut(
        id=row.id, organization_id=row.organization_id, store_id=row.store_id,
        creator_id=row.creator_id, status=row.status, version=row.version,
        created_at=row.created_at, updated_at=row.updated_at,
        evidence=PurchaseEvidenceOut.model_validate(evidence),
        lines=[PurchaseLineOut.model_validate(line) for line in lines],
    ).model_dump(mode="json")


def _audit(session: Session, actor: AuthenticatedSubject, action: str, row: PurchaseIntent,
           key: str, current: datetime, details: dict) -> None:
    # Never place raw WeChat or delivery-note text in the audit event.
    record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
                 store_id=actor.store_id, action=action, target_type="purchase_intent",
                 target_id=row.id, idempotency_key=key, occurred_at=current, details=details)


def create_intent(session: Session, actor: AuthenticatedSubject, payload: PurchaseIntentCreate,
                  *, idempotency_key: str, now: datetime | None = None) -> dict:
    current = _now(now)
    _require(session, actor, WRITE, current)
    if payload.consent_confirmed is not True:
        raise PurchaseError("purchase_consent_required")
    # Preserve source bytes as text; hash also binds the supplier and source kind.
    digest = hashlib.sha256(json.dumps(
        [payload.source_kind, payload.supplier_name, payload.original_text],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    command_payload = payload.model_dump(mode="json")

    def execute() -> dict:
        duplicate = session.scalar(select(PurchaseEvidence.id).where(
            PurchaseEvidence.organization_id == actor.organization_id,
            PurchaseEvidence.store_id == actor.store_id,
            PurchaseEvidence.content_sha256 == digest))
        if duplicate is not None:
            raise PurchaseError("purchase_duplicate_evidence")
        evidence = PurchaseEvidence(
            id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
            created_by=actor.subject_id, source_kind=payload.source_kind,
            supplier_name=payload.supplier_name, original_text=payload.original_text,
            content_sha256=digest, consent_confirmed=True, created_at=current)
        row = PurchaseIntent(
            id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
            evidence_id=evidence.id, creator_id=actor.subject_id, status="awaiting_arrival",
            version=1, created_at=current, updated_at=current)
        session.add_all([evidence, row])
        session.flush()
        for description in payload.line_descriptions:
            session.add(PurchaseIntentLine(
                id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
                intent_id=row.id, original_description=description,
                product_id=None, product_version=None, quantity=None,
                confirmed_by=None, confirmed_at=None))
        session.flush()
        _audit(session, actor, "purchase.intent_created", row,
               f"purchase:create:{row.id}", current,
               {"evidence_sha256": digest, "source_kind": payload.source_kind,
                "line_count": len(payload.line_descriptions), "version": 1})
        return _out(session, row)

    return execute_command(session, actor, operation="create", key=idempotency_key,
                           payload=command_payload, execute=execute,
                           receipt_model=PurchaseCommand)


def confirm_line(session: Session, actor: AuthenticatedSubject, intent_id: str, line_id: str,
                 payload: PurchaseLineConfirm, *, idempotency_key: str,
                 now: datetime | None = None) -> dict:
    current = _now(now)
    _require(session, actor, WRITE, current)
    command_payload = {"intent_id": intent_id, "line_id": line_id,
                       **payload.model_dump(mode="json")}

    def execute() -> dict:
        row = _intent(session, actor, intent_id)
        if row.version != payload.expected_version:
            raise PurchaseError("purchase_version_conflict")
        line = session.scalar(select(PurchaseIntentLine).where(
            PurchaseIntentLine.id == line_id, PurchaseIntentLine.intent_id == row.id,
            PurchaseIntentLine.organization_id == actor.organization_id,
            PurchaseIntentLine.store_id == actor.store_id))
        if line is None:
            raise PurchaseError("purchase_line_not_found")
        if line.confirmed_by is not None:
            raise PurchaseError("purchase_line_already_confirmed")
        product = session.scalar(select(CatalogProduct).where(
            CatalogProduct.id == payload.product_id,
            CatalogProduct.organization_id == actor.organization_id,
            CatalogProduct.store_id == actor.store_id,
            CatalogProduct.active.is_(True)))
        if product is None or product.version != payload.product_version:
            raise PurchaseError("purchase_product_version_conflict")
        # Explicit compare-and-swap. Never silently substitute the current version.
        changed = session.execute(update(PurchaseIntent).where(
            PurchaseIntent.id == row.id,
            PurchaseIntent.organization_id == actor.organization_id,
            PurchaseIntent.store_id == actor.store_id,
            PurchaseIntent.version == payload.expected_version).values(
                version=payload.expected_version + 1, updated_at=current
            ).execution_options(synchronize_session=False)).rowcount
        if changed != 1:
            raise PurchaseError("purchase_version_conflict")
        quantity = format(payload.quantity, "f")
        line.product_id = product.id
        line.product_version = product.version
        line.quantity = quantity
        line.confirmed_by = actor.subject_id
        line.confirmed_at = current
        session.flush()
        session.expire(row)
        session.refresh(row)
        _audit(session, actor, "purchase.line_confirmed", row,
               f"purchase:confirm:{row.id}:{line.id}:{row.version}", current,
               {"line_id": line.id, "product_id": product.id,
                "product_version": product.version, "quantity": quantity,
                "version": row.version})
        return _out(session, row)

    return execute_command(session, actor, operation="confirm_line", key=idempotency_key,
                           payload=command_payload, execute=execute,
                           receipt_model=PurchaseCommand)


def get_intent(session: Session, actor: AuthenticatedSubject, intent_id: str,
               *, now: datetime | None = None) -> dict:
    _require(session, actor, READ, _now(now))
    return _out(session, _intent(session, actor, intent_id))


def list_intents(session: Session, actor: AuthenticatedSubject,
                 *, now: datetime | None = None) -> list[dict]:
    _require(session, actor, READ, _now(now))
    rows = session.scalars(select(PurchaseIntent).where(
        PurchaseIntent.organization_id == actor.organization_id,
        PurchaseIntent.store_id == actor.store_id).order_by(
        PurchaseIntent.created_at.desc(), PurchaseIntent.id.desc()).limit(100))
    return [_out(session, row) for row in rows]
