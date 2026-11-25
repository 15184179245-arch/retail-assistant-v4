"""Strict synthetic receiving protocol for the ERP-005 loopback adapter.

This contract is deliberately independent of the official ERP API. A signed
synthetic readback is only a sandbox observation; it never claims stock posting.
"""
from __future__ import annotations

from decimal import Decimal, localcontext
import re
from typing import Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator
from typing import Annotated

from backend.app.modules.erp_commands.erp_003_wire import canonical_digest
from backend.app.modules.erp_purchasing.erp_005_schemas import (
    Digest,
    Identifier,
    Money,
    Number,
    Quantity,
    ReceiptCumulative,
    StrictDTO,
    Text,
    Unit,
    Version,
)
from backend.app.modules.receiving.document_service import compute_line_total

CONTRACT_ID = "retail.erp-receipts-sandbox"
ADAPTER_ID = "synthetic.receipts.v1"
MODE = "synthetic"
PROTOCOL_VERSION = 1
NONCE = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$")]
SIGNATURE = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
ErrorCode = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$"),
]
ReceiptRoute = Literal["api"]


class ReceiptWireCommon(StrictDTO):
    contract_id: Literal["retail.erp-receipts-sandbox"] = CONTRACT_ID
    adapter_id: Literal["synthetic.receipts.v1"] = ADAPTER_ID
    protocol_version: Literal[1] = PROTOCOL_VERSION
    mode: Literal["synthetic"] = MODE
    organization_id: Identifier
    store_id: Identifier
    route: ReceiptRoute

    @field_validator("protocol_version", mode="before")
    @classmethod
    def exact_protocol_version(cls, value: Any) -> int:
        if type(value) is not int or value != PROTOCOL_VERSION:
            raise ValueError("receipt_protocol_version_invalid")
        return PROTOCOL_VERSION


class ReceiptWireLine(StrictDTO):
    line_id: Identifier
    line_no: Annotated[int, Field(strict=True, ge=1, le=100)]
    product_id: Identifier
    product_version: Version
    intent_line_id: Identifier
    quantity: Quantity
    unit: Unit
    quoted_price: Number
    unit_price: Number
    line_amount: Money
    evidence_sha256: Digest
    price_evidence_sha256: Digest
    final_for_intent_line: bool

    @field_validator("final_for_intent_line", mode="before")
    @classmethod
    def strict_final_flag(cls, value: Any) -> bool:
        if type(value) is not bool:
            raise ValueError("receipt_final_flag_invalid")
        return value

    @model_validator(mode="after")
    def amount_matches_source(self) -> ReceiptWireLine:
        try:
            amount = compute_line_total(self.quantity, self.unit_price)
        except (ArithmeticError, TypeError, ValueError):
            raise ValueError("receipt_line_amount_invalid") from None
        if amount != Decimal(self.line_amount):
            raise ValueError("receipt_line_amount_inconsistent")
        return self


def _validate_cumulative(cumulative: list[ReceiptCumulative]) -> None:
    if len({item.intent_line_id for item in cumulative}) != len(cumulative):
        raise ValueError("receipt_duplicate_cumulative_line")


def _validate_lines(lines: list[ReceiptWireLine], total_amount: str) -> None:
    if [line.line_no for line in lines] != list(range(1, len(lines) + 1)):
        raise ValueError("receipt_line_order_invalid")
    if len({line.line_id for line in lines}) != len(lines):
        raise ValueError("receipt_duplicate_wire_line")
    with localcontext() as context:
        context.prec = 80
        total = sum((Decimal(line.line_amount) for line in lines), Decimal(0))
    if total != Decimal(total_amount):
        raise ValueError("receipt_total_inconsistent")


_DISPATCH_FIELDS = frozenset(
    (
        "contract_id",
        "adapter_id",
        "protocol_version",
        "mode",
        "organization_id",
        "store_id",
        "route",
        "attempt_id",
        "command_id",
        "request_id",
        "request_digest",
        "batch_id",
        "batch_version",
        "purchase_intent_id",
        "purchase_intent_version",
        "delivery_note_sha256",
        "source_digest",
        "input_digest",
        "currency",
        "currency_source",
        "total_amount",
        "cumulative",
        "lines",
    )
)
_DISPATCH_FIELDS_WITHOUT_DIGEST = _DISPATCH_FIELDS - {"input_digest"}
_DIGEST_EXCLUDED_FIELDS = frozenset(("input_digest", "nonce", "signature"))
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")


