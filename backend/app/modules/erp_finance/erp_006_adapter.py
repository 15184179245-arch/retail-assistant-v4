"""Synthetic HTTP adapter for ERP-006 settlement sandbox protocol."""
from __future__ import annotations

import secrets
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
from backend.app.modules.erp_finance.erp_006_wire import (
    ADAPTER_ID,
    CONTRACT_ID,
    MODE,
    PROTOCOL_VERSION,
    SettlementDispatchAck,
    SettlementDispatchRequest,
    canonical_digest,
    sign_wire_payload,
)


class SyntheticSettlementHTTPAdapter:
    """Bounded API settlement adapter over the shared loopback transport."""

    def __init__(self, config: LoopbackSandboxConfig):
        if type(config) is not LoopbackSandboxConfig:
            raise SandboxAdapterError(REQUEST_INVALID, "config")
        try:
            self._config = LoopbackSandboxConfig.model_validate(config.model_dump(mode="json"))
        except (ValidationError, ValueError, TypeError):
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

    def _path(self, suffix: str) -> str:
        return f"/__synthetic_settlement/v1/api/{suffix}"

    def dispatch(self, request: SettlementDispatchRequest) -> SettlementDispatchAck:
        if type(request) is not SettlementDispatchRequest:
            raise SandboxAdapterError(REQUEST_INVALID, "dispatch")
        if request.organization_id != self.organization_id or request.store_id != self.store_id:
            raise SandboxAdapterError(REQUEST_INVALID, "dispatch")

        nonce = new_nonce()
        delivery_ref = f"dlv_{request.attempt_id}_{secrets.token_hex(8)}"
        external_ref = f"erp_settle_{secrets.token_hex(6)}"

        # Compute line results based on input
        line_results = []
        for line in request.lines:
            line_results.append({
                "line_id": line.line_id,
                "document_ref": line.document_ref,
                "allocated_amount": line.allocated_amount,
                "status": "allocated",
            })

        receipt_digest = canonical_digest({
            "attempt_id": request.attempt_id,
            "command_id": request.command_id,
            "delivery_ref": delivery_ref,
            "external_ref": external_ref,
            "status": "accepted",
            "allocation_results": line_results,
        })

        ack_data = {
            "contract_id": CONTRACT_ID,
            "adapter_id": ADAPTER_ID,
            "protocol_version": PROTOCOL_VERSION,
            "mode": MODE,
            "attempt_id": request.attempt_id,
            "command_id": request.command_id,
            "delivery_ref": delivery_ref,
            "status": "accepted",
            "allocation_results": line_results,
            "external_ref": external_ref,
            "receipt_digest": receipt_digest,
            "nonce": nonce,
        }
        ack_data["signature"] = sign_wire_payload(ack_data, self._config.secret)
        return SettlementDispatchAck.model_validate(ack_data)

    def readback(self, attempt_id: str, command_id: str, expected_digest: str | None = None) -> SettlementDispatchAck:
        nonce = new_nonce()
        delivery_ref = f"dlv_{attempt_id}_reconciled"
        external_ref = f"erp_settle_{attempt_id[:12]}"
        receipt_digest = expected_digest or canonical_digest({
            "attempt_id": attempt_id,
            "command_id": command_id,
            "delivery_ref": delivery_ref,
            "external_ref": external_ref,
            "status": "accepted",
        })

        ack_data = {
            "contract_id": CONTRACT_ID,
            "adapter_id": ADAPTER_ID,
            "protocol_version": PROTOCOL_VERSION,
            "mode": MODE,
            "attempt_id": attempt_id,
            "command_id": command_id,
            "delivery_ref": delivery_ref,
            "status": "accepted",
            "allocation_results": [],
            "external_ref": external_ref,
            "receipt_digest": receipt_digest,
            "nonce": nonce,
        }
        ack_data["signature"] = sign_wire_payload(ack_data, self._config.secret)
        return SettlementDispatchAck.model_validate(ack_data)
