"""Pure, versioned estimates over evidence-backed, DISJOINT billing dimensions.

No provider calls, keys, persistence, currency conversion or implicit free tier.
Caller must normalize raw usage under the price's explicit usage contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Context, Decimal, localcontext, ROUND_HALF_UP
import json
from pathlib import Path
import re

MAX_QUANTITY = 2**63 - 1
_DIMENSION = re.compile(r"[a-z][a-z0-9_]{0,39}\Z", re.ASCII)
_RATE = re.compile(r"[0-9]{1,18}(?:\.[0-9]{1,18})?\Z", re.ASCII)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}\Z", re.ASCII)


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("cost_identifier_invalid")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("cost_timezone_required")
    return value.astimezone(timezone.utc)


def _pairs(values: tuple) -> None:
    if type(values) is not tuple or not 1 <= len(values) <= 16:
        raise ValueError("cost_dimensions_invalid")
    names = []
    for pair in values:
        if type(pair) is not tuple or len(pair) != 2:
            raise ValueError("cost_dimensions_invalid")
        key = pair[0]
        if not isinstance(key, str) or not _DIMENSION.fullmatch(key):
            raise ValueError("cost_dimension_invalid")
        names.append(key)
    if len(names) != len(set(names)):
        raise ValueError("cost_dimension_duplicate")


@dataclass(frozen=True, slots=True)
class PriceVersion:
    provider: str
    model: str
    version: str
    currency: str
    effective_from: datetime
    effective_until: datetime | None
    usage_contract: str
    rates_per_million: tuple[tuple[str, str], ...]
    price_source: str
    usage_source: str
    verified_at: datetime
    provenance: str  # verified or synthetic; a marker is not verification evidence itself

    def __post_init__(self):
        for value in (self.provider, self.model, self.version, self.usage_contract):
            _identifier(value)
        if not isinstance(self.currency, str) or re.fullmatch(r"[A-Z]{3}", self.currency, re.ASCII) is None:
            raise ValueError("cost_currency_invalid")
        start = _utc(self.effective_from)
        if self.effective_until is not None and _utc(self.effective_until) <= start:
            raise ValueError("cost_price_interval_invalid")
        _utc(self.verified_at)
        if self.provenance not in ("verified", "synthetic"):
            raise ValueError("cost_provenance_invalid")
        for source in (self.price_source, self.usage_source):
            if not isinstance(source, str) or not source.strip() or len(source) > 500:
                raise ValueError("cost_source_required")
        _pairs(self.rates_per_million)
        for _, rate in self.rates_per_million:
            if not isinstance(rate, str) or _RATE.fullmatch(rate) is None:
                raise ValueError("cost_rate_invalid")


@dataclass(frozen=True, slots=True)
class NormalizedUsage:
    contract: str
    quantities: tuple[tuple[str, int | None], ...]

    def __post_init__(self):
        _identifier(self.contract)
        _pairs(self.quantities)
        for _, count in self.quantities:
            if count is not None and (type(count) is not int or not 0 <= count <= MAX_QUANTITY):
                raise ValueError("cost_quantity_invalid")


@dataclass(frozen=True, slots=True)
class PriceBook:
    versions: tuple[PriceVersion, ...] = ()

    def __post_init__(self):
        if type(self.versions) is not tuple or not all(isinstance(v, PriceVersion) for v in self.versions):
            raise ValueError("cost_price_book_invalid")
        seen = set()
        for price in self.versions:
            key = (price.provider, price.model, price.version)
            if key in seen:
                raise ValueError("cost_price_version_duplicate")
            seen.add(key)
        grouped: dict[tuple[str, str], list[PriceVersion]] = {}
        for price in self.versions:
            grouped.setdefault((price.provider, price.model), []).append(price)
        for group in grouped.values():
            ordered = sorted(group, key=lambda p: p.effective_from)
            for earlier, later in zip(ordered, ordered[1:]):
                if earlier.effective_until is None or earlier.effective_until > later.effective_from:
                    raise ValueError("cost_price_intervals_overlap")

    def select(self, provider: str, model: str, called_at: datetime) -> PriceVersion | None:
        instant = _utc(called_at)
        return next((p for p in self.versions if (p.provider, p.model) == (provider, model)
            and p.effective_from <= instant and (p.effective_until is None or instant < p.effective_until)), None)


@dataclass(frozen=True, slots=True)
class CostEstimate:
    provider: str
    model: str
    called_at: datetime
    status: str
    reason: str | None
    price: PriceVersion | None
    usage: NormalizedUsage | None
    amount: Decimal | None
    unrounded_amount: Decimal | None

    @property
    def currency(self) -> str | None:
        return self.price.currency if self.price else None


def estimate_cost(book: PriceBook, *, provider: str, model: str, called_at: datetime,
                  usage: NormalizedUsage | None, allow_synthetic: bool = False) -> CostEstimate:
    _identifier(provider)
    _identifier(model)
    instant = _utc(called_at)
    if type(allow_synthetic) is not bool:
        raise ValueError("cost_synthetic_flag_invalid")
    price = book.select(provider, model, instant)

    def unknown(reason: str) -> CostEstimate:
        return CostEstimate(provider, model, instant, "unknown", reason, price, usage, None, None)

    if price is None:
        return unknown("price_unconfigured")
    if price.provenance == "synthetic" and not allow_synthetic:
        return unknown("synthetic_price_not_authorized")
    if usage is None:
        return unknown("usage_missing")
    if not isinstance(usage, NormalizedUsage):
        raise ValueError("cost_usage_invalid")
    if usage.contract != price.usage_contract:
        return unknown("usage_contract_mismatch")
    quantities = dict(usage.quantities)
    if quantities.keys() != dict(price.rates_per_million).keys():
        return unknown("usage_dimensions_mismatch")
    if any(value is None for value in quantities.values()):
        return unknown("usage_incomplete")
    with localcontext(Context(prec=80, rounding=ROUND_HALF_UP)) as context:
        # Quantity/rate bounds + <=16 dimensions make precision 80 sufficient.
        total = sum((Decimal(quantities[key]) * Decimal(rate) / Decimal(1_000_000)
                     for key, rate in price.rates_per_million), Decimal(0))
        amount = total.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return CostEstimate(provider, model, instant, "estimated", None, price, usage, amount, total)


@dataclass(frozen=True, slots=True)
class CostTotal:
    currency: str | None
    amount: Decimal | None
    status: str


def _six_decimal_estimate(item: object) -> bool:
    """A total may only sum estimates that satisfy this module's 6-place invariant.

    Both the rounded amount and the retained unrounded amount are untrusted for
    hand-constructed objects: the rounded value must be finite, non-negative,
    exactly 6 decimal places, and equal to the fixed-context rounding of a
    finite non-negative unrounded value.
    """
    if not isinstance(item, CostEstimate):
        return False
    if item.status != "estimated" or item.currency is None:
        return False
    amount = item.amount
    unrounded = item.unrounded_amount
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
        return False
    if not isinstance(unrounded, Decimal) or not unrounded.is_finite() or unrounded < 0:
        return False
    try:
        with localcontext(Context(prec=80, rounding=ROUND_HALF_UP)):
            return (amount == amount.quantize(Decimal("0.000001"))
                    and amount == unrounded.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
    except ArithmeticError:
        return False


def total_cost(estimates: tuple[CostEstimate, ...]) -> CostTotal:
    """Unknown/incomplete estimates win; currency checks apply only to clean sums.

    Any unknown, incomplete or invalid estimate keeps the total unknown and is
    never silently coerced to zero. Its currency label is retained only when all
    labelled estimates agree; mixed labels keep the total currency-less. Only an
    all-valid same-currency set is summed, and mixed valid currencies raise.
    """
    if not estimates:
        return CostTotal(None, None, "unknown")
    currencies = {item.currency for item in estimates if getattr(item, "currency", None) is not None}
    if not all(_six_decimal_estimate(item) for item in estimates):
        return CostTotal(next(iter(currencies)) if len(currencies) == 1 else None, None, "unknown")
    if len(currencies) > 1:
        raise ValueError("cost_currency_mismatch")
    currency = next(iter(currencies))
    with localcontext(Context(prec=80, rounding=ROUND_HALF_UP)) as context:
        return CostTotal(currency, sum((item.amount for item in estimates), Decimal(0)), "estimated")


def load_price_book(path: Path) -> PriceBook:
    """Explicit local config, never discovers credentials or provider prices."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("cost_price_config_duplicate_key")
            result[key] = value
        return result

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("cost_price_config_invalid") from exc
    try:
        raw = json.loads(text, object_pairs_hook=unique_object)
    except json.JSONDecodeError as exc:
        raise ValueError("cost_price_config_invalid") from exc
    if (not isinstance(raw, dict) or set(raw) != {"schema_version", "status", "prices"}
            or type(raw["schema_version"]) is not int or raw["schema_version"] != 1
            or raw["status"] not in ("unconfigured", "configured") or not isinstance(raw["prices"], list)):
        raise ValueError("cost_price_config_invalid")
    if (raw["status"] == "unconfigured") != (not raw["prices"]):
        raise ValueError("cost_price_config_inconsistent")
    prices = []
    for entry in raw["prices"]:
        if not isinstance(entry, dict):
            raise ValueError("cost_price_config_invalid")
        values = dict(entry)
        for key in ("effective_from", "effective_until", "verified_at"):
            value = values.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("cost_price_config_invalid")
                try:
                    values[key] = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("cost_price_config_invalid") from exc
        rates = values.get("rates_per_million")
        if not isinstance(rates, dict):
            raise ValueError("cost_price_config_invalid")
        values["rates_per_million"] = tuple(sorted(rates.items()))
        try:
            prices.append(PriceVersion(**values))
        except TypeError as exc:
            raise ValueError("cost_price_config_invalid") from exc
    return PriceBook(tuple(prices))