def dispatch_input_digest(value: dict[str, Any]) -> str:
    """Hash the complete normalized dispatch input, excluding only its digest."""

    if type(value) is not dict:
        raise ValueError("receipt_dispatch_input_invalid")
    keys = set(value)
    if keys - _DIGEST_EXCLUDED_FIELDS != _DISPATCH_FIELDS_WITHOUT_DIGEST:
        raise ValueError("receipt_dispatch_input_invalid")
    if "nonce" in value and (
        type(value["nonce"]) is not str or _NONCE_RE.fullmatch(value["nonce"]) is None
    ):
        raise ValueError("receipt_dispatch_input_invalid")
    if "signature" in value and (
        type(value["signature"]) is not str
        or _SIGNATURE_RE.fullmatch(value["signature"]) is None
    ):
        raise ValueError("receipt_dispatch_input_invalid")
    return canonical_digest(
        {key: item for key, item in value.items() if key not in _DIGEST_EXCLUDED_FIELDS}
    )


class ReceiptDispatchRequest(ReceiptWireCommon):
    attempt_id: Identifier
    command_id: Identifier
    request_id: Identifier
    request_digest: Digest
    batch_id: Identifier
    batch_version: Version
    purchase_intent_id: Identifier
    purchase_intent_version: Version
    delivery_note_sha256: Digest
    source_digest: Digest
    input_digest: Digest
    currency: Literal["CNY"]
    currency_source: Literal["synthetic_contract_only"]
    total_amount: Money
    cumulative: list[ReceiptCumulative] = Field(min_length=1, max_length=100)
    lines: list[ReceiptWireLine] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def complete_digest_and_lines(self) -> ReceiptDispatchRequest:
        _validate_cumulative(self.cumulative)
        _validate_lines(self.lines, self.total_amount)
        if self.input_digest != dispatch_input_digest(self.model_dump(mode="json")):
            raise ValueError("receipt_dispatch_digest_invalid")
        return self


class ReceiptCapabilities(ReceiptWireCommon):
    supports_line_readback: Literal[True]
    supports_once_dispatch: Literal[True]
    nonce: NONCE
    signature: SIGNATURE

    @field_validator("supports_line_readback", "supports_once_dispatch", mode="before")
    @classmethod
    def booleans_must_be_true(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("receipt_capability_invalid")
        return True


class ReceiptDispatchAck(ReceiptWireCommon):
    attempt_id: Identifier
    command_id: Identifier
    input_digest: Digest
    delivery_ref: Text
    nonce: NONCE
    signature: SIGNATURE


class ReceiptReadbackLine(ReceiptWireLine):
    status: Literal["accepted", "rejected", "unknown"]
    external_line_ref: Text | None = None
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def evidence_matches_status(self) -> ReceiptReadbackLine:
        if self.status == "accepted":
            if self.external_line_ref is None or self.error_code is not None:
                raise ValueError("receipt_readback_line_invalid")
        elif self.status == "rejected":
            if self.external_line_ref is not None or self.error_code is None:
                raise ValueError("receipt_readback_line_invalid")
        elif self.external_line_ref is not None:
            raise ValueError("receipt_readback_line_invalid")
        return self


class ReceiptReadbackReceipt(ReceiptWireCommon):
    attempt_id: Identifier
    command_id: Identifier
    request_id: Identifier
    request_digest: Digest
    batch_id: Identifier
    batch_version: Version
    purchase_intent_id: Identifier
    purchase_intent_version: Version
    delivery_note_sha256: Digest
    source_digest: Digest
    input_digest: Digest
    currency: Literal["CNY"]
    currency_source: Literal["synthetic_contract_only"]
    total_amount: Money
    cumulative: list[ReceiptCumulative] = Field(min_length=1, max_length=100)
    lines: list[ReceiptReadbackLine] = Field(min_length=1, max_length=100)
    document_state: Literal["accepted", "partial", "rejected", "unknown"]
    external_ref: Text | None = None
    error_code: ErrorCode | None = None
    nonce: NONCE
    signature: SIGNATURE

    @model_validator(mode="after")
    def evidence_matches_document_state(self) -> ReceiptReadbackReceipt:
        _validate_cumulative(self.cumulative)
        _validate_lines(self.lines, self.total_amount)
        accepted = [line.status == "accepted" for line in self.lines]
        if self.document_state == "accepted":
            if (not all(accepted) or self.external_ref is None
                    or self.error_code is not None):
                raise ValueError("receipt_readback_document_invalid")
        elif self.document_state == "rejected":
            if (any(accepted) or self.external_ref is not None
                    or self.error_code is None):
                raise ValueError("receipt_readback_document_invalid")
        elif self.document_state == "partial":
            if (not any(accepted) or all(accepted) or self.external_ref is not None):
                raise ValueError("receipt_readback_document_invalid")
        elif self.external_ref is not None:
            raise ValueError("receipt_readback_document_invalid")
        return self


__all__ = [
    "CONTRACT_ID",
    "ADAPTER_ID",
    "MODE",
    "PROTOCOL_VERSION",
    "ReceiptCapabilities",
    "ReceiptDispatchAck",
    "ReceiptDispatchRequest",
    "ReceiptReadbackLine",
    "ReceiptReadbackReceipt",
    "ReceiptWireCommon",
    "ReceiptWireLine",
    "dispatch_input_digest",
]
