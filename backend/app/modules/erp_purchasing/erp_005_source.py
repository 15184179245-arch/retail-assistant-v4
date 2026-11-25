"""Server receiving facts, isolated from formal ERP, stock and manual attestations."""
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from pydantic import ValidationError
from sqlalchemy import select, text

from backend.app.modules.auth.service import has_usable_account
from backend.app.modules.identity.command_guard import lock_employee_authority
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.catalog.models import CatalogProduct
from backend.app.modules.purchasing.models import PurchaseIntent, PurchaseIntentLine, PurchaseEvidence
from backend.app.modules.receiving.models import (ReceivingBatch, ReceivingLine,
    ReceivingDiscrepancy, ReceivingDecision)
from backend.app.modules.receiving import document_service as documents
from backend.app.modules.erp_commands.erp_003_wire import canonical_digest
from backend.app.modules.erp_purchasing.erp_005_schemas import (
    ReceiptIntent, ReceiptBatch, ReceiptSourceSnapshot, compute_cumulative,
)


class ERPReceiptError(ValueError):
    def __init__(self, code):
        self.code = 'erp_receipt_' + code
        super().__init__(self.code)


def fail(code):
    raise ERPReceiptError(code)


def clock(db):
    sql = "SELECT clock_timestamp()" if db.get_bind().dialect.name == 'postgresql' else "SELECT strftime('%Y-%m-%d %H:%M:%f','now')"
    value = db.execute(text(sql)).scalar_one()
    value = datetime.fromisoformat(value) if type(value) is str else value
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def scope(model, actor):
    return (model.organization_id == actor.organization_id, model.store_id == actor.store_id)


def require_person(db, actor, permission, now):
    if (not getattr(actor, 'account_id', None) or not getattr(actor, 'session_id', None)
            or type(getattr(actor, 'account_version', None)) is not int or actor.account_version <= 0):
        fail('human_required')
    authorize_scope(db, actor, organization_id=actor.organization_id, store_id=actor.store_id,
        permission=permission, now=now)
    if not has_usable_account(db, organization_id=actor.organization_id, store_id=actor.store_id,
            subject_id=actor.subject_id, now=now):
        fail('human_required')


def guard(db, actor, permission='erp.receiving.read'):
    if not getattr(actor, 'account_id', None) or not getattr(actor, 'session_id', None):
        fail('human_required')
    lock_employee_authority(db, actor)
    now = clock(db)
    for capability in ('receiving.read', 'purchasing.read', 'erp.command.read', 'erp.receiving.read', permission):
        require_person(db, actor, capability, now)
    return now


def _rows(db, model, actor, *conditions, maximum=100, order=()):
    rows = list(db.scalars(select(model).where(*scope(model, actor), *conditions)
        .order_by(*order).limit(maximum + 1).execution_options(populate_existing=True).with_for_update()))
    if len(rows) > maximum:
        fail('source_too_large')
    return rows


def _intent(db, actor, ident):
    found = _rows(db, PurchaseIntent, actor, PurchaseIntent.id == ident, maximum=1)
    if not found:
        fail('source_invalid')
    row = found[0]
    evidence = _rows(db, PurchaseEvidence, actor, PurchaseEvidence.id == row.evidence_id, maximum=1)
    if not evidence or not evidence[0].consent_confirmed:
        fail('source_invalid')
    lines = _rows(db, PurchaseIntentLine, actor, PurchaseIntentLine.intent_id == row.id,
        order=(PurchaseIntentLine.id,))
    return ReceiptIntent.model_validate(dict(id=row.id, version=row.version, status=row.status,
        evidence_id=row.evidence_id, evidence_sha256=evidence[0].content_sha256,
        supplier_name_claim=evidence[0].supplier_name,
        lines=[{k: getattr(line, k) for k in ('id', 'product_id', 'product_version', 'quantity',
               'confirmed_by', 'confirmed_at')} for line in lines]))


