"""Fixed numeric-loopback HTTP adapter for the ERP-007 synthetic catalog peer."""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

from pydantic import ValidationError

from backend.app.modules.erp_commands._loopback_transport import (
    MAX_CATALOG_UPLOAD_BYTES,
    new_nonce,
    parse_envelope,
    perform_loopback_request,
    verify_signed,
)
from backend.app.modules.erp_commands.erp_003_wire import (
    MAX_REQUEST_BYTES,
    REQUEST_INVALID,
    REQUEST_TOO_LARGE,
    RESPONSE_INVALID,
    LoopbackSandboxConfig,
    SandboxAdapterError,
    canonical_json,
)
from backend.app.modules.erp_catalog.erp_007_schemas import CatalogResult
from backend.app.modules.erp_catalog.erp_007_wire import (
    ADAPTER_ID,
    CONTRACT_ID,
    MAX_IMAGE_BYTES,
    MODE,
    PROTOCOL_VERSION,
    CatalogCapabilities,
    CatalogDispatchAck,
    CatalogDispatchRequest,
    CatalogReadbackReceipt,
    CatalogWireCommon,
    validate_catalog_result,
)
from backend.app.modules.media.local_storage import MediaStorageError, detect_content_type

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
_CAPABILITY_FIELDS = _COMMON_FIELDS | frozenset(
    ("supports_operations", "max_image_bytes", "supports_readback", "nonce", "signature")
)
_ACK_FIELDS = _COMMON_FIELDS | frozenset(
    ("operation", "attempt_id", "command_id", "input_digest", "delivery_ref", "nonce", "signature")
)
_READBACK_FIELDS = frozenset(("request", "result", "nonce", "signature"))
_BASE_PATH = "/__synthetic_catalog/v1/api"


