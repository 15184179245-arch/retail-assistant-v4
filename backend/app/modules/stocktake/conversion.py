"""STK-004 deterministic packaging conversion: evidence-gated, never an ERP unit rule.

The durable receipt is recorded by review_service in existing StocktakeCommand.
A rate is NOT established by a model prediction, guessed package label, or
untrusted client-supplied reviewer id. The verifier is the current human actor.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import re

RATE_PATTERN = re.compile(r"(?:[1-9][0-9]{0,5})\Z")
UNITS = frozenset({"piece", "box", "kg", "meter"})


class ConversionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ConversionError("stocktake_time_unaware")
    return value.astimezone(timezone.utc)


def verified_box_factor(factor: object, *, source_unit: str, target_unit: str) -> Decimal:
    if (source_unit, target_unit) != ("box", "piece"):
        raise ConversionError("stocktake_conversion_unit_unsupported")
    if not isinstance(factor, str) or RATE_PATTERN.fullmatch(factor) is None:
        raise ConversionError("stocktake_conversion_factor_unverified")
    amount = Decimal(factor)
    if amount <= 1:
        raise ConversionError("stocktake_conversion_factor_unverified")
    return amount


def active_factor(receipts: list[dict], *, product_id: str,
                  observed_at: datetime, now: datetime) -> tuple[Decimal | None, str | None, str]:
    """Return a factor only with exactly one valid human/media-backed receipt.

    Conversions may never be borrowed between products or locations/sessions.
    An expired, ambiguous or future-dated rate fails closed: raw box stays box.
    """
    observed_at, now = aware_utc(observed_at), aware_utc(now)
    matching = []
    for receipt in receipts:
        if receipt.get("product_id") != product_id:
            continue
        if receipt.get("status") != "human_verified_with_media":
            continue
        start = aware_utc(datetime.fromisoformat(receipt["effective_from"]))
        end = aware_utc(datetime.fromisoformat(receipt["expires_at"]))
        stamped = aware_utc(datetime.fromisoformat(receipt["verified_at"]))
        if not (start <= observed_at < end and start <= now < end and stamped <= now):
            continue
        factor = verified_box_factor(receipt.get("factor"),
                                     source_unit=receipt.get("source_unit"),
                                     target_unit=receipt.get("target_unit"))
        matching.append((factor, receipt.get("receipt_ref")))
    if not matching:
        return None, None, "conversion_evidence_missing_or_expired"
    if len(matching) != 1 or not matching[0][1]:
        return None, None, "conversion_evidence_ambiguous"
    return matching[0][0], matching[0][1], "verified_packaging_only"
