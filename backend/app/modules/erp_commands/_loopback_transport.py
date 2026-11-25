"""Private bounded HTTP transport shared by the synthetic ERP adapters.

This module deliberately accepts no URL, arbitrary headers, proxy settings or
transport callbacks. Callers can use only the frozen ERP-003 endpoints or the
two fixed synthetic-sales routes and the single receiving route over the trusted numeric-loopback config.
"""
from __future__ import annotations

import hmac
import http.client
import json
import re
import secrets
import socket
import threading
import time
from typing import Any

from pydantic import ValidationError

from backend.app.modules.erp_commands.erp_003_wire import (
    BATCHES_PATH,
    CAPABILITIES_PATH,
    ENVELOPE_FIELDS,
    IDEMPOTENCY_HEADER,
    KEY_HEADER,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    NONCE_HEADER,
    REDIRECT_REJECTED,
    REQUEST_INVALID,
    REQUEST_TOO_LARGE,
    RESPONSE_INVALID,
    RESPONSE_TOO_LARGE,
    STATUS_REJECTED,
    TIMEOUT,
    TRANSPORT_FAILED,
    LoopbackSandboxConfig,
    SandboxAdapterError,
    signature_for,
)

_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,35}$")
_READ_CHUNK = 65_536
# Only the exact catalog image-upload endpoint accepts the 5 MiB base64 envelope.
MAX_CATALOG_UPLOAD_BYTES = 7_340_032
_ALLOWED_PHASES = frozenset(("capabilities", "dispatch", "readback"))


def _invalid(phase: str) -> SandboxAdapterError:
    safe_phase = (
        phase if isinstance(phase, str) and phase in _ALLOWED_PHASES else "config"
    )
    return SandboxAdapterError(REQUEST_INVALID, safe_phase)


def _config(config: LoopbackSandboxConfig, phase: str) -> LoopbackSandboxConfig:
    if type(config) is not LoopbackSandboxConfig:
        raise _invalid(phase)
    try:
        return LoopbackSandboxConfig.model_validate(config.model_dump(mode="json"))
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise _invalid(phase) from None


def _path_phase(method: str, path: str) -> str | None:
    if method == "GET" and path == CAPABILITIES_PATH:
        return "capabilities"
    if method == "POST" and path == BATCHES_PATH:
        return "dispatch"
    erp_readback = BATCHES_PATH + "/"
    if method == "GET" and path.startswith(erp_readback):
        ident = path[len(erp_readback):]
        if _IDENTIFIER_RE.fullmatch(ident):
            return "readback"

    sales_base = "/__synthetic_sales/v1/"
    for route in ("api", "pos"):
        route_base = sales_base + route
        if method == "GET" and path == route_base + "/capabilities":
            return "capabilities"
        if method == "POST" and path == route_base + "/commands":
            return "dispatch"
        readback = route_base + "/commands/"
        if method == "GET" and path.startswith(readback):
            ident = path[len(readback):]
            if _IDENTIFIER_RE.fullmatch(ident):
                return "readback"
    receipt_base = "/__synthetic_receipts/v1/api"
    if method == "GET" and path == receipt_base + "/capabilities":
        return "capabilities"
    if method == "POST" and path == receipt_base + "/commands":
        return "dispatch"
    receipt_readback = receipt_base + "/commands/"
    if method == "GET" and path.startswith(receipt_readback):
        if _IDENTIFIER_RE.fullmatch(path[len(receipt_readback):]):
            return "readback"
    catalog_base = "/__synthetic_catalog/v1/api"
    if method == "GET" and path == catalog_base + "/capabilities":
        return "capabilities"
    for resource in ("commands", "uploads"):
        target = catalog_base + "/" + resource
        if method == "POST" and path == target:
            return "dispatch"
        if method == "GET" and path.startswith(target + "/"):
            if _IDENTIFIER_RE.fullmatch(path[len(target) + 1:]):
                return "readback"
    return None


