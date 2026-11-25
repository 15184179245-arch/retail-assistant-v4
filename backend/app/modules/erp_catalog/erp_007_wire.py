"""Strict synthetic catalog wire contract for ERP-007.

This is a local synthetic protocol, not the official ERP API. Image bytes are
bound to metadata at dispatch time and are intentionally absent from the
persisted request and readback proof.
"""
from __future__ import annotations

import hmac
from typing import Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator
from typing import Annotated

from backend.app.modules.erp_commands.erp_003_wire import canonical_digest
from backend.app.modules.erp_catalog.erp_007_schemas import (
    CatalogContext,
    CatalogResult,
    Digest,
    Identifier,
    Operation,
    StrictDTO,
    Text200,
    Version,
)

CONTRACT_ID = "retail.erp-catalog-sandbox"
ADAPTER_ID = "synthetic.catalog.v1"
MODE = "synthetic"
PROTOCOL_VERSION = 1
MAX_IMAGE_BYTES = 5_242_880
NONCE = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$")]
SIGNATURE = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]

_COMMON_FIELDS = frozenset(
    (
        "contract_id",
        "adapter_id",
        "protocol_version",
        "mode",
        "organization_id",
        "store_id",
        "route",
    )
)
_DISPATCH_FIELDS = _COMMON_FIELDS | frozenset(
    (
        "attempt_id",
        "command_id",
        "request_id",
        "request_digest",
        "operation",
        "source_digest",
        "action_digest",
        "context_digest",
        "input_digest",
        "context",
    )
)
_OPERATIONS = frozenset(("text_update", "image_upload", "image_bind"))


class CatalogWireCommon(StrictDTO):
    contract_id: Literal["retail.erp-catalog-sandbox"] = CONTRACT_ID
    adapter_id: Literal["synthetic.catalog.v1"] = ADAPTER_ID
    protocol_version: Literal[1] = PROTOCOL_VERSION
    mode: Literal["synthetic"] = MODE
    organization_id: Identifier
    store_id: Identifier
    route: Literal["api"]

    @field_validator("protocol_version", mode="before")
    @classmethod
    def exact_protocol_version(cls, value: Any) -> int:
        if type(value) is not int or value != PROTOCOL_VERSION:
            raise ValueError("catalog_protocol_version_invalid")
        return PROTOCOL_VERSION


class CatalogDispatchRequest(CatalogWireCommon):
    attempt_id: Identifier
    command_id: Identifier
    request_id: Identifier
    request_digest: Digest
    operation: Operation
    source_digest: Digest
    action_digest: Digest
    context_digest: Digest
    input_digest: Digest
    context: CatalogContext

    @model_validator(mode="before")
    @classmethod
    def exact_request_fields(cls, value: Any) -> Any:
        if isinstance(value, dict) and set(value) != _DISPATCH_FIELDS:
            raise ValueError("catalog_dispatch_fields_invalid")
        return value

    @model_validator(mode="after")
    def context_binding(self) -> CatalogDispatchRequest:
        source = self.context.source
        if (self.organization_id != source.organization_id
                or self.store_id != source.store_id
                or self.operation != self.context.action.operation):
            raise ValueError("catalog_dispatch_context_invalid")
        try:
            source_dump = source.model_dump(mode="json")
            action_dump = self.context.action.model_dump(mode="json")
            context_dump = self.context.model_dump(mode="json")
            metadata = self.model_dump(mode="json", exclude={"input_digest"})
            expected = canonical_digest(metadata)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("catalog_dispatch_digest_invalid") from None
        if (not hmac.compare_digest(self.source_digest, canonical_digest(source_dump))
                or not hmac.compare_digest(self.action_digest, canonical_digest(action_dump))
                or not hmac.compare_digest(self.context_digest, canonical_digest(context_dump))
                or not hmac.compare_digest(self.input_digest, expected)):
            raise ValueError("catalog_dispatch_digest_invalid")
        return self


