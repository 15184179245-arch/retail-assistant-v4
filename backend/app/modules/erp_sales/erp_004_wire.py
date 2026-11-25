"""Strict synthetic.sales.v1 request and readback contract for ERP-004.

This protocol is a local synthetic contract, not an official ERP or POS API.
The DTOs carry the complete reviewed sale so a signed readback can be compared
line by line. They intentionally contain no payment or ``paid`` field.
"""
from __future__ import annotations

from decimal import Decimal, localcontext
import re
from typing import Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator
from typing import Annotated

from backend.app.modules.erp_commands.erp_003_wire import canonical_digest
from backend.app.modules.erp_sales.erp_004_schemas import (
    Currency,
    Digest,
    Identifier,
    Money,
    Quantity,
    Route,
    StrictDTO,
    Text200,
    Version,
    compute_line_amounts,
)

CONTRACT_ID = "retail.erp-sales-sandbox"
ADAPTER_ID = "synthetic.sales.v1"
MODE = "synthetic"
PROTOCOL_VERSION = 1
NONCE = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$")]
SIGNATURE = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
ErrorCode = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")]


class SalesWireCommon(StrictDTO):
    contract_id: Literal["retail.erp-sales-sandbox"] = CONTRACT_ID
    adapter_id: Literal["synthetic.sales.v1"] = ADAPTER_ID
    protocol_version: Literal[1] = PROTOCOL_VERSION
    mode: Literal["synthetic"] = MODE
    organization_id: Identifier
    store_id: Identifier
    route: Route

    @field_validator("protocol_version", mode="before")
    @classmethod
    def exact_protocol_version(cls, value: Any) -> int:
        if type(value) is not int or value != PROTOCOL_VERSION:
            raise ValueError("sales_protocol_version_invalid")
        return PROTOCOL_VERSION


class SalesWireLine(StrictDTO):
    line_id: Identifier
    line_no: Annotated[int, Field(strict=True, ge=1, le=50)]
    product_id: Text200
    quantity: Quantity
    unit_price: Money
    discount_bps: Annotated[int, Field(strict=True, ge=0, le=10000)]
    discounted_unit_price: Money
    line_amount: Money

    @model_validator(mode="after")
    def amount_matches_source(self) -> "SalesWireLine":
        try:
            discounted, amount = compute_line_amounts(
                self.unit_price, self.quantity, self.discount_bps)
        except (ArithmeticError, TypeError, ValueError):
            raise ValueError("sales_line_amount_invalid") from None
        if (discounted != Decimal(self.discounted_unit_price)
                or amount != Decimal(self.line_amount)):
            raise ValueError("sales_line_amount_inconsistent")
        return self


def _validate_lines(lines: list[SalesWireLine], total_amount: str) -> None:
    if [line.line_no for line in lines] != list(range(1, len(lines) + 1)):
        raise ValueError("sales_line_order_invalid")
    if len({line.line_id for line in lines}) != len(lines):
        raise ValueError("sales_duplicate_line")
    with localcontext() as context:
        context.prec = 80
        total = sum((Decimal(line.line_amount) for line in lines), Decimal(0))
    if total != Decimal(total_amount):
        raise ValueError("sales_total_inconsistent")


_DISPATCH_FIELDS = frozenset((
    "contract_id", "adapter_id", "protocol_version", "mode", "organization_id",
    "store_id", "route", "attempt_id", "command_id", "request_id", "draft_id",
    "handoff_id", "request_digest", "source_digest", "input_digest",
    "draft_version", "currency", "total_amount", "lines",
))
_DISPATCH_FIELDS_WITHOUT_DIGEST = _DISPATCH_FIELDS - {"input_digest"}


def dispatch_input_digest(value: dict[str, Any]) -> str:
    """Hash a complete normalized dispatch dict, excluding only its own digest."""

    if type(value) is not dict:
        raise ValueError("sales_dispatch_input_invalid")
    keys = set(value)
    if keys not in (_DISPATCH_FIELDS_WITHOUT_DIGEST, _DISPATCH_FIELDS):
        raise ValueError("sales_dispatch_input_invalid")
    payload = {key: item for key, item in value.items() if key != "input_digest"}
    return canonical_digest(payload)


class SalesDispatchRequest(SalesWireCommon):
    attempt_id: Identifier
    command_id: Identifier
    request_id: Identifier
    draft_id: Identifier
    handoff_id: Identifier
    request_digest: Digest
    source_digest: Digest
    input_digest: Digest
    draft_version: Version
    currency: Currency
    total_amount: Money
    lines: list[SalesWireLine] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def complete_digest_and_lines(self) -> "SalesDispatchRequest":
        _validate_lines(self.lines, self.total_amount)
        if self.input_digest != dispatch_input_digest(self.model_dump(mode="json")):
            raise ValueError("sales_dispatch_digest_invalid")
        return self


class SalesCapabilities(SalesWireCommon):
    supports_line_readback: Literal[True]
    supports_once_dispatch: Literal[True]
    settlement_mode: Literal["not_verified"]
    nonce: NONCE
    signature: SIGNATURE

    @field_validator("supports_line_readback", "supports_once_dispatch", mode="before")
    @classmethod
    def booleans_must_be_true(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("sales_capability_invalid")
        return True


class SalesDispatchAck(SalesWireCommon):
    attempt_id: Identifier
    command_id: Identifier
    input_digest: Digest
    delivery_ref: Text200
    acknowledged: Literal[True]
    nonce: NONCE
    signature: SIGNATURE

    @field_validator("acknowledged", mode="before")
    @classmethod
    def acknowledgement_must_be_true(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("sales_ack_invalid")
        return True


class SalesReadbackLine(SalesWireLine):
    status: Literal["accepted", "rejected", "unknown"]
    external_line_ref: Text200 | None = None
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def evidence_matches_status(self) -> "SalesReadbackLine":
        if self.status == "accepted":
            if self.external_line_ref is None or self.error_code is not None:
                raise ValueError("sales_readback_line_invalid")
        elif self.status == "rejected":
            if self.external_line_ref is not None or self.error_code is None:
                raise ValueError("sales_readback_line_invalid")
        elif self.external_line_ref is not None:
            raise ValueError("sales_readback_line_invalid")
        return self


class SalesReadbackReceipt(SalesWireCommon):
    attempt_id: Identifier
    command_id: Identifier
    request_id: Identifier
    request_digest: Digest
    draft_id: Identifier
    draft_version: Version
    handoff_id: Identifier
    source_digest: Digest
    input_digest: Digest
    currency: Currency
    total_amount: Money
    lines: list[SalesReadbackLine] = Field(min_length=1, max_length=50)
    document_state: Literal["accepted", "rejected", "unknown"]
    external_ref: Text200 | None = None
    error_code: ErrorCode | None = None
    payment_state: Literal["not_verified"] = "not_verified"
    nonce: NONCE
    signature: SIGNATURE

    @model_validator(mode="after")
    def evidence_matches_document_state(self) -> "SalesReadbackReceipt":
        _validate_lines(self.lines, self.total_amount)
        if self.document_state == "accepted":
            if (any(line.status != "accepted" for line in self.lines)
                    or self.external_ref is None or self.error_code is not None):
                raise ValueError("sales_readback_document_invalid")
        elif self.document_state == "rejected":
            if (any(line.status == "accepted" for line in self.lines)
                    or self.external_ref is not None or self.error_code is None):
                raise ValueError("sales_readback_document_invalid")
        return self
