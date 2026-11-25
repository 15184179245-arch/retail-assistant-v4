"""Strict synthetic.settlement.v1 wire protocol contracts for ERP-006.

This protocol is a local synthetic sandbox contract, not an official ERP or banking API.
It registers and verifies settlement allocations against verified workpapers without
triggering external money movement.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT_ID = "retail.erp-settlement-sandbox"
ADAPTER_ID = "synthetic.settlement.v1"
MODE = "synthetic"
PROTOCOL_VERSION = 1


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_digest(data: Any) -> str:
    raw = canonical_json(data).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class StrictWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WireLine(StrictWire):
    line_id: str = Field(min_length=1, max_length=120)
    document_ref: str = Field(min_length=1, max_length=120)
    allocated_amount: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=255)


class SettlementDispatchRequest(StrictWire):
    contract_id: Literal["retail.erp-settlement-sandbox"] = CONTRACT_ID
    adapter_id: Literal["synthetic.settlement.v1"] = ADAPTER_ID
    protocol_version: Literal[1] = PROTOCOL_VERSION
    mode: Literal["synthetic"] = MODE
    organization_id: str = Field(min_length=1, max_length=36)
    store_id: str = Field(min_length=1, max_length=36)
    route: Literal["api"] = "api"
    attempt_id: str = Field(min_length=36, max_length=36)
    command_id: str = Field(min_length=36, max_length=36)
    workpaper_id: str = Field(min_length=36, max_length=36)
    operation: Literal["receipt_allocation", "payment_allocation"]
    party_type: Literal["customer", "supplier"]
    party_ref: str = Field(min_length=1, max_length=120)
    currency: Literal["CNY", "USD"]
    settlement_amount: str = Field(min_length=1, max_length=32)
    source_version: int = Field(ge=1)
    source_digest: str = Field(min_length=64, max_length=64)
    proposal_digest: str = Field(min_length=64, max_length=64)
    lines: list[WireLine] = Field(min_length=1, max_length=500)
    nonce: str = Field(min_length=32, max_length=32)
    signature: str = Field(min_length=64, max_length=64)

    @field_validator("protocol_version", mode="before")
    @classmethod
    def exact_protocol_version(cls, value: Any) -> int:
        if type(value) is not int or value != PROTOCOL_VERSION:
            raise ValueError("settlement_protocol_version_invalid")
        return PROTOCOL_VERSION


class SettlementDispatchAck(StrictWire):
    contract_id: Literal["retail.erp-settlement-sandbox"] = CONTRACT_ID
    adapter_id: Literal["synthetic.settlement.v1"] = ADAPTER_ID
    protocol_version: Literal[1] = PROTOCOL_VERSION
    mode: Literal["synthetic"] = MODE
    attempt_id: str = Field(min_length=36, max_length=36)
    command_id: str = Field(min_length=36, max_length=36)
    delivery_ref: str = Field(min_length=1, max_length=200)
    status: Literal["accepted", "rejected", "unknown"]
    allocation_results: list[dict[str, Any]] = Field(default_factory=list)
    external_ref: str | None = Field(default=None, max_length=200)
    receipt_digest: str = Field(min_length=64, max_length=64)
    nonce: str = Field(min_length=32, max_length=32)
    signature: str = Field(min_length=64, max_length=64)


def sign_wire_payload(data: dict[str, Any], secret_key: str = "synthetic-settlement-secret") -> str:
    unsigned = {k: v for k, v in data.items() if k != "signature"}
    content = canonical_json(unsigned) + secret_key
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