class SyntheticCatalogHTTPAdapter:
    """One fixed synthetic route over the shared bounded loopback transport."""

    def __init__(self, config: LoopbackSandboxConfig):
        if type(config) is not LoopbackSandboxConfig:
            raise SandboxAdapterError(REQUEST_INVALID, "config")
        try:
            self._config = LoopbackSandboxConfig.model_validate(config.model_dump(mode="json"))
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise SandboxAdapterError(REQUEST_INVALID, "config") from None

    @property
    def organization_id(self) -> str:
        return self._config.organization_id

    @property
    def store_id(self) -> str:
        return self._config.store_id

    @property
    def route(self) -> str:
        return "api"

    @property
    def mode(self) -> str:
        return MODE

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    def __repr__(self) -> str:
        # Keep the configured shared secret and all endpoint paths out of repr.
        return (
            "SyntheticCatalogHTTPAdapter("
            f"route={self.route!r}, host={self._config.host!r}, port={self._config.port!r}, "
            f"organization_id={self.organization_id!r}, store_id={self.store_id!r})"
        )

    @staticmethod
    def _path(suffix: str) -> str:
        return f"{_BASE_PATH}/{suffix}"

    def _check_common(
        self, dto: CatalogWireCommon, phase: str, maybe_sent: bool = False
    ) -> None:
        if (
            dto.contract_id != CONTRACT_ID
            or dto.adapter_id != ADAPTER_ID
            or dto.protocol_version != PROTOCOL_VERSION
            or dto.mode != MODE
            or dto.organization_id != self.organization_id
            or dto.store_id != self.store_id
            or dto.route != self.route
        ):
            raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent)

    @staticmethod
    def _validate_json(
        model: type, data: dict[str, Any], phase: str, maybe_sent: bool
    ):
        try:
            # JSON validation preserves strict handling of JSON arrays and dates.
            return model.model_validate_json(canonical_json(data))
        except (ValidationError, SandboxAdapterError, TypeError, ValueError, RecursionError):
            raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent) from None

    def _revalidate(self, request: Any, phase: str) -> CatalogDispatchRequest:
        if type(request) is not CatalogDispatchRequest:
            raise SandboxAdapterError(REQUEST_INVALID, phase)
        try:
            validated = CatalogDispatchRequest.model_validate_json(request.model_dump_json())
        except (ValidationError, SandboxAdapterError, TypeError, ValueError, RecursionError):
            raise SandboxAdapterError(REQUEST_INVALID, phase) from None
        if (
            validated.organization_id != self.organization_id
            or validated.store_id != self.store_id
            or validated.route != self.route
        ):
            raise SandboxAdapterError(REQUEST_INVALID, phase)
        return validated

    def capabilities(self) -> CatalogCapabilities:
        nonce = new_nonce()
        _, raw = perform_loopback_request(
            self._config,
            "GET",
            self._path("capabilities"),
            nonce,
            None,
            "capabilities",
        )
        data = parse_envelope(raw, "capabilities", False)
        verify_signed(self._config, data, nonce, "capabilities", False)
        if set(data) != _CAPABILITY_FIELDS:
            raise SandboxAdapterError(RESPONSE_INVALID, "capabilities", False)
        capabilities = self._validate_json(CatalogCapabilities, data, "capabilities", False)
        self._check_common(capabilities, "capabilities")
        if (set(capabilities.supports_operations) != {"text_update", "image_upload", "image_bind"}
                or capabilities.max_image_bytes != MAX_IMAGE_BYTES
                or capabilities.supports_readback is not True):
            raise SandboxAdapterError(RESPONSE_INVALID, "capabilities", False)
        return capabilities

    @staticmethod
    def _validated_image_bytes(request: CatalogDispatchRequest, image_bytes: Any) -> bytes:
        action = request.context.action
        if action.operation != "image_upload":
            if image_bytes is not None:
                raise SandboxAdapterError(REQUEST_INVALID, "dispatch")
            return b""
        if type(image_bytes) is not bytes:
            raise SandboxAdapterError(REQUEST_INVALID, "dispatch")
        asset = action.asset
        if (not 1 <= len(image_bytes) <= MAX_IMAGE_BYTES
                or len(image_bytes) != asset.byte_size
                or not hmac.compare_digest(hashlib.sha256(image_bytes).hexdigest(), asset.sha256)):
            raise SandboxAdapterError(REQUEST_INVALID, "dispatch")
        try:
            content_type = detect_content_type(image_bytes)
        except (MediaStorageError, ValueError, TypeError):
            raise SandboxAdapterError(REQUEST_INVALID, "dispatch") from None
        if content_type != asset.content_type:
            raise SandboxAdapterError(REQUEST_INVALID, "dispatch")
        return image_bytes

    def dispatch(
        self, request: CatalogDispatchRequest, *, image_bytes: bytes | None = None
    ) -> CatalogDispatchAck:
        validated = self._revalidate(request, "dispatch")
        content = self._validated_image_bytes(validated, image_bytes)
        metadata = validated.model_dump(mode="json")
        if validated.operation == "image_upload":
            body_value = {
                "request": metadata,
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
            path = self._path("uploads")
            limit = MAX_CATALOG_UPLOAD_BYTES
        else:
            body_value = metadata
            path = self._path("commands")
            limit = MAX_REQUEST_BYTES
        try:
            body = canonical_json(body_value).encode("utf-8")
        except (SandboxAdapterError, TypeError, ValueError, RecursionError):
            raise SandboxAdapterError(REQUEST_INVALID, "dispatch") from None
        if not body or len(body) > limit:
            raise SandboxAdapterError(REQUEST_TOO_LARGE, "dispatch", False)

        # Prove the signed contract/scope before the one and only write request.
        self.capabilities()
        nonce = new_nonce()
        _, raw = perform_loopback_request(
            self._config,
            "POST",
            path,
            nonce,
            body,
            "dispatch",
            idempotency_key=validated.attempt_id,
        )
        data = parse_envelope(raw, "dispatch", True)
        verify_signed(self._config, data, nonce, "dispatch", True)
        if set(data) != _ACK_FIELDS:
            raise SandboxAdapterError(RESPONSE_INVALID, "dispatch", True)
        ack = self._validate_json(CatalogDispatchAck, data, "dispatch", True)
        self._check_common(ack, "dispatch", True)
        if (
            ack.operation != validated.operation
            or ack.attempt_id != validated.attempt_id
            or ack.command_id != validated.command_id
            or ack.input_digest != validated.input_digest
        ):
            raise SandboxAdapterError(RESPONSE_INVALID, "dispatch", True)
        return ack

    def readback(self, request: CatalogDispatchRequest) -> CatalogReadbackReceipt | None:
        validated = self._revalidate(request, "readback")
        resource = "uploads" if validated.operation == "image_upload" else "commands"
        nonce = new_nonce()
        status, raw = perform_loopback_request(
            self._config,
            "GET",
            self._path(f"{resource}/{validated.attempt_id}"),
            nonce,
            None,
            "readback",
            allow_404=True,
        )
        if status == 404:
            return None
        data = parse_envelope(raw, "readback", False)
        verify_signed(self._config, data, nonce, "readback", False)
        if set(data) != _READBACK_FIELDS:
            raise SandboxAdapterError(RESPONSE_INVALID, "readback", False)
        receipt = self._validate_json(CatalogReadbackReceipt, data, "readback", False)
        try:
            observed_request = receipt.request.model_dump(mode="json")
            expected_request = validated.model_dump(mode="json")
            if observed_request != expected_request:
                raise ValueError("catalog_readback_request_mismatch")
            validate_catalog_result(validated, receipt.result)
        except (ValidationError, SandboxAdapterError, TypeError, ValueError, RecursionError):
            raise SandboxAdapterError(RESPONSE_INVALID, "readback", False) from None
        return receipt


__all__ = ["SyntheticCatalogHTTPAdapter"]
