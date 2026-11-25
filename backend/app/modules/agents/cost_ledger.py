"""Persistent monetary reservation, settlement and scoped cost queries (AGT-019).

Reservation happens once per run/step/call key before the provider call and is
committed together with an audit event. Settlement reuses the stored price
snapshot and the approved AGT-016 estimator; a missing or incomplete usage keeps
the reservation ``unknown`` (never released). Only an explicit pre-call
rejection releases a reservation. Amounts are exact decimal strings; prompts,
keys and raw provider payloads are never stored.

The four Worker cost write APIs (``reserve_cost``, ``settle_cost``,
``mark_cost_unknown``, ``release_cost``) accept ``commit: bool = True``
(AGT-026 B2a); ``resolve_reconciliation`` keeps its own self-committing
semantics. The default keeps the historical behavior: the cost layer commits its
own unit of work and, on a lost race, rolls back and re-reads before reporting
an idempotent replay. ``commit=False`` means the caller owns the surrounding
transaction: this module only flushes and never commits or rolls back. A
CAS/Integrity/Operational error is surfaced as :class:`CostLedgerError` so the
caller can roll back the whole guarded transaction and re-acquire its
authorization fence; a conflict is never converted into a success by re-reading
after a private rollback.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_CEILING, ROUND_HALF_UP, localcontext
import os
from pathlib import Path
import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents.cost_service import (
    NormalizedUsage, PriceBook, PriceVersion, estimate_cost, load_price_book,
)
from backend.app.modules.agents.models import AgentRun, AgentStep, BudgetPolicy, CostLedger
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope

_BOUNDABLE_DIMENSIONS = frozenset({"input", "output"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}\Z", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}\Z", re.ASCII)
_AMOUNT = re.compile(r"(?:0|[1-9][0-9]{0,29})(?:\.[0-9]{1,18})?\Z", re.ASCII)
_DEFAULT_PRICE_PATH = Path(__file__).resolve().parents[4] / "agents" / "specs" / "model-prices.json"


class CostLedgerError(RuntimeError):
    """Stable, non-leaking cost failure that callers map to a public code."""

    def __init__(self, code: str, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CostLedgerError("cost_ledger_conflict")
    return value.astimezone(timezone.utc)


def _now(value: datetime | None) -> datetime:
    return _utc(value) if value is not None else datetime.now(timezone.utc)


def _amount(value: object, code: str) -> Decimal:
    if not isinstance(value, str) or _AMOUNT.fullmatch(value) is None:
        raise CostLedgerError(code)
    try:
        amount = Decimal(value)
    except ArithmeticError:
        raise CostLedgerError(code) from None
    if not amount.is_finite() or amount < 0:
        raise CostLedgerError(code)
    return amount


def _formatted(value: Decimal) -> str:
    if not value.is_finite() or value < 0:
        raise CostLedgerError("cost_ledger_conflict")
    return format(value, "f")


def _price_book_path() -> Path:
    raw = os.environ.get("AGENT_PRICE_CONFIG_PATH", "")
    return Path(raw) if raw.strip() else _DEFAULT_PRICE_PATH


def _synthetic_prices_allowed() -> bool:
    return os.environ.get("AGENT_ALLOW_SYNTHETIC_PRICES", "") == "1"


def _configured_price_book() -> PriceBook:
    try:
        return load_price_book(_price_book_path())
    except (ValueError, OSError):
        raise CostLedgerError("cost_price_unconfigured") from None


def _select_price(book: PriceBook, run: AgentRun, at: datetime) -> PriceVersion:
    try:
        price = book.select(run.provider_key, run.model, at)
    except (ValueError, TypeError):
        raise CostLedgerError("cost_price_unconfigured") from None
    if price is None:
        raise CostLedgerError("cost_price_unconfigured")
    if price.provenance == "synthetic" and not _synthetic_prices_allowed():
        raise CostLedgerError("cost_price_unconfigured")
    return price


def _reservation_usage(price: PriceVersion, max_input_tokens: int,
                       max_output_tokens: int) -> NormalizedUsage:
    if (type(max_input_tokens) is not int or type(max_output_tokens) is not int
            or max_input_tokens <= 0 or max_output_tokens <= 0):
        raise CostLedgerError("cost_reservation_unbounded")
    if {name for name, _ in price.rates_per_million} != _BOUNDABLE_DIMENSIONS:
        raise CostLedgerError("cost_reservation_unbounded")
    return NormalizedUsage(price.usage_contract,
                           (("input", max_input_tokens), ("output", max_output_tokens)))


def _active_policy(session: Session, run: AgentRun, scope_type: str,
                   subject_id: str | None, price: PriceVersion, at: datetime) -> BudgetPolicy:
    statement = select(BudgetPolicy).where(
        BudgetPolicy.organization_id == run.organization_id,
        BudgetPolicy.store_id == run.store_id,
        BudgetPolicy.scope_type == scope_type,
        BudgetPolicy.active.is_(True),
        BudgetPolicy.effective_from <= at,
        or_(BudgetPolicy.effective_until.is_(None), BudgetPolicy.effective_until > at),
        BudgetPolicy.provider_key == price.provider,
        BudgetPolicy.model == price.model,
        BudgetPolicy.price_version == price.version,
    )
    if scope_type == "employee":
        statement = statement.where(BudgetPolicy.subject_id == subject_id)
    else:
        statement = statement.where(BudgetPolicy.subject_id.is_(None))
    statement = statement.order_by(BudgetPolicy.effective_from.desc())
    # Always refresh an already-loaded policy: reserve/settle bump its version
    # with synchronize_session=False inside the same service Session.
    policy = session.scalar(statement.execution_options(populate_existing=True))
    if policy is None:
        raise CostLedgerError("cost_budget_unconfigured")
    if not isinstance(policy.currency, str) or _CURRENCY.fullmatch(policy.currency) is None:
        raise CostLedgerError("cost_budget_unconfigured")
    return policy


def _limit(policy: BudgetPolicy) -> Decimal | None:
    """Only the explicit policy value 'unlimited' disables a monetary ceiling."""
    return None if policy.limit_amount == 'unlimited' else _amount(policy.limit_amount, "cost_budget_unconfigured")


def _per_call_limit(policy: BudgetPolicy) -> Decimal | None:
    return None if policy.per_call_limit == 'unlimited' else _amount(policy.per_call_limit, "cost_budget_unconfigured")


def _exceeds(amount: Decimal, ceiling: Decimal | None) -> bool:
    return ceiling is not None and amount > ceiling


def _ceiling_amount(estimate) -> Decimal:
    """Reservation/settlement share one conservative 6-place ceiling rule."""
    raw = estimate.unrounded_amount
    if raw is None:
        raise CostLedgerError("cost_reservation_unbounded")
    with localcontext(Context(prec=80, rounding=ROUND_CEILING)):
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)


def _row_occupancy(row: CostLedger) -> Decimal:
    if row.status == "released":
        return Decimal(0)
    if row.status == "settled":
        return _amount(row.estimated_amount, "cost_ledger_conflict")
    if row.status in ("reserved", "unknown"):
        return _amount(row.reserved_amount, "cost_ledger_conflict")
    if row.status == "reconciliation_required":
        reserved = _amount(row.reserved_amount, "cost_ledger_conflict")
        estimated = _amount(row.estimated_amount, "cost_ledger_conflict")
        return max(reserved, estimated)
    raise CostLedgerError("cost_ledger_conflict")


def _sum_amounts(amounts: Iterable[Decimal]) -> Decimal:
    """Sum validated finite amounts exactly without the ambient Decimal context."""
    total = places = 0
    for amount in amounts:
        sign, digits, exponent = amount.as_tuple()
        scale = -exponent
        if scale > places:
            total *= 10 ** (scale - places)
            places = scale
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        total += (-coefficient if sign else coefficient) * 10 ** (places - scale)
    sign, digits, _ = Decimal(total).as_tuple()
    return Decimal((sign, digits, -places))


def _spent(session: Session, policy: BudgetPolicy, *, exclude_id: str | None = None) -> Decimal:
    """Sum only rows bound to this exact scoped policy and currency."""
    statement = select(CostLedger).where(
        CostLedger.organization_id == policy.organization_id,
        CostLedger.store_id == policy.store_id,
        CostLedger.currency == policy.currency,
        CostLedger.created_at >= policy.effective_from,
    )
    if policy.effective_until is not None:
        statement = statement.where(CostLedger.created_at < policy.effective_until)
    if policy.scope_type == "employee":
        statement = statement.where(CostLedger.employee_budget_id == policy.id)
    else:
        statement = statement.where(CostLedger.store_budget_id == policy.id)
    if exclude_id is not None:
        statement = statement.where(CostLedger.id != exclude_id)
    return _sum_amounts(_row_occupancy(row) for row in session.scalars(statement).all())


def _reconciliation_blocked(session: Session, policy: BudgetPolicy) -> bool:
    column = (CostLedger.employee_budget_id if policy.scope_type == "employee"
              else CostLedger.store_budget_id)
    return session.scalar(select(CostLedger.id).where(
        CostLedger.organization_id == policy.organization_id,
        CostLedger.store_id == policy.store_id,
        CostLedger.currency == policy.currency,
        column == policy.id,
        CostLedger.status == "reconciliation_required",
    ).limit(1)) is not None


def _policy_by_id(session: Session, ledger: CostLedger, policy_id: str) -> BudgetPolicy | None:
    return session.scalar(select(BudgetPolicy).where(
        BudgetPolicy.organization_id == ledger.organization_id,
        BudgetPolicy.store_id == ledger.store_id,
        BudgetPolicy.id == policy_id,
    ).execution_options(populate_existing=True))


def _bump_policy(session: Session, policy: BudgetPolicy, at: datetime) -> None:
    """Serialize concurrent reservations on the scoped policy rows."""
    result = session.execute(
        update(BudgetPolicy)
        .where(
            BudgetPolicy.organization_id == policy.organization_id,
            BudgetPolicy.store_id == policy.store_id,
            BudgetPolicy.id == policy.id,
            BudgetPolicy.version == policy.version,
            BudgetPolicy.active.is_(True),
        )
        .values(version=policy.version + 1, updated_at=at)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise CostLedgerError("cost_ledger_conflict")
    # Keep the identity-mapped policy in step with the DB without marking it
    # dirty (a later reserve in the same Session must read the bumped version).
    set_committed_value(policy, "version", policy.version + 1)
    set_committed_value(policy, "updated_at", at)


def _rollback_if_owner(session: Session, commit: bool) -> None:
    """Roll back only when the cost layer owns the transaction (``commit=True``)."""
    if commit:
        session.rollback()


def _find_ledger(session: Session, organization_id: str, store_id: str,
                 run_id: str, step_id: str, call_key: str, *,
                 commit: bool = True) -> CostLedger | None:
    try:
        return session.scalar(select(CostLedger).where(
            CostLedger.organization_id == organization_id,
            CostLedger.store_id == store_id,
            CostLedger.run_id == run_id,
            CostLedger.step_id == step_id,
            CostLedger.call_key == call_key,
        ).execution_options(populate_existing=True))
    except OperationalError:
        _rollback_if_owner(session, commit)
        raise CostLedgerError("cost_ledger_conflict") from None


def _price_snapshot(price: PriceVersion) -> dict:
    effective_from = _utc(price.effective_from)
    effective_until = _utc(price.effective_until)
    verified_at = _utc(price.verified_at)
    return {
        "provider": price.provider, "model": price.model, "version": price.version,
        "currency": price.currency, "usage_contract": price.usage_contract,
        "effective_from": effective_from.isoformat(), "effective_until":
            effective_until.isoformat() if effective_until is not None else None,
        "rates_per_million": dict(price.rates_per_million),
        "price_source": price.price_source, "usage_source": price.usage_source,
        "verified_at": verified_at.isoformat(), "provenance": price.provenance,
    }


def _price_from_snapshot(snapshot: object) -> PriceVersion:
    keys = {"provider", "model", "version", "currency", "usage_contract", "effective_from",
            "effective_until", "rates_per_million", "price_source", "usage_source",
            "verified_at", "provenance"}
    if not isinstance(snapshot, dict) or set(snapshot) != keys:
        raise CostLedgerError("cost_ledger_conflict")
    try:
        rates = snapshot["rates_per_million"]
        if not isinstance(rates, dict):
            raise ValueError
        parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
        values = {
            "provider": snapshot["provider"], "model": snapshot["model"],
            "version": snapshot["version"], "currency": snapshot["currency"],
            "usage_contract": snapshot["usage_contract"],
            "effective_from": parse(snapshot["effective_from"]),
            "effective_until": (parse(snapshot["effective_until"])
                                if snapshot["effective_until"] is not None else None),
            "rates_per_million": tuple(sorted(rates.items())),
            "price_source": snapshot["price_source"],
            "usage_source": snapshot["usage_source"],
            "verified_at": parse(snapshot["verified_at"]),
            "provenance": snapshot["provenance"],
        }
        return PriceVersion(**values)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CostLedgerError("cost_ledger_conflict") from None


def _usage_snapshot(usage: NormalizedUsage | None) -> dict | None:
    if usage is None:
        return None
    return {"contract": usage.contract,
            "quantities": [[name, count] for name, count in usage.quantities]}


def _audit(session: Session, actor: AuthenticatedSubject, ledger: CostLedger,
           action: str, details: dict, at: datetime) -> None:
    key = f"agent_cost:{ledger.run_id}:{ledger.step_id}:{ledger.id}:{action}"
    record_event(session, actor_id=actor.subject_id,
                 organization_id=ledger.organization_id, store_id=ledger.store_id,
                 action=action, target_type="agent_cost", target_id=ledger.id,
                 idempotency_key=key, details=details, occurred_at=at)


def _reservation_details(ledger: CostLedger, reason: str | None = None) -> dict:
    return {
        "run_id": ledger.run_id, "step_id": ledger.step_id,
        "call_key": ledger.call_key, "provider_key": ledger.provider_key,
        "model": ledger.model, "price_version": ledger.price_version,
        "currency": ledger.currency, "reserved_amount": ledger.reserved_amount,
        "estimated_amount": ledger.estimated_amount, "status": ledger.status,
        "reason": reason, "error_code": ledger.error_code,
    }


def reserve_cost(session: Session, actor: AuthenticatedSubject, run: AgentRun,
                 step: AgentStep, *, max_input_tokens: int, max_output_tokens: int,
                 price_book: PriceBook | None = None,
                 now: datetime | None = None,
                 commit: bool = True) -> CostLedger:
    """Persist one bounded reservation (and its audit) in a committed transaction.

    ``commit=False`` stages the reservation, policy version bumps and audit in
    the caller's transaction; the caller commits or rolls back the whole unit.
    """
    if (not isinstance(run, AgentRun) or not isinstance(step, AgentStep)
            or (step.organization_id, step.store_id, step.run_id)
            != (run.organization_id, run.store_id, run.id)):
        raise CostLedgerError("cost_ledger_conflict")
    at = _now(now)
    call_key = f"{run.id}:{step.sequence_no}"
    existing = _find_ledger(session, run.organization_id, run.store_id, run.id,
                            step.id, call_key, commit=commit)
    if existing is not None:
        if existing.provider_key != run.provider_key or existing.model != run.model:
            raise CostLedgerError("cost_ledger_conflict")
        return existing

    try:
        book = price_book if price_book is not None else _configured_price_book()
        price = _select_price(book, run, at)
        employee = _active_policy(session, run, "employee", run.requester_id, price, at)
        store = _active_policy(session, run, "store", None, price, at)
        if price.currency != employee.currency or price.currency != store.currency:
            raise CostLedgerError("cost_currency_mismatch")
        if _reconciliation_blocked(session, employee) or _reconciliation_blocked(session, store):
            raise CostLedgerError("cost_budget_exceeded")
        usage = _reservation_usage(price, max_input_tokens, max_output_tokens)
        estimate = estimate_cost(PriceBook((price,)), provider=run.provider_key,
                                 model=run.model, called_at=at, usage=usage,
                                 allow_synthetic=True)
        if estimate.status != "estimated" or estimate.unrounded_amount is None:
            raise CostLedgerError("cost_reservation_unbounded")
        # Reserve the ceiling of the exact unrounded maximum, never the rounded
        # estimate: a tiny positive rate must not reserve zero.
        reserved = _ceiling_amount(estimate)
        if _exceeds(reserved, _per_call_limit(employee)) or _exceeds(reserved, _per_call_limit(store)):
            raise CostLedgerError("cost_budget_exceeded")
        if (_exceeds(_sum_amounts((_spent(session, employee), reserved)), _limit(employee))
                or _exceeds(_sum_amounts((_spent(session, store), reserved)), _limit(store))):
            raise CostLedgerError("cost_budget_exceeded")

        # Conditional version bumps are the cross-transaction admission gate:
        # a racing reservation either observes the new version (and recounts)
        # or fails closed with cost_ledger_conflict.
        _bump_policy(session, employee, at)
        _bump_policy(session, store, at)
        if (_exceeds(_sum_amounts((_spent(session, employee), reserved)), _limit(employee))
                or _exceeds(_sum_amounts((_spent(session, store), reserved)), _limit(store))):
            raise CostLedgerError("cost_budget_exceeded")

        ledger = CostLedger(
            id=str(uuid4()), organization_id=run.organization_id,
            store_id=run.store_id, run_id=run.id, step_id=step.id,
            call_key=call_key, requester_id=run.requester_id,
            provider_key=run.provider_key, model=run.model,
            price_version=price.version, currency=price.currency,
            reserved_amount=_formatted(reserved), estimated_amount=None,
            status="reserved", reserved_usage=_usage_snapshot(usage),
            usage_snapshot=None, price_snapshot=_price_snapshot(price),
            employee_budget_id=employee.id, store_budget_id=store.id,
            error_code=None, created_at=at, updated_at=at, settled_at=None,
        )
        session.add(ledger)
        session.flush()
        _audit(session, actor, ledger, "agent_cost.reserved",
               _reservation_details(ledger), at)
        if not commit:
            # Caller-owned transaction: flush only, never commit. The caller
            # returns the attached row after its own commit/rollback decision.
            session.flush()
            return ledger
        session.commit()
        return _find_ledger(session, run.organization_id, run.store_id,
                            run.id, step.id, call_key) or ledger
    except CostLedgerError:
        _rollback_if_owner(session, commit)
        raise
    except IntegrityError:
        if not commit:
            # A unique-key race is a conflict, not a replay. Never roll back
            # (the caller owns the transaction) and never re-read a success.
            raise CostLedgerError("cost_ledger_conflict") from None
        session.rollback()
        existing = _find_ledger(session, run.organization_id, run.store_id,
                                run.id, step.id, call_key)
        if existing is not None:
            return existing
        raise CostLedgerError("cost_ledger_conflict") from None
    except OperationalError:
        _rollback_if_owner(session, commit)
        raise CostLedgerError("cost_ledger_conflict") from None


def _usage_from_tokens(price: PriceVersion, input_tokens: int | None,
                       output_tokens: int | None) -> NormalizedUsage | None:
    if (type(input_tokens) is not int or type(output_tokens) is not int
            or input_tokens < 0 or output_tokens < 0):
        return None
    if {name for name, _ in price.rates_per_million} != _BOUNDABLE_DIMENSIONS:
        return None
    try:
        return NormalizedUsage(price.usage_contract,
                               (("input", input_tokens), ("output", output_tokens)))
    except ValueError:
        return None


def _mark_unknown_locked(session: Session, actor: AuthenticatedSubject,
                         ledger: CostLedger, reason: str, at: datetime, *,
                         commit: bool = True) -> CostLedger:
    if ledger.status == "unknown":
        return ledger
    if ledger.status != "reserved":
        return ledger
    result = session.execute(
        update(CostLedger)
        .where(
            CostLedger.organization_id == ledger.organization_id,
            CostLedger.store_id == ledger.store_id,
            CostLedger.id == ledger.id,
            CostLedger.status == "reserved",
        )
        .values(status="unknown", error_code=reason, updated_at=at, settled_at=at)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        if commit:
            session.rollback()
            current = _find_ledger(session, ledger.organization_id, ledger.store_id,
                                   ledger.run_id, ledger.step_id, ledger.call_key)
            if current is not None:
                return current
        # Managed mode: keep the caller's fence/transaction; the whole
        # transaction must be rolled back and re-guarded by the caller.
        raise CostLedgerError("cost_ledger_conflict")
    ledger.status = "unknown"
    ledger.error_code = reason
    ledger.updated_at = at
    ledger.settled_at = at
    _audit(session, actor, ledger, "agent_cost.unknown",
           _reservation_details(ledger, reason), at)
    if not commit:
        session.flush()
        return ledger
    session.commit()
    return _find_ledger(session, ledger.organization_id, ledger.store_id,
                        ledger.run_id, ledger.step_id, ledger.call_key) or ledger


def mark_cost_unknown(session: Session, actor: AuthenticatedSubject, run: AgentRun,
                      step: AgentStep, *, reason: str,
                      now: datetime | None = None,
                      commit: bool = True) -> CostLedger | None:
    """Keep the reservation occupied after timeout/cancel/missing usage."""
    if not isinstance(reason, str) or not _IDENTIFIER.fullmatch(reason):
        raise CostLedgerError("cost_ledger_conflict")
    at = _now(now)
    ledger = _find_ledger(session, run.organization_id, run.store_id, run.id,
                          step.id, f"{run.id}:{step.sequence_no}", commit=commit)
    if ledger is None:
        return None
    if ledger.status in ("settled", "released", "reconciliation_required"):
        return ledger
    try:
        return _mark_unknown_locked(session, actor, ledger, reason, at,
                                    commit=commit)
    except CostLedgerError:
        _rollback_if_owner(session, commit)
        raise
    except (IntegrityError, OperationalError):
        _rollback_if_owner(session, commit)
        raise CostLedgerError("cost_ledger_conflict") from None


def _lock_budgets(session: Session, ledger: CostLedger, at: datetime,
                  estimated: Decimal) -> bool:
    """CAS both scoped policies in fixed employee/store order during settlement.

    The version bump fences a concurrent reservation: either it observes the
    settle-side bump and recounts, or this settle fails closed with
    ``cost_ledger_conflict``. PostgreSQL row locking remains an OPS-001 gate.
    """
    over = False
    for policy_id in (ledger.employee_budget_id, ledger.store_budget_id):
        if policy_id is None:
            over = True
            continue
        policy = _policy_by_id(session, ledger, policy_id)
        if policy is None:
            over = True
            continue
        _bump_policy(session, policy, at)
        try:
            limit = _limit(policy)
        except CostLedgerError:
            over = True
            continue
        if _exceeds(_sum_amounts((_spent(session, policy, exclude_id=ledger.id), estimated)), limit):
            over = True
    return over


def _matches_settled_usage(ledger: CostLedger | None, amount: str, usage: dict | None) -> bool:
    if (ledger is None or ledger.status not in ("settled", "reconciliation_required")
            or ledger.estimated_amount != amount or usage is None):
        return False
    saved = ledger.usage_snapshot
    # Equality alone would equate JSON booleans/floats with integer token counts.
    return saved == usage and all(type(count) is int for _, count in saved["quantities"])


def settle_cost(session: Session, actor: AuthenticatedSubject, run: AgentRun,
                step: AgentStep, *, input_tokens: int | None,
                output_tokens: int | None,
                now: datetime | None = None,
                commit: bool = True) -> CostLedger | None:
    """Settle exactly once with the stored price snapshot; never release on unknown.

    ``commit=False`` leaves the settlement, policy fences and audit in the
    caller's transaction; unknown usage is also staged through the managed
    private helper instead of being committed here. Driver-level errors on that
    staging path (busy lock, integrity race) surface as ``CostLedgerError`` and,
    in managed mode, never trigger a private rollback or re-read.
    """
    at = _now(now)
    ledger = _find_ledger(session, run.organization_id, run.store_id, run.id,
                          step.id, f"{run.id}:{step.sequence_no}", commit=commit)
    if ledger is None:
        raise CostLedgerError("cost_ledger_conflict")
    if ledger.status in ("unknown", "released"):
        raise CostLedgerError("cost_ledger_conflict")
    price = _price_from_snapshot(ledger.price_snapshot)
    usage = _usage_from_tokens(price, input_tokens, output_tokens)
    estimate = estimate_cost(PriceBook((price,)), provider=ledger.provider_key,
                             model=ledger.model, called_at=_utc(ledger.created_at),
                             usage=usage, allow_synthetic=True)
    if estimate.status != "estimated" or estimate.unrounded_amount is None:
        # Missing/partial usage stays ``unknown`` through the shared private
        # helper. Its UPDATE, audit and flush run in this call stack, so a
        # driver-level error (busy lock, integrity race) must be translated to
        # the contract error here instead of leaking a raw SQLAlchemy exception.
        try:
            return _mark_unknown_locked(session, actor, ledger,
                                        "cost_usage_unknown", at, commit=commit)
        except CostLedgerError:
            _rollback_if_owner(session, commit)
            raise
        except (IntegrityError, OperationalError):
            # ``commit=False``: no private rollback and no re-read; the caller
            # owns the transaction and must roll back and re-guard itself.
            _rollback_if_owner(session, commit)
            raise CostLedgerError("cost_ledger_conflict") from None
    # Settlement must not fall below the conservative reservation rule: a tiny
    # positive rate settles at 0.000001, never 0.000000, so budget is not released.
    estimated = _ceiling_amount(estimate)
    formatted = _formatted(estimated)
    snapshot = _usage_snapshot(usage)

    if ledger.status in ("settled", "reconciliation_required"):
        if _matches_settled_usage(ledger, formatted, snapshot):
            return ledger
        raise CostLedgerError("cost_ledger_conflict")
    if ledger.status != "reserved":
        raise CostLedgerError("cost_ledger_conflict")

    try:
        over_reserved = estimated > _amount(ledger.reserved_amount, "cost_ledger_conflict")
        over_budget = _lock_budgets(session, ledger, at, estimated) or over_reserved
        status = "reconciliation_required" if over_budget else "settled"
        error_code = "cost_budget_exceeded" if over_budget else None
        result = session.execute(
            update(CostLedger)
            .where(
                CostLedger.organization_id == ledger.organization_id,
                CostLedger.store_id == ledger.store_id,
                CostLedger.id == ledger.id,
                CostLedger.status == "reserved",
            )
            .values(status=status, estimated_amount=formatted,
                    usage_snapshot=snapshot, error_code=error_code,
                    updated_at=at, settled_at=at)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            if commit:
                session.rollback()
                current = _find_ledger(session, ledger.organization_id, ledger.store_id,
                                       ledger.run_id, ledger.step_id, ledger.call_key)
                if _matches_settled_usage(current, formatted, snapshot):
                    return current
            # Managed mode: the CAS lost inside the caller's transaction. Fail
            # closed instead of rolling back and re-reading a fake success.
            raise CostLedgerError("cost_ledger_conflict")
        ledger.status = status
        ledger.estimated_amount = formatted
        ledger.usage_snapshot = snapshot
        ledger.error_code = error_code
        ledger.updated_at = at
        ledger.settled_at = at
        _audit(session, actor, ledger,
               "agent_cost.reconciliation_required" if over_budget else "agent_cost.settled",
               _reservation_details(ledger), at)
        if not commit:
            session.flush()
            return ledger
        session.commit()
        return _find_ledger(session, ledger.organization_id, ledger.store_id,
                            ledger.run_id, ledger.step_id, ledger.call_key) or ledger
    except CostLedgerError:
        _rollback_if_owner(session, commit)
        raise
    except (IntegrityError, OperationalError):
        _rollback_if_owner(session, commit)
        raise CostLedgerError("cost_ledger_conflict") from None


def reconcile_pi_provider_cost(session: Session, actor: AuthenticatedSubject,
                               run: AgentRun, step: AgentStep, *, receipt_id: str,
                               response_digest: str, now: datetime | None = None) -> CostLedger:
    """Receipt-bound H1 known-bill settlement in the caller's guarded root.

    Never commits, rolls back, authorizes execution, changes a price snapshot,
    or resurrects a released bill. Generic settle_cost still rejects unknown.
    The Host stages its response first; flush + a fresh scoped DB query are
    mandatory, so an unpersisted receipt/message/identity-map flag cannot bill.
    """
    from backend.app.modules.agents.models import AgentPiProviderReceipt, AgentExecution
    from backend.app.modules.agents import repository
    if (not isinstance(run, AgentRun) or not isinstance(step, AgentStep)
            or (step.organization_id,step.store_id,step.run_id) != (run.organization_id,run.store_id,run.id)
            or (actor.organization_id,actor.store_id,actor.subject_id)
                != (run.organization_id,run.store_id,run.requester_id)
            or type(response_digest) is not str or re.fullmatch(r'[0-9a-f]{64}',response_digest) is None):
        raise CostLedgerError('cost_ledger_conflict')
    at = _now(now)
    try:
        session.flush()
        # Read SQL values, not potentially stale/forged detached ORM objects.
        receipt = session.execute(select(AgentPiProviderReceipt.__table__).where(
            AgentPiProviderReceipt.id == receipt_id,
            AgentPiProviderReceipt.organization_id == run.organization_id,
            AgentPiProviderReceipt.store_id == run.store_id,
            AgentPiProviderReceipt.run_id == run.id,
            AgentPiProviderReceipt.model_step_id == step.id)).mappings().one_or_none()
        db_run = session.execute(select(AgentRun.__table__).where(
            AgentRun.organization_id == run.organization_id,AgentRun.store_id == run.store_id,
            AgentRun.id == run.id)).mappings().one_or_none()
        db_step = session.execute(select(AgentStep.__table__).where(
            AgentStep.organization_id == run.organization_id,AgentStep.store_id == run.store_id,
            AgentStep.run_id == run.id,AgentStep.id == step.id)).mappings().one_or_none()
        execution = session.scalar(select(AgentExecution).where(
            AgentExecution.organization_id == run.organization_id,AgentExecution.store_id == run.store_id,
            AgentExecution.run_id == run.id))
        context = execution.frozen_context if execution is not None else None
        if (receipt is None or db_run is None or db_step is None or type(context) is not dict
                or db_step['kind'] != 'model' or step.kind != 'model'
                or receipt['state'] != 'responded' or receipt['send_started'] is not True
                or receipt['response_digest'] != response_digest
                or receipt['model'] != 'deepseek-v4.1-flash'
                or run.model != db_run['model'] or receipt['model'] != db_run['model']
                or receipt['toolset_version'] != db_run['toolset_version']
                or db_run['requester_id'] != actor.subject_id):
            raise CostLedgerError('cost_ledger_conflict')
        record = repository.validate_pi_runtime_record(context.get('pi_runtime_record'))
        if (record['contract_version'] not in (2, 3) or record['source_manifest_sha256'] != receipt['manifest_digest']
                or record['wire_protocol'] != receipt['wire_protocol']):
            raise CostLedgerError('cost_ledger_conflict')
        dims = [receipt[k] for k in ('usage_input','usage_output','cache_read','cache_write')]
        if any(type(v) is not int or not 0 <= v <= 9007199254740991 for v in dims) or dims[3] != 0:
            raise CostLedgerError('cost_ledger_conflict')
        ledger = _find_ledger(session,run.organization_id,run.store_id,run.id,step.id,
                              f"{run.id}:{db_step['sequence_no']}",commit=False)
        if (ledger is None or ledger.provider_key != db_run['provider_key'] or ledger.model != receipt['model']
                or ledger.requester_id != actor.subject_id):
            raise CostLedgerError('cost_ledger_conflict')
        price = _price_from_snapshot(ledger.price_snapshot)
        if (price.provider != ledger.provider_key or price.model != ledger.model
                or price.version != ledger.price_version or price.currency != ledger.currency
                or price.usage_contract != 'disjoint-v1'):
            raise CostLedgerError('cost_ledger_conflict')
        # Aggregate every prompt dimension at the immutable noncached rate.
        usage = _usage_from_tokens(price,dims[0]+dims[2]+dims[3],dims[1])
        estimate = estimate_cost(PriceBook((price,)),provider=ledger.provider_key,model=ledger.model,
            called_at=_utc(ledger.created_at),usage=usage,allow_synthetic=True)
        if estimate.status != 'estimated' or estimate.unrounded_amount is None:
            raise CostLedgerError('cost_ledger_conflict')
        estimated = _ceiling_amount(estimate)
        formatted,snapshot = _formatted(estimated),_usage_snapshot(usage)
        if ledger.status in ('settled','reconciliation_required'):
            if ledger.estimated_amount != formatted or ledger.usage_snapshot != snapshot:
                raise CostLedgerError('cost_ledger_conflict')
            return ledger
        if ledger.status == 'reserved':
            return settle_cost(session,actor,run,step,input_tokens=dims[0]+dims[2]+dims[3],
                output_tokens=dims[1],now=at,commit=False)
        if ledger.status != 'unknown':
            raise CostLedgerError('cost_ledger_conflict')
        over = _lock_budgets(session,ledger,at,estimated) or estimated > _amount(ledger.reserved_amount,'cost_ledger_conflict')
        status = 'reconciliation_required' if over else 'settled'
        result = session.execute(update(CostLedger).where(
            CostLedger.organization_id == ledger.organization_id,CostLedger.store_id == ledger.store_id,
            CostLedger.id == ledger.id,CostLedger.status == 'unknown',
        ).values(status=status,estimated_amount=formatted,usage_snapshot=snapshot,
            error_code='cost_budget_exceeded' if over else None,updated_at=at,settled_at=at)
            .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise CostLedgerError('cost_ledger_conflict')
        for key,value in dict(status=status,estimated_amount=formatted,usage_snapshot=snapshot,
                error_code='cost_budget_exceeded' if over else None,updated_at=at,settled_at=at).items():
            set_committed_value(ledger,key,value)
        _audit(session,actor,ledger,'agent_cost.pi_provider_reconciled',
            {**_reservation_details(ledger),'receipt_id':receipt_id,'response_digest':response_digest,
             'estimate_basis':'uncached_prompt_upper_bound'},at)
        session.flush()
        return ledger
    except (repository.AgentPersistenceError, IntegrityError, OperationalError):
        raise CostLedgerError('cost_ledger_conflict') from None


def resolve_reconciliation(session: Session, actor: AuthenticatedSubject,
                          ledger: CostLedger, *, reason: str,
                          now: datetime | None = None) -> CostLedger:
    """Explicit, audited resolution of one over-reservation reconciliation.

    Internal operation (no HTTP route): a human/operator reviews the overrun and
    records the decision. The settled amount stays counted against the budget;
    clearing reconciliation_required re-opens admission for that policy.
    """
    if not isinstance(reason, str) or not _IDENTIFIER.fullmatch(reason):
        raise CostLedgerError("cost_ledger_conflict")
    if not isinstance(ledger, CostLedger) or ledger.status != "reconciliation_required":
        raise CostLedgerError("cost_ledger_conflict")
    at = _now(now)
    result = session.execute(
        update(CostLedger)
        .where(
            CostLedger.organization_id == ledger.organization_id,
            CostLedger.store_id == ledger.store_id,
            CostLedger.id == ledger.id,
            CostLedger.status == "reconciliation_required",
        )
        .values(status="settled", error_code=None, updated_at=at)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        session.rollback()
        raise CostLedgerError("cost_ledger_conflict")
    ledger.status = "settled"
    ledger.error_code = None
    ledger.updated_at = at
    _audit(session, actor, ledger, "agent_cost.reconciliation_resolved",
           _reservation_details(ledger, reason), at)
    session.commit()
    return _find_ledger(session, ledger.organization_id, ledger.store_id,
                        ledger.run_id, ledger.step_id, ledger.call_key) or ledger


def release_cost(session: Session, actor: AuthenticatedSubject, run: AgentRun,
                 step: AgentStep, *, reason: str,
                 now: datetime | None = None,
                 commit: bool = True) -> CostLedger | None:
    """Release only a reservation that was rejected before any provider call.

    ``commit=False`` stages the release and its audit in the caller's
    transaction; a lost CAS raises instead of being re-read as a success.
    """
    if not isinstance(reason, str) or not _IDENTIFIER.fullmatch(reason):
        raise CostLedgerError("cost_ledger_conflict")
    at = _now(now)
    ledger = _find_ledger(session, run.organization_id, run.store_id, run.id,
                          step.id, f"{run.id}:{step.sequence_no}", commit=commit)
    if ledger is None:
        return None
    if ledger.status == "released":
        return ledger
    if ledger.status != "reserved":
        raise CostLedgerError("cost_ledger_conflict")
    try:
        result = session.execute(
            update(CostLedger)
            .where(
                CostLedger.organization_id == ledger.organization_id,
                CostLedger.store_id == ledger.store_id,
                CostLedger.id == ledger.id,
                CostLedger.status == "reserved",
            )
            .values(status="released", error_code=reason, updated_at=at, settled_at=at)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            if commit:
                session.rollback()
                current = _find_ledger(session, ledger.organization_id, ledger.store_id,
                                       ledger.run_id, ledger.step_id, ledger.call_key)
                if current is not None and current.status == "released":
                    return current
            raise CostLedgerError("cost_ledger_conflict")
        ledger.status = "released"
        ledger.error_code = reason
        ledger.updated_at = at
        ledger.settled_at = at
        _audit(session, actor, ledger, "agent_cost.released",
               _reservation_details(ledger, reason), at)
        if not commit:
            session.flush()
            return ledger
        session.commit()
        return _find_ledger(session, ledger.organization_id, ledger.store_id,
                            ledger.run_id, ledger.step_id, ledger.call_key) or ledger
    except CostLedgerError:
        _rollback_if_owner(session, commit)
        raise
    except (IntegrityError, OperationalError):
        _rollback_if_owner(session, commit)
        raise CostLedgerError("cost_ledger_conflict") from None


def _permission_allowed(session: Session, actor: AuthenticatedSubject, permission: str) -> bool:
    try:
        authorize_scope(session, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission=permission)
    except HTTPException as exc:
        if exc.status_code == 403 and exc.detail == "permission_denied":
            return False
        raise
    return True


def _scoped_time(value: datetime | str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise CostLedgerError("cost_time_range_invalid", 400)
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise CostLedgerError("cost_time_range_invalid", 400) from None


def _scoped_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    if (not isinstance(value, str) or not 1 <= len(value) <= 36
            or _IDENTIFIER.fullmatch(value) is None):
        raise CostLedgerError("cost_filter_invalid", 400)
    return value


def _item(ledger: CostLedger) -> dict:
    return {
        "id": ledger.id, "run_id": ledger.run_id, "step_id": ledger.step_id,
        "call_key": ledger.call_key, "requester_id": ledger.requester_id,
        "provider_key": ledger.provider_key, "model": ledger.model,
        "price_version": ledger.price_version, "currency": ledger.currency,
        "reserved_amount": ledger.reserved_amount,
        "estimated_amount": ledger.estimated_amount, "status": ledger.status,
        "error_code": ledger.error_code,
        "created_at": _utc(ledger.created_at).isoformat(),
        "settled_at": _utc(ledger.settled_at).isoformat() if ledger.settled_at else None,
    }


def query_costs(session: Session, actor: AuthenticatedSubject, *,
                run_id: str | None = None, requester_id: str | None = None,
                from_time: datetime | str | None = None,
                to_time: datetime | str | None = None,
                limit: int = 50, cursor: str | None = None) -> tuple[list[dict], str | None]:
    """Read a scoped page; organization/store always come from the principal."""
    subject_type = session.scalar(select(Subject.subject_type).where(
        Subject.organization_id == actor.organization_id, Subject.id == actor.subject_id))
    if subject_type != "person":
        raise HTTPException(status_code=403, detail="permission_denied")
    store_allowed = _permission_allowed(session, actor, "agents.cost.read_store")
    self_allowed = _permission_allowed(session, actor, "agents.cost.read_self")
    if not store_allowed and not self_allowed:
        raise HTTPException(status_code=403, detail="permission_denied")
    if not store_allowed:
        if requester_id is not None and requester_id != actor.subject_id:
            raise HTTPException(status_code=403, detail="permission_denied")
        requester_id = actor.subject_id
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CostLedgerError("cost_limit_invalid", 400)
    run_id = _scoped_identifier(run_id)
    requester_id = _scoped_identifier(requester_id)
    start = _scoped_time(from_time, "from_time")
    end = _scoped_time(to_time, "to_time")
    if start is not None and end is not None and start >= end:
        raise CostLedgerError("cost_time_range_invalid", 400)
    if cursor is not None:
        if (not isinstance(cursor, str) or not 1 <= len(cursor) <= 36
                or _IDENTIFIER.fullmatch(cursor) is None):
            raise CostLedgerError("cost_cursor_invalid", 400)

    base = [
        CostLedger.organization_id == actor.organization_id,
        CostLedger.store_id == actor.store_id,
    ]
    statement = select(CostLedger).where(*base)
    if run_id is not None:
        statement = statement.where(CostLedger.run_id == run_id)
    if requester_id is not None:
        statement = statement.where(CostLedger.requester_id == requester_id)
    if start is not None:
        statement = statement.where(CostLedger.created_at >= start)
    if end is not None:
        statement = statement.where(CostLedger.created_at < end)
    if cursor is not None:
        cursor_row = session.scalar(select(CostLedger).where(
            *base, CostLedger.id == cursor))
        if cursor_row is None:
            raise CostLedgerError("cost_cursor_invalid", 400)
        if requester_id is not None and cursor_row.requester_id != requester_id:
            raise CostLedgerError("cost_cursor_invalid", 400)
        statement = statement.where(or_(
            CostLedger.created_at < cursor_row.created_at,
            and_(CostLedger.created_at == cursor_row.created_at,
                 CostLedger.id < cursor_row.id),
        ))
    statement = statement.order_by(
        CostLedger.created_at.desc(), CostLedger.id.desc()).limit(limit + 1)
    rows = list(session.scalars(statement).all())
    has_more = len(rows) > limit
    page = rows[:limit]
    return [_item(row) for row in page], (page[-1].id if has_more and page else None)
