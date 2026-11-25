"""HTTP adapter for the fixed synthetic.receipts.v1 loopback contract."""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from backend.app.modules.erp_commands._loopback_transport import (
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
from backend.app.modules.erp_purchasing.erp_005_wire import (
    ADAPTER_ID,
    CONTRACT_ID,
    MODE,
    PROTOCOL_VERSION,
    ReceiptCapabilities,
    ReceiptDispatchAck,
    ReceiptDispatchRequest,
    ReceiptReadbackReceipt,
    ReceiptWireCommon,
)

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
_LINE_FIELDS = (
    "line_id",
    "line_no",
    "product_id",
    "product_version",
    "intent_line_id",
    "quantity",
    "unit",
    "quoted_price",
    "unit_price",
    "line_amount",
    "evidence_sha256",
    "price_evidence_sha256",
    "final_for_intent_line",
)


class SyntheticReceiptHTTPAdapter:
    """One-route API adapter over the shared bounded loopback transport."""

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
        return (
            "SyntheticReceiptHTTPAdapter("
            f"route={self.route!r}, host={self._config.host!r}, port={self._config.port!r}, "
            f"organization_id={self.organization_id!r}, store_id={self.store_id!r})"
        )

    @staticmethod
    def _path(suffix: str) -> str:
        return f"/__synthetic_receipts/v1/api/{suffix}"

    def _verify_common(
        self, dto: ReceiptWireCommon, data: dict[str, Any], phase: str, maybe_sent: bool
    ) -> None:
        if not _COMMON_FIELDS.issubset(data):
            raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent)
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
    def _validate(model: type, data: dict[str, Any], phase: str, maybe_sent: bool):
        try:
            return model.model_validate(data)
        except (ValidationError, TypeError, ValueError, RecursionError):
            raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent) from None

    def _revalidate(self, request: Any, phase: str) -> ReceiptDispatchRequest:
        if type(request) is not ReceiptDispatchRequest:
            raise SandboxAdapterError(REQUEST_INVALID, phase)
        try:
            validated = ReceiptDispatchRequest.model_validate_json(request.model_dump_json())
        except (ValidationError, SandboxAdapterError, TypeError, ValueError, RecursionError):
            raise SandboxAdapterError(REQUEST_INVALID, phase) from None
        if (
            validated.organization_id != self.organization_id
            or validated.store_id != self.store_id
            or validated.route != self.route
        ):
            raise SandboxAdapterError(REQUEST_INVALID, phase)
        return validated

    def capabilities(self) -> None:
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
        capabilities = self._validate(ReceiptCapabilities, data, "capabilities", False)
        self._verify_common(capabilities, data, "capabilities", False)
        return None

    def dispatch(self, request: ReceiptDispatchRequest) -> ReceiptDispatchAck:
        validated = self._revalidate(request, "dispatch")
        # The fixed peer must prove this API contract and scope before each POST.
        self.capabilities()
        nonce = new_nonce()
        body = canonical_json(validated.model_dump(mode="json")).encode("utf-8")
        if not body or len(body) > MAX_REQUEST_BYTES:
            raise SandboxAdapterError(REQUEST_TOO_LARGE, "dispatch", False)
        _, raw = perform_loopback_request(
            self._config,
            "POST",
            self._path("commands"),
            nonce,
            body,
            "dispatch",
            idempotency_key=validated.attempt_id,
        )
        data = parse_envelope(raw, "dispatch", True)
        verify_signed(self._config, data, nonce, "dispatch", True)
        ack = self._validate(ReceiptDispatchAck, data, "dispatch", True)
        self._verify_common(ack, data, "dispatch", True)
        if (
            ack.attempt_id != validated.attempt_id
            or ack.command_id != validated.command_id
            or ack.input_digest != validated.input_digest
        ):
            raise SandboxAdapterError(RESPONSE_INVALID, "dispatch", True)
        return ack

    def readback(self, request: ReceiptDispatchRequest) -> ReceiptReadbackReceipt | None:
        validated = self._revalidate(request, "readback")
        self.capabilities()
        nonce = new_nonce()
        status, raw = perform_loopback_request(
            self._config,
            "GET",
            self._path(f"commands/{validated.attempt_id}"),
            nonce,
            None,
            "readback",
            allow_404=True,
        )
        if status == 404:
            return None
        data = parse_envelope(raw, "readback", False)
        verify_signed(self._config, data, nonce, "readback", False)
        receipt = self._validate(ReceiptReadbackReceipt, data, "readback", False)
        self._verify_common(receipt, data, "readback", False)
        identity_fields = (
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
        )
        if any(getattr(receipt, name) != getattr(validated, name) for name in identity_fields):
            raise SandboxAdapterError(RESPONSE_INVALID, "readback", False)
        if len(receipt.lines) != len(validated.lines):
            raise SandboxAdapterError(RESPONSE_INVALID, "readback", False)
        for source, observed in zip(validated.lines, receipt.lines):
            if any(getattr(observed, name) != getattr(source, name) for name in _LINE_FIELDS):
                raise SandboxAdapterError(RESPONSE_INVALID, "readback", False)
        return receipt


__all__ = ["SyntheticReceiptHTTPAdapter"]