def perform_loopback_request(
    config: LoopbackSandboxConfig,
    method: str,
    path: str,
    nonce: str,
    body: bytes | None,
    phase: str,
    *,
    allow_404: bool = False,
    idempotency_key: str | None = None,
) -> tuple[int, bytes]:
    """Perform one request to an allowlisted synthetic protocol endpoint."""

    safe_phase = (
        phase if isinstance(phase, str) and phase in _ALLOWED_PHASES else "config"
    )
    trusted = _config(config, safe_phase)
    if (type(method) is not str or type(path) is not str
            or type(phase) is not str or phase not in _ALLOWED_PHASES
            or _path_phase(method, path) != phase
            or type(nonce) is not str or _NONCE_RE.fullmatch(nonce) is None
            or type(allow_404) is not bool):
        raise _invalid(safe_phase)

    if method == "GET":
        if body is not None or idempotency_key is not None:
            raise _invalid(phase)
        if allow_404 and phase != "readback":
            raise _invalid(phase)
    else:
        if (type(body) is not bytes or not body
                or type(idempotency_key) is not str
                or _IDENTIFIER_RE.fullmatch(idempotency_key) is None):
            raise _invalid(phase)
        body_limit = (MAX_CATALOG_UPLOAD_BYTES if path == "/__synthetic_catalog/v1/api/uploads"
                      else MAX_REQUEST_BYTES)
        if len(body) > body_limit:
            raise SandboxAdapterError(REQUEST_TOO_LARGE, phase, False)
        if allow_404:
            raise _invalid(phase)

    deadline = time.monotonic() + trusted.timeout
    connection = http.client.HTTPConnection(
        trusted.host, trusted.port, timeout=trusted.timeout)
    timer: threading.Timer | None = None
    sent = False
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxAdapterError(TIMEOUT, phase, False)
        connection.timeout = remaining
        connection.connect()
        sock = connection.sock
        if sock is None:
            raise SandboxAdapterError(TRANSPORT_FAILED, phase, False)

        def _kill() -> None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(max(0.0, deadline - time.monotonic()), _kill)
        timer.name = "sandbox-deadline-kill"
        timer.daemon = True
        timer.start()

        headers = {NONCE_HEADER: nonce, KEY_HEADER: trusted.secret}
        if method == "POST":
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Accept"] = "application/json"
            headers[IDEMPOTENCY_HEADER] = idempotency_key
        else:
            headers["Accept"] = "application/json"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxAdapterError(TIMEOUT, phase, sent)
        sock.settimeout(remaining)
        # Set before request(): any exception from this point leaves POST
        # outcome conservative and must be recovered by a read-only GET.
        sent = method == "POST"
        connection.request(method, path, body=body, headers=headers)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxAdapterError(TIMEOUT, phase, sent)
        sock.settimeout(remaining)
        response = connection.getresponse()
        status = response.status

        if 300 <= status < 400:
            raise SandboxAdapterError(REDIRECT_REJECTED, phase, sent)
        if status == 404 and allow_404:
            return 404, b""
        if not 200 <= status < 300:
            raise SandboxAdapterError(STATUS_REJECTED, phase, sent)

        expected_length: int | None = None
        length_header = response.getheader("Content-Length")
        if length_header is not None:
            try:
                expected_length = int(length_header)
            except (TypeError, ValueError):
                raise SandboxAdapterError(RESPONSE_INVALID, phase, sent) from None
            if expected_length < 0 or expected_length > MAX_RESPONSE_BYTES:
                raise SandboxAdapterError(RESPONSE_TOO_LARGE, phase, sent)

        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxAdapterError(TIMEOUT, phase, sent)
            sock.settimeout(remaining)
            chunk = response.read(min(_READ_CHUNK, MAX_RESPONSE_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_RESPONSE_BYTES:
                raise SandboxAdapterError(RESPONSE_TOO_LARGE, phase, sent)
        if expected_length is not None and len(chunks) != expected_length:
            code = (TIMEOUT if time.monotonic() >= deadline - 0.05
                    else RESPONSE_INVALID)
            raise SandboxAdapterError(code, phase, sent)
        return status, bytes(chunks)
    except SandboxAdapterError:
        raise
    except (socket.timeout, TimeoutError):
        raise SandboxAdapterError(TIMEOUT, phase, sent) from None
    except (OSError, http.client.HTTPException):
        code = TIMEOUT if time.monotonic() >= deadline - 0.05 else TRANSPORT_FAILED
        raise SandboxAdapterError(code, phase, sent) from None
    finally:
        if timer is not None:
            timer.cancel()
            try:
                timer.join(timeout=1.0)
            except RuntimeError:
                pass
        try:
            connection.close()
        except OSError:
            pass


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_envelope(raw: bytes, phase: str, maybe_sent: bool) -> dict[str, Any]:
    """Parse the exact six-field API envelope and reject ambiguous JSON."""

    if type(raw) is not bytes:
        raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SandboxAdapterError(RESPONSE_TOO_LARGE, phase, maybe_sent)
    try:
        envelope = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent) from None
    if not isinstance(envelope, dict):
        raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent)
    if set(envelope) != set(ENVELOPE_FIELDS) or len(envelope) != len(ENVELOPE_FIELDS):
        raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent)
    if envelope["success"] is not True:
        raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent)
    if not isinstance(envelope["code"], str) or not isinstance(
            envelope["user_message"], str):
        raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent)
    if envelope["error"] is not None and not isinstance(envelope["error"], str):
        raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent)
    if envelope["hint"] is not None and not isinstance(envelope["hint"], str):
        raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent)
    data = envelope["data"]
    if not isinstance(data, dict):
        raise SandboxAdapterError(RESPONSE_INVALID, phase, maybe_sent)
    return data


def verify_signed(
    config: LoopbackSandboxConfig,
    data: dict[str, Any],
    nonce: str,
    phase: str,
    maybe_sent: bool,
) -> None:
    """Verify the synthetic response HMAC and the request-specific nonce."""

    safe_phase = (
        phase if isinstance(phase, str) and phase in _ALLOWED_PHASES else "config"
    )
    trusted = _config(config, safe_phase)
    if (not isinstance(data, dict) or type(nonce) is not str
            or _NONCE_RE.fullmatch(nonce) is None or type(maybe_sent) is not bool):
        raise SandboxAdapterError(RESPONSE_INVALID, safe_phase, bool(maybe_sent))
    signature = data.get("signature")
    if not isinstance(signature, str) or _SIGNATURE_RE.fullmatch(signature) is None:
        raise SandboxAdapterError(RESPONSE_INVALID, safe_phase, maybe_sent)
    try:
        expected = signature_for(trusted.secret, data)
    except (SandboxAdapterError, TypeError, ValueError, RecursionError):
        raise SandboxAdapterError(RESPONSE_INVALID, safe_phase, maybe_sent) from None
    if not hmac.compare_digest(expected, signature):
        raise SandboxAdapterError(RESPONSE_INVALID, safe_phase, maybe_sent)
    if not isinstance(data.get("nonce"), str) or data["nonce"] != nonce:
        raise SandboxAdapterError(RESPONSE_INVALID, safe_phase, maybe_sent)


def new_nonce() -> str:
    return secrets.token_hex(16)