def _batch(db, actor, row, intent):
    if (row.status != 'reviewed' or row.confirmed_by is None or row.confirmed_at is None
            or row.created_by == row.confirmed_by):
        fail('batch_not_reviewed')
    lines = _rows(db, ReceivingLine, actor, ReceivingLine.batch_id == row.id,
        order=(ReceivingLine.line_key, ReceivingLine.id))
    diffs = _rows(db, ReceivingDiscrepancy, actor, ReceivingDiscrepancy.batch_id == row.id,
        maximum=500, order=(ReceivingDiscrepancy.id,))
    decisions = _rows(db, ReceivingDecision, actor, ReceivingDecision.batch_id == row.id,
        maximum=5000, order=(ReceivingDecision.batch_version, ReceivingDecision.id))
    latest = {d.discrepancy_id: d for d in decisions}
    if any(d.id not in latest or latest[d.id].decision != 'accept_observation' for d in diffs):
        fail('discrepancy_unresolved')
    if any(d.discrepancy_id not in {x.id for x in diffs} for d in decisions):
        fail('source_invalid')
    values = []
    originals = {x.id: x for x in intent.lines}
    for line in lines:
        original = originals.get(line.intent_line_id)
        if (line.unknown_product_label is not None or original is None
                or (line.product_id, line.product_version) != (original.product_id, original.product_version)):
            fail('product_binding_invalid')
        products = _rows(db, CatalogProduct, actor, CatalogProduct.id == line.product_id, maximum=1)
        if not products or not products[0].active or products[0].version != line.product_version:
            fail('product_changed')
        priced = (line.quoted_price, line.delivered_price, line.price_evidence_sha256)
        if any(v is not None for v in priced) and any(v is None for v in priced):
            fail('price_incomplete')
        if line.delivered_price is not None and Decimal(line.quoted_price) != Decimal(line.delivered_price):
            # Explicit acceptance of the ORIGINAL price discrepancy is required,
            # even if a damaged source falsely claims it was already reviewed.
            matches = [d for d in diffs if d.line_id == line.id and d.kind == 'price_change']
            expected = dict(quoted_price=line.quoted_price, delivered_price=line.delivered_price,
                price_proof_sha256=line.price_evidence_sha256, unit=line.unit,
                source_verification='employee_attested_not_erp')
            if len(matches) != 1 or matches[0].detail != expected:
                fail('price_decision_missing')
        values.append(dict(id=line.id, line_key=line.line_key, intent_line_id=line.intent_line_id,
            product_id=line.product_id, product_version=line.product_version, quantity=line.observed_quantity,
            unit=line.unit, final_for_intent_line=line.final_for_intent_line, evidence_sha256=line.evidence_sha256,
            quoted_price=line.quoted_price, delivered_price=line.delivered_price,
            price_evidence_sha256=line.price_evidence_sha256,
            line_total=None if line.delivered_price is None else format(
                documents.compute_line_total(line.observed_quantity, line.delivered_price), 'f'),
            observed_by=line.observed_by, observed_at=line.observed_at))
    return ReceiptBatch.model_validate(dict(id=row.id, version=row.version, intent_id=row.intent_id,
        expected_intent_version=row.expected_intent_version, status=row.status,
        delivery_note_sha256=row.delivery_note_sha256, created_by=row.created_by, confirmed_by=row.confirmed_by,
        created_at=row.created_at, confirmed_at=row.confirmed_at, lines=values,
        decisions=[dict(id=latest[d.id].id, discrepancy_id=d.id, line_id=d.line_id, kind=d.kind,
            detail_digest=canonical_digest(d.detail), batch_version=latest[d.id].batch_version,
            decision=latest[d.id].decision, reviewer_id=latest[d.id].reviewer_id,
            responsible_id=latest[d.id].responsible_id, evidence_sha256=latest[d.id].evidence_sha256,
            created_at=latest[d.id].created_at) for d in diffs]))


def snapshot(db, actor, batch_id):
    """Freeze the COMPLETE related-batch set under the employee/store fence.

    All siblings must be independently reviewed; otherwise their cumulative
    quantities are not yet eligible for a receiving command. Manual receiving
    remains available. CNY here is an explicit synthetic contract, not a newly
    inferred source currency, tax or supplier mapping.
    """
    guard(db, actor)
    current = _rows(db, ReceivingBatch, actor, ReceivingBatch.id == batch_id, maximum=1)
    if not current:
        fail('not_found')
    if current[0].intent_id is None:
        fail('intent_required')
    try:
        intent = _intent(db, actor, current[0].intent_id)
        related = _rows(db, ReceivingBatch, actor, ReceivingBatch.intent_id == intent.id,
            order=(ReceivingBatch.created_at, ReceivingBatch.id))
        batches = [_batch(db, actor, row, intent) for row in related]
        batch = next(b for b in batches if b.id == batch_id)
        if any(line.line_total is None for line in batch.lines):
            fail('price_incomplete')
        with localcontext() as context:
            context.prec = 80
            total = sum((Decimal(line.line_total) for line in batch.lines), Decimal(0))
        result = ReceiptSourceSnapshot(organization_id=actor.organization_id, store_id=actor.store_id,
            batch_id=batch.id, batch_version=batch.version, intent=intent, related_batches=batches,
            cumulative=compute_cumulative(intent, batches), currency='CNY',
            currency_source='synthetic_contract_only', total_amount=format(total, 'f'), effect='local_sandbox_only')
    except (ValidationError, documents.PurchaseDocumentError, ArithmeticError, ValueError, TypeError) as exc:
        if isinstance(exc, ERPReceiptError):
            raise
        fail('source_invalid')
    for permission in ('erp.receiving.read', 'receiving.read', 'purchasing.read', 'erp.command.read'):
        require_person(db, actor, permission, clock(db))
    return result


def source_digest(value):
    return canonical_digest(value.model_dump(mode='json'))
