"""ERP-003 B2a: isolated synthetic ERP HTTP adapter.

The adapter talks only to a trusted-host-configured numeric loopback address.
It never follows redirects, never reads proxy environment variables, never
retries a dispatch, and never logs or returns the synthetic secret.

Semantics (frozen):

* ``capabilities()`` proves the configured peer speaks this synthetic contract
  for the configured scope. It must succeed before a dispatch POST.
* ``dispatch()`` validates locally, performs the handshake, then sends at most
  one POST. An Ack only means the batch was received; it is never business
  success.
* ``readback()`` only ever issues GETs and returns ``None`` for an unknown
  batch (404). It never re-POSTs after a 404.

Every failure is a :class:`SandboxAdapterError` carrying a stable code, a
phase and a conservative ``maybe_sent`` flag. No secret, URL or raw exception
text reaches the caller, and exception chaining is suppressed.
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
    ACK_INVALID,
    ADAPTER_ID,
    BATCHES_PATH,
    CAPABILITIES_PATH,
    CONTRACT_ID,
    HANDSHAKE_FAILED,
    LINE_MISMATCH,
    MAX_REQUEST_BYTES,
    REQUEST_INVALID,
    REQUEST_TOO_LARGE,
    RESPONSE_INVALID,
    LoopbackSandboxConfig,
    SandboxAdapterError,
    SandboxCapabilities,
    SandboxDispatchAck,
    SandboxDispatchRequest,
    SandboxReadbackReceipt,
    canonical_digest,
    canonical_json,
    line_input_digest,
    queue_input_digest,
)


def _parse_envelope(raw: bytes, phase: str, maybe_sent: bool) -> dict[str, Any]:
    """Compatibility wrapper for ERP-003 envelope parsing."""
    return parse_envelope(raw, phase, maybe_sent)


class SyntheticERPHTTPAdapter:
    """A bounded, loopback-only HTTP client for the synthetic ERP sandbox."""

    def __init__(self, config: LoopbackSandboxConfig):
        if type(config) is not LoopbackSandboxConfig:
            raise SandboxAdapterError(REQUEST_INVALID, "config")
        try:
            self._config = LoopbackSandboxConfig.model_validate(
                config.model_dump(mode="json"))
        except (ValidationError, ValueError, TypeError):
            raise SandboxAdapterError(REQUEST_INVALID, "config") from None
        self._secret = self._config.secret.encode("utf-8")

    def __repr__(self) -> str:
        return (
            "SyntheticERPHTTPAdapter("
            f"host={self._config.host!r}, port={self._config.port!r}, "
            f"organization_id={self._config.organization_id!r}, "
            f"store_id={self._config.store_id!r})"
        )

    # -- public operations -------------------------------------------------

    def capabilities(self) -> None:
        nonce = _new_nonce()
        _, raw = self._perform(
            "GET", CAPABILITIES_PATH, nonce, None, "capabilities")
        data = _parse_envelope(raw, "capabilities", False)
        self._verify_signed(data, nonce, "capabilities", False)
        dto = self._validate(SandboxCapabilities, data, "capabilities", False)
        self._verify_common(dto, data, "capabilities", False, HANDSHAKE_FAILED)
        return None

    def dispatch(self, request: SandboxDispatchRequest) -> SandboxDispatchAck:
        validated = self._revalidate(request, "dispatch")
        self.capabilities()
        nonce = _new_nonce()
        body = canonical_json(validated.model_dump(mode="json")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise SandboxAdapterError(REQUEST_TOO_LARGE, "dispatch", False)
        _, raw = self._perform(
            "POST",
            BATCHES_PATH,
            nonce,
            body,
            "dispatch",
            idempotency_key=validated.batch_id,
        )
        data = _parse_envelope(raw, "dispatch", True)
        self._verify_signed(data, nonce, "dispatch", True)
        ack = self._validate(SandboxDispatchAck, data, "dispatch", True)
        self._verify_common(ack, data, "dispatch", True, RESPONSE_INVALID)
        if ack.batch_id != validated.batch_id:
            raise SandboxAdapterError(ACK_INVALID, "dispatch", True) from None
        if ack.input_digest != validated.input_digest:
            raise SandboxAdapterError(ACK_INVALID, "dispatch", True) from None
        return ack

    def readback(self, request: SandboxDispatchRequest) -> SandboxReadbackReceipt | None:
        validated = self._revalidate(request, "readback")
        self.capabilities()
        nonce = _new_nonce()
        status, raw = self._perform(
            "GET",
            f"{BATCHES_PATH}/{validated.batch_id}",
            nonce,
            None,
            "readback",
            allow_404=True,
        )
        if status == 404:
            return None
        data = _parse_envelope(raw, "readback", False)
        self._verify_signed(data, nonce, "readback", False)
        receipt = self._validate(SandboxReadbackReceipt, data, "readback", False)
        self._verify_common(receipt, data, "readback", False, RESPONSE_INVALID)
        if receipt.batch_id != validated.batch_id:
            raise SandboxAdapterError(LINE_MISMATCH, "readback", False) from None
        if receipt.input_digest != validated.input_digest:
            raise SandboxAdapterError(LINE_MISMATCH, "readback", False) from None
        self._match_lines(validated, receipt)
        return receipt

    # -- validation helpers ------------------------------------------------

    def _revalidate(self, request: Any, phase: str) -> SandboxDispatchRequest:
        if type(request) is not SandboxDispatchRequest:
            raise SandboxAdapterError(REQUEST_INVALID, phase)
        try:
            # Rebuild from the serialized content so a mutated or forged model
            # cannot smuggle stale digests past the frozen validators.
            revalidated = SandboxDispatchRequest.model_validate_json(
                request.model_dump_json())
        except (ValidationError, ValueError, TypeError, SandboxAdapterError, RecursionError):
            raise SandboxAdapterError(REQUEST_INVALID, phase) from None
        if (revalidated.organization_id != self._config.organization_id
                or revalidated.store_id != self._config.store_id):
            # Scope binding is local; a request for another tenant must never
            # reach capabilities, POST, or GET.
            raise SandboxAdapterError(REQUEST_INVALID, phase) from None
        return revalidated

    def _validate(self, model, data, phase: str, maybe_sent: bool):
        try:
            return model.model_validate(data)
        except (ValidationError, RecursionError):
            raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent) from None

    def _verify_common(
        self, dto, data, phase: str, maybe_sent: bool, code: str) -> None:
        required = (
            "contract_id", "adapter_id", "protocol_version", "mode",
            "organization_id", "store_id")
        if any(key not in data for key in required):
            raise SandboxAdapterError(code, phase, maybe_sent)
        if dto.contract_id != CONTRACT_ID or dto.adapter_id != ADAPTER_ID:
            raise SandboxAdapterError(code, phase, maybe_sent)
        if dto.organization_id != self._config.organization_id:
            raise SandboxAdapterError(code, phase, maybe_sent)
        if dto.store_id != self._config.store_id:
            raise SandboxAdapterError(code, phase, maybe_sent)

    def _verify_signed(self, data, nonce: str, phase: str, maybe_sent: bool) -> None:
        """Compatibility wrapper for ERP-003 signature verification."""
        verify_signed(self._config, data, nonce, phase, maybe_sent)

    def _match_lines(self, request: SandboxDispatchRequest,
                     receipt: SandboxReadbackReceipt) -> None:
        if len(receipt.lines) != len(request.lines):
            raise SandboxAdapterError(LINE_MISMATCH, "readback", False)
        expected = {line.line_id: line for line in request.lines}
        seen: set[str] = set()
        for line in receipt.lines:
            source = expected.get(line.line_id)
            if source is None or line.line_id in seen:
                raise SandboxAdapterError(LINE_MISMATCH, "readback", False)
            seen.add(line.line_id)
            if (
                line.line_no != source.line_no
                or line.intent_id != source.intent_id
                or line.command_id != source.command_id
                or line.input_digest != source.input_digest
            ):
                raise SandboxAdapterError(LINE_MISMATCH, "readback", False)

    # -- one bounded HTTP round trip ---------------------------------------

    def _perform(
        self,
        method: str,
        path: str,
        nonce: str,
        body: bytes | None,
        phase: str,
        *,
        allow_404: bool = False,
        idempotency_key: str | None = None,
    ) -> tuple[int, bytes]:
        """Compatibility wrapper for the shared bounded transport."""
        return perform_loopback_request(
            self._config, method, path, nonce, body, phase,
            allow_404=allow_404, idempotency_key=idempotency_key)


def _new_nonce() -> str:
    """Compatibility wrapper for ERP-003's 128-bit nonce generator."""
    return new_nonce()


__all__ = [
    "SandboxAdapterError",
    "SandboxDispatchRequest",
    "SandboxDispatchAck",
    "SandboxReadbackReceipt",
    "LoopbackSandboxConfig",
    "SyntheticERPHTTPAdapter",
    "canonical_digest",
    "queue_input_digest",
    "line_input_digest",
]
