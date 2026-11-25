"""Strict, cancellable Chat Completions HTTP adapter.

This module deliberately uses only the standard library and never logs request
or response material. Provider output remains an untrusted suggestion.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import math
import os
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Mapping
from urllib.parse import urlsplit

from backend.app.core.model_provider import ProviderRequest, ProviderResponse
from backend.app.core.model_context import (
    PROTOCOL_RESERVE, context_window, estimate_tokens,
)


_RETRYABLE = {"provider_timeout", "provider_network_error", "provider_rate_limited", "provider_unavailable"}
_MAX_REQUEST_BYTES = 1_048_576


class ProviderError(RuntimeError):
    """Safe public error; its displayed text is always just a stable code."""

    def __init__(
        self,
        code: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        self.code = code
        self.retryable = code in _RETRYABLE
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: float = 30.0
    max_response_bytes: int = 1_048_576
    token_parameter: str = "max_completion_tokens"
    allow_loopback_http: bool = False
    context_window_tokens: int | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ProviderConfig":
        env = os.environ if environ is None else environ
        names = ("AGENT_PROVIDER_BASE_URL", "AGENT_PROVIDER_API_KEY", "AGENT_PROVIDER_MODEL")
        vals = [env.get(name, "") for name in names]
        if any(not isinstance(value, str) or not value.strip() for value in vals):
            raise ProviderError("provider_not_configured") from None

        try:
            timeout_raw = env.get("AGENT_PROVIDER_TIMEOUT_SECONDS", "")
            limit_raw = env.get("AGENT_PROVIDER_MAX_RESPONSE_BYTES", "")
            token_raw = env.get("AGENT_PROVIDER_TOKEN_PARAMETER", "")
            loop_raw = env.get("AGENT_PROVIDER_ALLOW_LOOPBACK_HTTP", "")
            if not all(isinstance(value, str) for value in (timeout_raw, limit_raw, token_raw, loop_raw)):
                raise ValueError
            timeout = 30.0 if not timeout_raw.strip() else float(timeout_raw)
            limit = 1_048_576 if not limit_raw.strip() else int(limit_raw)
            token_parameter = "max_completion_tokens" if not token_raw.strip() else token_raw.strip()
            if loop_raw.strip() not in ("", "0", "1"):
                raise ValueError
            allow_loopback = loop_raw.strip() == "1"
            cfg = cls(vals[0], vals[1], vals[2], timeout, limit, token_parameter,
                      allow_loopback, context_window(vals[2], environ=env, base_url=vals[0]))
            _validate_config(cfg)
            return cfg
        except (ValueError, TypeError, OverflowError):
            raise ProviderError("provider_configuration_invalid") from None


def _ascii_visible(value: str, *, allow_space: bool = False) -> bool:
    if not value or not value.isascii():
        return False
    return all((ch == " " if allow_space else False) or ("!" <= ch <= "~") for ch in value)


def _validate_config(config: ProviderConfig) -> tuple[str, str, int, bool]:
    try:
        if type(config.allow_loopback_http) is not bool:
            raise ValueError
        if (config.context_window_tokens is not None
                and (type(config.context_window_tokens) is not int
                     or not 128 <= config.context_window_tokens <= 16_777_216)):
            raise ValueError
        if type(config.timeout_seconds) not in (int, float):
            raise ValueError
        timeout = float(config.timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
            raise ValueError
        if type(config.max_response_bytes) is not int or not 1 <= config.max_response_bytes <= 8_388_608:
            raise ValueError
        if config.token_parameter not in ("max_completion_tokens", "max_tokens"):
            raise ValueError
        if not _ascii_visible(config.model) or len(config.model) > 200:
            raise ValueError
        if not _ascii_visible(config.api_key) or len(config.api_key) > 4096:
            raise ValueError
        if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in config.base_url) or not config.base_url.isascii():
            raise ValueError
        parts = urlsplit(config.base_url)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError
        if parts.query or parts.fragment:
            raise ValueError
        port = parts.port
        host = parts.hostname
        if not host or any(ch.isspace() for ch in host):
            raise ValueError
        if parts.netloc.endswith(":"):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if parts.scheme == "http":
            if not config.allow_loopback_http:
                raise ValueError
            try:
                if not ip_address(host).is_loopback:
                    raise ValueError
            except ValueError:
                raise ValueError from None
        path = parts.path.rstrip("/")
        if "\\" in path or any(ord(ch) < 32 for ch in path):
            raise ValueError
        authority = parts.netloc
        if not authority or "://" in authority:
            raise ValueError
        return parts.scheme, host, port or (443 if parts.scheme == "https" else 80), parts.scheme == "https"
    except (ValueError, UnicodeError, AttributeError, TypeError):
        raise ProviderError("provider_configuration_invalid") from None


class _Transport:
    """Owns the worker socket and interrupts it when its absolute deadline fires."""

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.sock: socket.socket | None = None

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.stopped.is_set():
            raise TimeoutError
        return remaining

    def install_socket(self, sock: socket.socket) -> None:
        with self.lock:
            if self.stopped.is_set() or time.monotonic() >= self.deadline:
                sock.close()
                raise TimeoutError
            self.sock = sock

    def stop(self) -> None:
        with self.lock:
            self.stopped.set()
            sock = self.sock
            self.sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


class _DirectConnection(http.client.HTTPConnection):
    """HTTPConnection with explicit DNS/socket ownership and no environment proxy."""

    def __init__(self, host: str, port: int, transport: _Transport, use_tls: bool):
        super().__init__(host, port, timeout=transport.remaining())
        self.transport = transport
        self.use_tls = use_tls

    def connect(self) -> None:
        tr = self.transport
        # DNS may not be interruptible, but no socket or credential-bearing POST
        # is created after it returns if cancellation/deadline has fired.
        tr.remaining()
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        last_error: OSError | None = None
        for family, socktype, proto, _canonname, address in addresses:
            tr.remaining()
            raw = socket.socket(family, socktype, proto)
            try:
                raw.settimeout(tr.remaining())
                tr.install_socket(raw)
                raw.connect(address)
                tr.remaining()
                if self.use_tls:
                    tls = ssl.create_default_context().wrap_socket(
                        raw, server_hostname=self.host, do_handshake_on_connect=False
                    )
                    with tr.lock:
                        if tr.stopped.is_set() or time.monotonic() >= tr.deadline:
                            tls.close()
                            raise TimeoutError
                        tr.sock = tls
                    self.sock = tls
                    tls.settimeout(tr.remaining())
                    tls.do_handshake()
                    tr.remaining()
                else:
                    self.sock = raw
                return
            except TimeoutError:
                raw.close()
                raise
            except ssl.SSLError:
                with tr.lock:
                    failed_sock = tr.sock
                    tr.sock = None
                if failed_sock is not None:
                    try:
                        failed_sock.close()
                    except OSError:
                        pass
                raw.close()
                raise
            except OSError as exc:
                last_error = exc
                with tr.lock:
                    if tr.sock is raw:
                        tr.sock = None
                raw.close()
        if last_error is not None:
            raise last_error
        raise OSError


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError
    return parsed


def _check_json_depth(root: object, maximum: int = 64) -> None:
    """Reject adversarially nested JSON without using Python recursion."""
    pending: list[tuple[object, int]] = [(root, 0)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, dict):
            if depth >= maximum and value:
                raise ValueError
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            if depth >= maximum and value:
                raise ValueError
            pending.extend((child, depth + 1) for child in value)


class ChatCompletionsProvider:
    def __init__(self, config: ProviderConfig):
        if not isinstance(config, ProviderConfig):
            raise ProviderError("provider_configuration_invalid") from None
        self.config = config
        self._parts = _validate_config(config)

    async def complete(self, request: ProviderRequest, /) -> ProviderResponse:
        try:
            if not isinstance(request, ProviderRequest):
                raise ProviderError("provider_invalid_request") from None
            body = self._make_body(request)
            window = (self.config.context_window_tokens
                      or context_window(self.config.model, environ={}, base_url=self.config.base_url))
            # JSON escaping/UTF-8 must not reintroduce the old 1MiB context cap.
            request_bytes = max(_MAX_REQUEST_BYTES, window * 16) if window else _MAX_REQUEST_BYTES
            if len(body) > request_bytes:
                raise ProviderError("provider_budget_exceeded") from None
            estimated_tokens = estimate_tokens(request.input_text) + PROTOCOL_RESERVE
            if estimated_tokens > request.budget.max_input_tokens:
                raise ProviderError("provider_budget_exceeded") from None
            if window and estimated_tokens + request.budget.max_output_tokens > window:
                raise ProviderError("provider_context_window_exceeded") from None
            wall_remaining = (request.deadline_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
            if wall_remaining <= 0:
                raise ProviderError("provider_timeout") from None
            duration = min(float(self.config.timeout_seconds), wall_remaining)
            deadline = time.monotonic() + duration
            tr = _Transport(deadline)
            timer = threading.Timer(duration, tr.stop)
            timer.daemon = True
            timer.start()
            worker = asyncio.create_task(asyncio.to_thread(self._exchange, body, tr))
            worker.add_done_callback(self._consume_worker_result)
            try:
                result = await asyncio.wait_for(asyncio.shield(worker), timeout=duration)
            except asyncio.TimeoutError:
                tr.stop()
                raise ProviderError("provider_timeout") from None
            except asyncio.CancelledError:
                tr.stop()
                raise
            finally:
                timer.cancel()
            return self._parse_result(result, request)
        except ProviderError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProviderError("provider_invalid_request") from None

    @staticmethod
    def _consume_worker_result(task: asyncio.Task[object]) -> None:
        # A timed-out/cancelled caller no longer awaits the to_thread task. Read
        # its eventual result so a transport error cannot become an unhandled
        # task exception; the public caller already received its stable error.
        if not task.cancelled():
            task.exception()

    def _make_body(self, request: ProviderRequest) -> bytes:
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": request.input_text}],
            "n": 1,
            "stream": False,
            "store": False,
            self.config.token_parameter: request.budget.max_output_tokens,
        }
        try:
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ProviderError("provider_invalid_request") from None

    def _exchange(self, body: bytes, tr: _Transport) -> tuple[int, str, bytes]:
        _scheme, host, port, use_tls = self._parts
        parts = urlsplit(self.config.base_url)
        path = parts.path.rstrip("/") + "/chat/completions"
        conn: _DirectConnection | None = None
        try:
            conn = _DirectConnection(host, port, tr, use_tls)
            tr.remaining()
            conn.request(
                "POST",
                path or "/chat/completions",
                body=body,
                headers={
                    "Host": parts.netloc,
                    "Authorization": "Bearer " + self.config.api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            tr.remaining()
            response = conn.getresponse()
            status = response.status
            if status != 200:
                # Map status without interpreting or buffering any error body.
                return status, "", b""
            content_type = response.getheader("Content-Type", "")
            encoding = response.getheader("Content-Encoding", "identity")
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    declared_length = int(length)
                    if declared_length < 0:
                        raise ProviderError("provider_invalid_response") from None
                    if declared_length > self.config.max_response_bytes:
                        raise ProviderError("provider_response_too_large") from None
                except ValueError:
                    raise ProviderError("provider_invalid_response") from None
            if encoding.strip().lower() not in ("", "identity"):
                raise ProviderError("provider_invalid_response") from None
            if status == 200:
                media_type = content_type.split(";", 1)[0].strip().lower()
                if media_type != "application/json":
                    raise ProviderError("provider_invalid_response") from None
            chunks = bytearray()
            total = 0
            while True:
                tr.remaining()
                if conn.sock is not None:
                    conn.sock.settimeout(tr.remaining())
                chunk = response.read1(min(65_536, self.config.max_response_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > self.config.max_response_bytes:
                    raise ProviderError("provider_response_too_large") from None
                chunks.extend(chunk)
            if response.length not in (None, 0):
                raise ProviderError("provider_invalid_response") from None
            tr.remaining()
            return status, content_type, bytes(chunks)
        except ProviderError:
            raise
        except http.client.IncompleteRead:
            raise ProviderError("provider_invalid_response") from None
        except (TimeoutError, socket.timeout):
            raise ProviderError("provider_timeout") from None
        except (OSError, http.client.HTTPException, ssl.SSLError):
            if tr.stopped.is_set() or time.monotonic() >= tr.deadline:
                raise ProviderError("provider_timeout") from None
            raise ProviderError("provider_network_error") from None
        except Exception:
            if tr.stopped.is_set() or time.monotonic() >= tr.deadline:
                raise ProviderError("provider_timeout") from None
            raise ProviderError("provider_network_error") from None
        finally:
            tr.stop()
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _parse_result(self, result: tuple[int, str, bytes], request: ProviderRequest) -> ProviderResponse:
        status, _content_type, raw = result
        if status == 401 or status == 403:
            raise ProviderError("provider_authentication_failed") from None
        if status == 429:
            raise ProviderError("provider_rate_limited") from None
        if 500 <= status <= 599:
            raise ProviderError("provider_unavailable") from None
        if 300 <= status <= 399:
            raise ProviderError("provider_redirect_rejected") from None
        if status != 200:
            raise ProviderError("provider_http_error") from None
        try:
            text = raw.decode("utf-8", errors="strict")
            data = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_constant,
                parse_float=_parse_finite_float,
            )
        except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
            raise ProviderError("provider_invalid_response") from None
        if not isinstance(data, dict):
            raise ProviderError("provider_invalid_response") from None
        try:
            _check_json_depth(data)
        except ValueError:
            raise ProviderError("provider_invalid_response") from None

        usage = data.get("usage")
        if not isinstance(usage, dict):
            raise ProviderError("provider_usage_invalid") from None
        input_value = usage.get("prompt_tokens")
        output_value = usage.get("completion_tokens")
        input_tokens = input_value if type(input_value) is int and input_value >= 0 else None
        output_tokens = output_value if type(output_value) is int and output_value >= 0 else None
        if input_tokens is None or output_tokens is None:
            raise ProviderError(
                "provider_usage_invalid", input_tokens=input_tokens, output_tokens=output_tokens
            ) from None
        total = usage.get("total_tokens")
        if "total_tokens" in usage and (type(total) is not int or total != input_tokens + output_tokens):
            raise ProviderError("provider_usage_invalid", input_tokens=input_tokens, output_tokens=output_tokens) from None
        if input_tokens > request.budget.max_input_tokens or output_tokens > request.budget.max_output_tokens:
            raise ProviderError("provider_budget_exceeded", input_tokens=input_tokens, output_tokens=output_tokens) from None
        window = (self.config.context_window_tokens
                  or context_window(self.config.model, environ={}, base_url=self.config.base_url))
        if window and input_tokens + output_tokens > window:
            raise ProviderError("provider_context_window_exceeded", input_tokens=input_tokens,
                                output_tokens=output_tokens) from None

        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProviderError("provider_invalid_response", input_tokens=input_tokens, output_tokens=output_tokens) from None
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderError("provider_invalid_response", input_tokens=input_tokens, output_tokens=output_tokens) from None
        if message.get("role") != "assistant":
            raise ProviderError("provider_invalid_response", input_tokens=input_tokens, output_tokens=output_tokens) from None
        if message.get("tool_calls") or message.get("function_call"):
            raise ProviderError("provider_tool_calls_unsupported", input_tokens=input_tokens, output_tokens=output_tokens) from None
        if message.get("refusal") is not None:
            raise ProviderError("provider_refused", input_tokens=input_tokens, output_tokens=output_tokens) from None
        if choice.get("finish_reason") != "stop":
            raise ProviderError("provider_output_incomplete", input_tokens=input_tokens, output_tokens=output_tokens) from None
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("provider_invalid_response", input_tokens=input_tokens, output_tokens=output_tokens) from None
        try:
            content.encode("utf-8")
            return ProviderResponse(suggestion=content, input_tokens=input_tokens, output_tokens=output_tokens)
        except (TypeError, ValueError):
            raise ProviderError("provider_invalid_response", input_tokens=input_tokens, output_tokens=output_tokens) from None


class DeepSeekJsonProvider(ChatCompletionsProvider):
    """Fixed official Flash JSON protocol for bounded Agent/search decisions.

    Explicit non-thinking mode avoids consuming the small decision budget on
    internal reasoning. This is never applied to another host or model.
    """
    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        if self._parts[0:2] != ('https', 'api.deepseek.com') or config.model != 'deepseek-flash':
            raise ProviderError('provider_configuration_invalid')

    def _make_body(self, request: ProviderRequest) -> bytes:
        body = json.loads(super()._make_body(request))
        body['thinking'] = {'type': 'disabled'}
        body['response_format'] = {'type': 'json_object'}
        return json.dumps(body,ensure_ascii=False,separators=(',', ':'),allow_nan=False).encode()