class CatalogCapabilities(CatalogWireCommon):
    supports_operations: list[Operation] = Field(min_length=3, max_length=3)
    max_image_bytes: Annotated[int, Field(strict=True, ge=1)]
    supports_readback: Literal[True]
    nonce: NONCE
    signature: SIGNATURE

    @model_validator(mode="before")
    @classmethod
    def require_physical_common_fields(cls, value: Any) -> Any:
        if isinstance(value, dict) and not _COMMON_FIELDS.issubset(value):
            raise ValueError("catalog_capabilities_common_invalid")
        return value

    @field_validator("protocol_version", mode="before")
    @classmethod
    def exact_protocol_version(cls, value: Any) -> int:
        if type(value) is not int or value != PROTOCOL_VERSION:
            raise ValueError("catalog_protocol_version_invalid")
        return PROTOCOL_VERSION

    @field_validator("max_image_bytes", mode="before")
    @classmethod
    def exact_max_image_bytes(cls, value: Any) -> int:
        if type(value) is not int or value != MAX_IMAGE_BYTES:
            raise ValueError("catalog_capability_invalid")
        return MAX_IMAGE_BYTES

    @field_validator("supports_readback", mode="before")
    @classmethod
    def exact_readback_flag(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("catalog_capability_invalid")
        return True

    @field_validator("supports_operations", mode="before")
    @classmethod
    def exact_operation_list(cls, value: Any) -> list[str]:
        if (type(value) is not list or len(value) != 3
                or any(type(item) is not str for item in value)
                or len(set(value)) != 3 or set(value) != _OPERATIONS):
            raise ValueError("catalog_capability_invalid")
        return value


class CatalogDispatchAck(CatalogWireCommon):
    operation: Operation
    attempt_id: Identifier
    command_id: Identifier
    input_digest: Digest
    delivery_ref: Text200
    nonce: NONCE
    signature: SIGNATURE

    @model_validator(mode="before")
    @classmethod
    def require_physical_common_fields(cls, value: Any) -> Any:
        if isinstance(value, dict) and not _COMMON_FIELDS.issubset(value):
            raise ValueError("catalog_ack_common_invalid")
        return value

    @field_validator("protocol_version", mode="before")
    @classmethod
    def exact_protocol_version(cls, value: Any) -> int:
        if type(value) is not int or value != PROTOCOL_VERSION:
            raise ValueError("catalog_protocol_version_invalid")
        return PROTOCOL_VERSION


class CatalogReadbackReceipt(StrictDTO):
    request: CatalogDispatchRequest
    result: CatalogResult
    nonce: NONCE
    signature: SIGNATURE

    @model_validator(mode="before")
    @classmethod
    def exact_receipt_fields(cls, value: Any) -> Any:
        if isinstance(value, dict) and set(value) != {"request", "result", "nonce", "signature"}:
            raise ValueError("catalog_readback_fields_invalid")
        return value

    @model_validator(mode="after")
    def exact_result_binding(self) -> CatalogReadbackReceipt:
        validate_catalog_result(self.request, self.result)
        return self


def dispatch_input_digest(value: dict[str, Any]) -> str:
    """Digest the complete request metadata, excluding only ``input_digest``."""

    if type(value) is not dict or set(value) != _DISPATCH_FIELDS - {"input_digest"}:
        raise ValueError("catalog_dispatch_fields_invalid")
    return canonical_digest(value)


def _revalidate_request(request: CatalogDispatchRequest) -> CatalogDispatchRequest:
    if type(request) is not CatalogDispatchRequest:
        raise ValueError("catalog_dispatch_request_invalid")
    try:
        return CatalogDispatchRequest.model_validate_json(request.model_dump_json())
    except (TypeError, ValueError, RecursionError):
        raise ValueError("catalog_dispatch_request_invalid") from None


def validate_catalog_result(
    request: CatalogDispatchRequest, result: CatalogResult
) -> CatalogResult:
    """Validate that a normalized signed peer result proves this exact action."""

    request = _revalidate_request(request)
    if type(result) is not CatalogResult:
        raise ValueError("catalog_result_invalid")
    try:
        result = CatalogResult.model_validate_json(result.model_dump_json())
    except (TypeError, ValueError, RecursionError):
        raise ValueError("catalog_result_invalid") from None

    context = request.context
    source = context.source
    action = context.action
    operation = action.operation
    if result.operation != operation or result.action_digest != request.action_digest:
        raise ValueError("catalog_result_binding_invalid")

    current_remote_version = source.sandbox_remote_version
    if result.outcome == "rejected":
        if result.remote_version != current_remote_version:
            raise ValueError("catalog_rejection_version_invalid")
        # CatalogResult's strict schema rejects applied fields on a rejection.
        return result

    if operation == "text_update":
        if (result.field_name != action.field_name
                or result.field_value != action.new_value
                or result.asset_ref is not None or result.asset_sha256 is not None
                or result.remote_version != current_remote_version + 1):
            raise ValueError("catalog_text_result_binding_invalid")
    elif operation == "image_upload":
        if (result.field_name is not None or result.field_value is not None
                or result.asset_sha256 != action.asset.sha256
                or result.asset_ref is None or not result.asset_ref.strip()
                or result.remote_version != current_remote_version):
            raise ValueError("catalog_upload_result_binding_invalid")
    elif operation == "image_bind":
        if (result.field_name is not None or result.field_value is not None
                or result.asset_sha256 != action.asset.sha256
                or result.asset_ref != action.upload_proof.asset_ref
                or result.remote_version != current_remote_version + 1):
            raise ValueError("catalog_bind_result_binding_invalid")
    else:  # Operation's Literal makes this unreachable; keep the boundary explicit.
        raise ValueError("catalog_result_operation_invalid")
    return result


def normalized_proof(
    request: CatalogDispatchRequest, result: CatalogResult
) -> dict[str, dict[str, Any]]:
    """Return the immutable evidence shape, intentionally omitting nonce/signature."""

    validated_request = _revalidate_request(request)
    validated_result = validate_catalog_result(validated_request, result)
    return {
        "request": validated_request.model_dump(mode="json"),
        "result": validated_result.model_dump(mode="json"),
    }


__all__ = [
    "ADAPTER_ID",
    "CONTRACT_ID",
    "MAX_IMAGE_BYTES",
    "MODE",
    "PROTOCOL_VERSION",
    "CatalogCapabilities",
    "CatalogDispatchAck",
    "CatalogDispatchRequest",
    "CatalogReadbackReceipt",
    "CatalogWireCommon",
    "dispatch_input_digest",
    "normalized_proof",
    "validate_catalog_result",
]
