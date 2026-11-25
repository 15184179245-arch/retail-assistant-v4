"""HTTP adapter for the synthetic.sales.v1 protocol.

The route is selected by trusted host construction and frozen into every
request. ACKs represent delivery only; signed GET readback is still synthetic
evidence and never establishes a formal ERP document or payment.
"""
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
from backend.app.modules.erp_sales.erp_004_schemas import Route
from backend.app.modules.erp_sales.erp_004_wire import (
    ADAPTER_ID,
    CONTRACT_ID,
    MODE,
    PROTOCOL_VERSION,
    SalesCapabilities,
    SalesDispatchAck,
    SalesDispatchRequest,
    SalesReadbackReceipt,
    SalesWireCommon,
)

_COMMON_FIELDS = frozenset((
    "contract_id", "adapter_id", "protocol_version", "mode",
    "organization_id", "store_id", "route",
))


class SyntheticSalesHTTPAdapter:
    """Bounded API/POS synthetic adapter over the shared loopback transport."""

    def __init__(self, config: LoopbackSandboxConfig, *, route: Route):
        if type(config) is not LoopbackSandboxConfig:
            raise SandboxAdapterError(REQUEST_INVALID, "config")
        if type(route) is not str or route not in ("api", "pos"):
            raise SandboxAdapterError(REQUEST_INVALID, "config")
        try:
            self._config = LoopbackSandboxConfig.model_validate(
                config.model_dump(mode="json"))
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise SandboxAdapterError(REQUEST_INVALID, "config") from None
        self._route: Route = route

    @property
    def route(self) -> Route:
        return self._route

    @property
    def organization_id(self) -> str:
        return self._config.organization_id

    @property
    def store_id(self) -> str:
        return self._config.store_id

    @property
    def mode(self) -> str:
        return MODE

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    def __repr__(self) -> str:
        return (
            "SyntheticSalesHTTPAdapter("
            f"route={self.route!r}, host={self._config.host!r}, "
            f"port={self._config.port!r}, organization_id={self.organization_id!r}, "
            f"store_id={self.store_id!r})"
        )

    def _path(self, suffix: str) -> str:
        return f"/__synthetic_sales/v1/{self.route}/{suffix}"

    def _verify_common(self, dto: SalesWireCommon, data: dict[str, Any], phase: str,
                       maybe_sent: bool) -> None:
        if not _COMMON_FIELDS.issubset(data):
            raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent)
        if (dto.contract_id != CONTRACT_ID
                or dto.adapter_id != ADAPTER_ID
                or dto.protocol_version != PROTOCOL_VERSION
                or dto.mode != MODE
                or dto.organization_id != self.organization_id
                or dto.store_id != self.store_id
                or dto.route != self.route):
            raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent)

    @staticmethod
    def _validate(model: type, data: dict[str, Any], phase: str,
                  maybe_sent: bool):
        try:
            return model.model_validate(data)
        except (ValidationError, TypeError, ValueError, RecursionError):
            raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent) from None

    def _revalidate(self, request: Any, phase: str) -> SalesDispatchRequest:
        if type(request) is not SalesDispatchRequest:
            raise SandboxAdapterError(REQUEST_INVALID, phase)
        try:
            validated = SalesDispatchRequest.model_validate_json(request.model_dump_json())
        except (ValidationError, SandboxAdapterError, TypeError, ValueError, RecursionError):
            raise SandboxAdapterError(REQUEST_INVALID, phase) from None
        if (validated.organization_id != self.organization_id
                or validated.store_id != self.store_id or validated.route != self.route):
            raise SandboxAdapterError(REQUEST_INVALID, phase)
        return validated

    def capabilities(self) -> None:
        nonce = new_nonce()
        _, raw = perform_loopback_request(
            self._config, "GET", self._path("capabilities"), nonce, None,
            "capabilities")
        data = parse_envelope(raw, "capabilities", False)
        verify_signed(self._config, data, nonce, "capabilities", False)
        capabilities = self._validate(SalesCapabilities, data, "capabilities", False)
        self._verify_common(capabilities, data, "capabilities", False)
        return None

    def dispatch(self, request: SalesDispatchRequest) -> SalesDispatchAck:
        validated = self._revalidate(request, "dispatch")
        # No POST is attempted unless the fixed peer proves route and scope.
        self.capabilities()
        nonce = new_nonce()
        body = canonical_json(validated.model_dump(mode="json")).encode("utf-8")
        if not body or len(body) > MAX_REQUEST_BYTES:
            raise SandboxAdapterError(REQUEST_TOO_LARGE, "dispatch", False)
        _, raw = perform_loopback_request(
            self._config, "POST", self._path("commands"), nonce, body,
            "dispatch", idempotency_key=validated.attempt_id)
        data = parse_envelope(raw, "dispatch", True)
        verify_signed(self._config, data, nonce, "dispatch", True)
        ack = self._validate(SalesDispatchAck, data, "dispatch", True)
        self._verify_common(ack, data, "dispatch", True)
        if (ack.attempt_id != validated.attempt_id
                or ack.command_id != validated.command_id
                or ack.input_digest != validated.input_digest):
            raise SandboxAdapterError(RESPONSE_INVALID, "dispatch", True)
        return ack

    def readback(self, request: SalesDispatchRequest) -> SalesReadbackReceipt | None:
        validated = self._revalidate(request, "readback")
        self.capabilities()
        nonce = new_nonce()
        status, raw = perform_loopback_request(
            self._config, "GET", self._path(f"commands/{validated.attempt_id}"),
            nonce, None, "readback", allow_404=True)
        if status == 404:
            return None
        data = parse_envelope(raw, "readback", False)
        verify_signed(self._config, data, nonce, "readback", False)
        receipt = self._validate(SalesReadbackReceipt, data, "readback", False)
        self._verify_common(receipt, data, "readback", False)
        identity_fields = (
            "attempt_id", "command_id", "request_id", "request_digest",
            "draft_id", "draft_version", "handoff_id", "source_digest",
            "input_digest", "currency", "total_amount",
        )
        if any(getattr(receipt, name) != getattr(validated, name)
               for name in identity_fields):
            raise SandboxAdapterError(RESPONSE_INVALID, "readback", False)
        if len(receipt.lines) != len(validated.lines):
            raise SandboxAdapterError(RESPONSE_INVALID, "readback", False)
        line_fields = (
            "line_id", "line_no", "product_id", "quantity", "unit_price",
            "discount_bps", "discounted_unit_price", "line_amount",
        )
        for source, observed in zip(validated.lines, receipt.lines):
            if any(getattr(observed, name) != getattr(source, name)
                   for name in line_fields):
                raise SandboxAdapterError(RESPONSE_INVALID, "readback", False)
        return receipt
