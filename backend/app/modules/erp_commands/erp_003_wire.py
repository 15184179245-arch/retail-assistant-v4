"""ERP-003 B2a: strict synthetic sandbox wire contract and loopback config.

This is a synthetic, local-only protocol invented for ERP-003. It is not a
Guanjiapo official API. The module has no database, no credentials input from
HTTP callers and no dynamic paths: the adapter may only talk to a numeric
loopback address configured by the trusted host, with a synthetic-only secret.

All wire DTOs are ``extra="forbid"`` and ``strict=True``; protocol_version is a
strict int equal to 1. Error codes are stable ASCII and never carry a secret,
URL, raw response or original exception.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import re
from typing import Annotated, Any, Literal, Mapping, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------------
# Frozen protocol constants (v1)
# --------------------------------------------------------------------------

CONTRACT_ID = "retail.erp-sandbox"
ADAPTER_ID = "synthetic.erp.v1"
MODE = "synthetic"
PROTOCOL_VERSION = 1

CAPABILITIES_PATH = "/__synthetic_erp/v1/capabilities"
BATCHES_PATH = "/__synthetic_erp/v1/batches"
NONCE_HEADER = "X-Sandbox-Nonce"
KEY_HEADER = "X-Sandbox-Key"
IDEMPOTENCY_HEADER = "Idempotency-Key"

ENVELOPE_FIELDS = ("success", "data", "code", "error", "user_message", "hint")
SIGNATURE_ALGORITHM = "HMAC-SHA256"
SIGNED_RESPONSE_COMMON_FIELDS = (
    "contract_id", "adapter_id", "protocol_version", "mode",
    "organization_id", "store_id",
)
_ACKNOWLEDGED_TRUE = True
CANONICAL_JSON = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": [",", ":"],
    "allow_nan": False,
    "encoding": "utf-8",
}

MAX_REQUEST_BYTES = 262_144
MAX_RESPONSE_BYTES = 131_072
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 10.0
MIN_TIMEOUT_SECONDS = 0.001

NONCE_PATTERN = r"^[0-9a-f]{32}$"
SIGNATURE_PATTERN = r"^[0-9a-f]{64}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
ERROR_CODE_PATTERN = r"^[A-Z][A-Z0-9_]*$"
SECRET_PATTERN = r"^sandbox_[A-Za-z0-9._~-]{24,192}$"

# Stable error codes. No free text ever reaches a caller.
CONFIG_INVALID = "sandbox_config_invalid"
REQUEST_INVALID = "sandbox_request_invalid"
REQUEST_TOO_LARGE = "sandbox_request_too_large"
DIGEST_INVALID = "sandbox_digest_invalid"
HANDSHAKE_FAILED = "sandbox_handshake_failed"
TRANSPORT_FAILED = "sandbox_transport_failed"
TIMEOUT = "sandbox_timeout"
REDIRECT_REJECTED = "sandbox_redirect_rejected"
STATUS_REJECTED = "sandbox_status_rejected"
RESPONSE_TOO_LARGE = "sandbox_response_too_large"
RESPONSE_INVALID = "sandbox_response_invalid"
LINE_MISMATCH = "sandbox_line_mismatch"
ACK_INVALID = "sandbox_ack_invalid"

_ALLOWED_PHASES = ("config", "capabilities", "dispatch", "readback")


class SandboxAdapterError(Exception):
    """A stable, secret-free sandbox adapter failure.

    ``maybe_sent`` is conservative: once a dispatch POST attempt has started
    (even if it failed before a response arrived) it is True, so a caller can
    never assume the command was not executed. Exception chaining is suppressed
    by every raise site in the adapter.
    """

    def __init__(self, code: str, phase: str, maybe_sent: bool = False):
        if not re.fullmatch(r"^[a-z][a-z0-9_]*$", code or ""):
            code = RESPONSE_INVALID
        if phase not in _ALLOWED_PHASES:
            phase = "config"
        self.code = code
        self.phase = phase
        self.maybe_sent = bool(maybe_sent)
        super().__init__(
            f"sandbox adapter error: code={self.code} phase={self.phase} "
            f"maybe_sent={'true' if self.maybe_sent else 'false'}"
        )

    def __str__(self) -> str:
        return (
            f"sandbox adapter error: code={self.code} phase={self.phase} "
            f"maybe_sent={'true' if self.maybe_sent else 'false'}"
        )


# --------------------------------------------------------------------------
# Canonical JSON and digests
# --------------------------------------------------------------------------

def canonical_json(value: Any) -> str:
    """The frozen canonical JSON form: UTF-8, sorted keys, no NaN/Infinity."""

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        # Lone surrogates survive json.dumps but cannot be UTF-8 encoded.
        text.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise SandboxAdapterError(DIGEST_INVALID, "config") from None
    return text


def canonical_digest(value: Any) -> str:
    """Lowercase hex SHA-256 of the frozen canonical JSON form."""

    payload = canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def signature_for(secret: "str | bytes", data: Mapping[str, Any]) -> str:
    """HMAC-SHA256 over the whole data object except ``signature``."""

    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    payload = {k: v for k, v in data.items() if k != "signature"}
    canonical = canonical_json(payload).encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# Strict base and shared field types
# --------------------------------------------------------------------------

Identifier = Annotated[
    str,
    Field(min_length=1, max_length=36, pattern=IDENTIFIER_PATTERN),
]
Digest = Annotated[str, Field(pattern=DIGEST_PATTERN)]
Nonce = Annotated[str, Field(pattern=NONCE_PATTERN)]
Signature = Annotated[str, Field(pattern=SIGNATURE_PATTERN)]
Text400 = Annotated[str, Field(min_length=1, max_length=400)]


class StrictDTO(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
    )


def _exact_int(value: Any, *, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError("expected a bounded integer")
    return value


def _synthetic_text(value: Any) -> str:
    """Business parameter text: 1..400 chars, already trimmed, no control chars."""

    if not isinstance(value, str):
        raise ValueError("expected text")
    if not 1 <= len(value) <= 400 or value.strip() != value:
        raise ValueError("expected trimmed text of 1..400 characters")
    if any(ord(ch) < 0x20 for ch in value):
        raise ValueError("control characters are not allowed")
    return value


class WireCommon(StrictDTO):
    contract_id: Literal["retail.erp-sandbox"] = CONTRACT_ID
    adapter_id: Literal["synthetic.erp.v1"] = ADAPTER_ID
    protocol_version: Literal[1] = PROTOCOL_VERSION
    mode: Literal["synthetic"] = MODE
    organization_id: Identifier
    store_id: Identifier

    @field_validator("protocol_version", mode="before")
    @classmethod
    def protocol_is_exact_int_one(cls, value):
        # json true / 1.0 / "1" must all fail: only Python int 1 is accepted.
        if type(value) is not int or value != PROTOCOL_VERSION:
            raise ValueError("unsupported sandbox protocol")
        return PROTOCOL_VERSION


# --------------------------------------------------------------------------
# Restricted business parameters (old ERP-002 shapes only)
# --------------------------------------------------------------------------

class CatalogFieldParameters(StrictDTO):
    field_name: Literal["display_label", "specification_note"]
    proposed_value: Text400

    @field_validator("proposed_value", mode="before")
    @classmethod
    def trimmed(cls, value):
        return _synthetic_text(value)


class PurchaseDocumentParameters(StrictDTO):
    memo: Text400

    @field_validator("memo", mode="before")
    @classmethod
    def trimmed(cls, value):
        return _synthetic_text(value)


class SalesReturnParameters(StrictDTO):
    reason: Text400

    @field_validator("reason", mode="before")
    @classmethod
    def trimmed(cls, value):
        return _synthetic_text(value)


BusinessParameters = (
    CatalogFieldParameters | PurchaseDocumentParameters | SalesReturnParameters
)

_ACTION_TARGET = {
    "catalog_field_proposal": ("catalog_product", CatalogFieldParameters),
    "purchase_document_proposal": ("purchase_intent", PurchaseDocumentParameters),
    "sales_return_proposal": ("sales_draft", SalesReturnParameters),
}


class SandboxLineRequest(StrictDTO):
    line_id: Identifier
    line_no: int = Field(ge=1, le=50)
    intent_id: Identifier
    command_id: Identifier
    request_digest: Digest
    action: Literal[
        "catalog_field_proposal",
        "purchase_document_proposal",
        "sales_return_proposal",
    ]
    target_kind: Literal["catalog_product", "purchase_intent", "sales_draft"]
    target_id: Identifier
    target_version: int = Field(ge=1, le=2_147_483_647)
    target_digest: Digest
    source_sha256: Digest
    parameters: BusinessParameters
    parameters_digest: Digest
    input_digest: Digest

    @field_validator("line_no", mode="before")
    @classmethod
    def line_no_exact_positive_int(cls, value):
        return _exact_int(value, low=1, high=50)

    @field_validator("target_version", mode="before")
    @classmethod
    def target_version_exact_positive_int(cls, value):
        return _exact_int(value, low=1, high=2_147_483_647)

    @model_validator(mode="after")
    def action_target_parameters_agree(self):
        expected_kind, expected_type = _ACTION_TARGET[self.action]
        if self.target_kind != expected_kind:
            raise ValueError("action and target_kind do not match")
        if type(self.parameters) is not expected_type:
            raise ValueError("parameters do not match the declared action")
        if self.parameters_digest != canonical_digest(self.parameters.model_dump()):
            raise ValueError("parameters_digest does not match parameters")
        if self.input_digest != line_input_digest(self):
            raise ValueError("line input_digest does not match the line content")
        return self


class SandboxDispatchRequest(WireCommon):
    batch_id: Identifier
    input_digest: Digest
    lines: list[SandboxLineRequest] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def lines_are_contiguous_unique_and_bound(self):
        numbers = {line.line_no for line in self.lines}
        if numbers != set(range(1, len(self.lines) + 1)):
            raise ValueError("line_no must be contiguous 1..n")
        for attribute in ("line_id", "intent_id", "command_id"):
            values = [getattr(line, attribute) for line in self.lines]
            if len(set(values)) != len(values):
                raise ValueError(f"duplicate {attribute}")
        expected = queue_input_digest(
            self.organization_id,
            self.store_id,
            self.adapter_id,
            self.lines,
        )
        if self.input_digest != expected:
            raise ValueError("batch input_digest does not match the command set")
        return self


class SandboxCapabilities(WireCommon):
    nonce: Nonce
    signature: Signature


class SandboxDispatchAck(WireCommon):
    batch_id: Identifier
    input_digest: Digest
    acknowledged: bool
    nonce: Nonce
    signature: Signature

    @field_validator("acknowledged", mode="before")
    @classmethod
    def acknowledged_must_be_true(cls, value):
        if value is not _ACKNOWLEDGED_TRUE:
            raise ValueError("acknowledgement must be true")
        return _ACKNOWLEDGED_TRUE


class SandboxReadbackLine(StrictDTO):
    line_id: Identifier
    line_no: int = Field(ge=1, le=50)
    intent_id: Identifier
    command_id: Identifier
    input_digest: Digest
    status: Literal["accepted", "rejected", "unknown"]
    external_ref: str | None = Field(default=None, min_length=1, max_length=200)
    error_code: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=ERROR_CODE_PATTERN
    )

    @field_validator("line_no", mode="before")
    @classmethod
    def line_no_exact_positive_int(cls, value):
        return _exact_int(value, low=1, high=50)

    @field_validator("external_ref")
    @classmethod
    def external_ref_is_plain_text(cls, value):
        if value is not None and (value.strip() != value or any(
                ord(ch) < 0x20 for ch in value)):
            raise ValueError("external_ref must be trimmed printable text")
        return value

    @model_validator(mode="after")
    def status_evidence_is_consistent(self):
        if self.status == "accepted":
            if self.external_ref is None:
                raise ValueError("accepted line requires an external_ref")
            if self.error_code is not None:
                raise ValueError("accepted line cannot carry an error_code")
        else:
            if self.external_ref is not None:
                raise ValueError("unverified line cannot invent an external_ref")
            if self.status == "rejected" and self.error_code is None:
                raise ValueError("rejected line requires a stable error_code")
        return self


class SandboxReadbackReceipt(WireCommon):
    batch_id: Identifier
    input_digest: Digest
    lines: list[SandboxReadbackLine] = Field(min_length=1, max_length=50)
    nonce: Nonce
    signature: Signature

    @model_validator(mode="after")
    def lines_are_unique_and_contiguous(self):
        if len({line.line_id for line in self.lines}) != len(self.lines):
            raise ValueError("duplicate readback line_id")
        if {line.line_no for line in self.lines} != set(
                range(1, len(self.lines) + 1)):
            raise ValueError("readback line_no must be contiguous 1..n")
        return self


# --------------------------------------------------------------------------
# Digest helpers (frozen public names; the root service will call these)
# --------------------------------------------------------------------------

def line_input_digest(line: "SandboxLineRequest | Mapping[str, Any]") -> str:
    """Canonical digest of a line excluding ``input_digest`` itself."""

    if isinstance(line, SandboxLineRequest):
        data = line.model_dump(exclude={"input_digest"})
    elif isinstance(line, Mapping):
        data = {key: value for key, value in line.items() if key != "input_digest"}
    else:
        raise SandboxAdapterError(REQUEST_INVALID, "config") from None
    return canonical_digest(data)


def _command_ref(item: Any) -> tuple[int | None, dict[str, str]]:
    if isinstance(item, SandboxLineRequest):
        return item.line_no, {
            "command_id": item.command_id,
            "request_digest": item.request_digest,
        }
    if isinstance(item, Mapping):
        if "command_id" not in item or "request_digest" not in item:
            raise SandboxAdapterError(REQUEST_INVALID, "config") from None
        return item.get("line_no"), {
            "command_id": item["command_id"],
            "request_digest": item["request_digest"],
        }
    if isinstance(item, (tuple, list)) and len(item) == 2:
        return None, {"command_id": item[0], "request_digest": item[1]}
    raise SandboxAdapterError(REQUEST_INVALID, "config") from None


def queue_input_digest(
    organization_id: str,
    store_id: str,
    adapter_id: str,
    commands: Sequence[Any],
) -> str:
    """The batch input digest: the reviewed command set, ordered by line_no.

    It deliberately excludes ``batch_id`` so a replay of the same reviewed
    command set keeps the same digest.
    """

    refs = [_command_ref(item) for item in (commands or [])]
    if refs and all(number is not None for number, _ in refs):
        refs.sort(key=lambda ref: ref[0])
    payload = {
        "organization_id": organization_id,
        "store_id": store_id,
        "adapter_id": adapter_id,
        "commands": [ref for _, ref in refs],
    }
    return canonical_digest(payload)


# --------------------------------------------------------------------------
# Trusted-host loopback configuration
# --------------------------------------------------------------------------

_CREDENTIAL_MARKERS = (
    "bearer",
    "authorization",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret_key",
    "sk-",
    "sk_",
    "token",
    "jwt",
    "-----begin",
    "erp_",
    "employee",
)


class LoopbackSandboxConfig(StrictDTO):
    """Trusted-host-only configuration; the secret never enters repr or errors."""

    host: str
    port: int
    organization_id: Identifier
    store_id: Identifier
    secret: str = Field(repr=False, min_length=32, max_length=200)
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @field_validator("host")
    @classmethod
    def numeric_loopback_only(cls, value):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise ValueError("host must be a numeric IP address") from None
        if not address.is_loopback:
            raise ValueError("host must be a loopback address")
        return value

    @field_validator("port", mode="before")
    @classmethod
    def bounded_port(cls, value):
        if type(value) is not int:
            raise ValueError("port must be an integer")
        if not 1 <= value <= 65535:
            raise ValueError("port out of range")
        return value

    @field_validator("timeout", mode="before")
    @classmethod
    def bounded_timeout(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("timeout must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("timeout must be finite")
        if not MIN_TIMEOUT_SECONDS <= number <= MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout out of range")
        return number

    @field_validator("secret")
    @classmethod
    def synthetic_secret_only(cls, value):
        if not isinstance(value, str):
            raise ValueError("secret must be text")
        if not 32 <= len(value) <= 200:
            raise ValueError("secret length out of range")
        if "\r" in value or "\n" in value or value.strip() != value:
            raise ValueError("secret must not contain whitespace padding")
        if re.fullmatch(SECRET_PATTERN, value) is None:
            raise ValueError("secret must be a synthetic sandbox secret")
        lowered = value.lower()
        if any(marker in lowered for marker in _CREDENTIAL_MARKERS):
            raise ValueError("secret must not look like an ERP/employee/API credential")
        return value


def _signed_response_schema(model: type[WireCommon]) -> dict[str, Any]:
    schema = model.model_json_schema()
    required = schema["required"]
    required.extend(field for field in SIGNED_RESPONSE_COMMON_FIELDS if field not in required)
    if model is SandboxDispatchAck:
        schema["properties"]["acknowledged"]["const"] = _ACKNOWLEDGED_TRUE
    return schema


def build_contract_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ERP-003 synthetic ERP sandbox wire contract v1",
        "description": (
            "Generated from backend/app/modules/erp_commands/erp_003_wire.py. "
            "Synthetic loopback-only protocol; it is not a Guanjiapo official API."
        ),
        "contract_id": CONTRACT_ID,
        "adapter_id": ADAPTER_ID,
        "mode": MODE,
        "protocol_version": PROTOCOL_VERSION,
        "envelope_fields": list(ENVELOPE_FIELDS),
        "signature_algorithm": {
            "name": SIGNATURE_ALGORITHM,
            "nonce_header": NONCE_HEADER,
            "secret_header": KEY_HEADER,
            "signed_bytes": (
                "UTF-8 canonical JSON of response data with the signature key "
                "removed (sorted keys, compact separators, no NaN)"
            ),
            "signature_format": "lowercase hex, 64 characters",
            "comparison": "constant-time",
        },
        "capabilities": _signed_response_schema(SandboxCapabilities),
        "dispatch_request": SandboxDispatchRequest.model_json_schema(),
        "dispatch_ack": _signed_response_schema(SandboxDispatchAck),
        "readback_receipt": _signed_response_schema(SandboxReadbackReceipt),
    }


__all__ = [
    "CONTRACT_ID",
    "ADAPTER_ID",
    "MODE",
    "PROTOCOL_VERSION",
    "CAPABILITIES_PATH",
    "BATCHES_PATH",
    "NONCE_HEADER",
    "KEY_HEADER",
    "IDEMPOTENCY_HEADER",
    "ENVELOPE_FIELDS",
    "SIGNATURE_ALGORITHM",
    "SIGNED_RESPONSE_COMMON_FIELDS",
    "CANONICAL_JSON",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "SandboxAdapterError",
    "canonical_json",
    "canonical_digest",
    "signature_for",
    "SandboxLineRequest",
    "SandboxDispatchRequest",
    "SandboxCapabilities",
    "SandboxDispatchAck",
    "SandboxReadbackLine",
    "SandboxReadbackReceipt",
    "line_input_digest",
    "queue_input_digest",
    "build_contract_schema",
    "LoopbackSandboxConfig",
]
