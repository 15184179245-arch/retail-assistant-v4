"""Private fixed HTTPS exec transport. NOT authorization, login or a tool.

Caller durably consumes an operation/page before this primitive. No retry or
redirect. Deadline covers child startup, DNS, TLS, write and entire read.
Secrets use bounded stdin only; no argv/env/log/exception copies.
"""
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
import base64
import http.client
import json
import os
from pathlib import Path
import selectors
import ssl
import subprocess
import sys
import time

from . import research_read_contract as contract

_ROOT = Path(__file__).resolve().parents[5]
_COMMAND = (sys.executable, "-I", "-B", "-c",
    "import sys;sys.path.insert(0," + repr(str(_ROOT)) + ");"
    "from backend.app.modules.integrations.guanjia.research_read_transport import main;sys.exit(main())")
_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}
_MAX_OUTPUT = 100000


@dataclass(frozen=True, repr=False)
class Credentials:
    token: str = field(repr=False)
    tenant_id: str
    tenancy: str
    expires_at: datetime

    def validate(self, approved):
        if (type(self.token) is not str or not 8 <= len(self.token) <= 8192
                or any(ord(ch) < 33 or ord(ch) > 126 for ch in self.token)
                or type(self.expires_at) is not datetime or self.expires_at.tzinfo is None
                or self.expires_at <= contract.utcnow()
                or self.tenant_id != approved["tenant_id"] or self.tenancy != approved["tenancy"]):
            contract.deny()
        contract.identifier(self.tenant_id)
        contract.identifier(self.tenancy)


@dataclass(frozen=True, repr=False)
class RawReceipt:
    raw: bytes = field(repr=False)
    sha256: str
    call_interval: tuple[int, int] = (1, 1)


class TransportUnknown(Exception):
    """A consumed page is never replayed, even when zero entry is observed."""
    def __init__(self, code="transport_unknown", call_interval=(0, 1)):
        self.code, self.call_interval = code, call_interval
        super().__init__(code)


def _reap(process):
    # Child inherits outer worker's group so outer cancellation covers both.
    # Never killpg here: that would kill our own parent worker.
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait(timeout=2)
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            stream.close()


def _exchange(payload, deadline):
    process = None
    selector = selectors.DefaultSelector()
    output = bytearray()
    try:
        if time.monotonic() >= deadline:
            raise TransportUnknown()
        process = subprocess.Popen(_COMMAND, cwd=str(_ROOT), env=dict(_ENV),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=False, bufsize=0)
        for stream in (process.stdin, process.stdout):
            os.set_blocking(stream.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "in")
        selector.register(process.stdout, selectors.EVENT_READ, "out")
        sent = 0
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportUnknown("transport_deadline")
            for key, _ in selector.select(remaining):
                if key.data == "in":
                    n = os.write(key.fd, payload[sent:sent+4096])
                    if n <= 0:
                        raise TransportUnknown()
                    sent += n
                    if sent == len(payload):
                        selector.unregister(key.fileobj)
                        process.stdin.close()
                else:
                    chunk = os.read(key.fd, 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > _MAX_OUTPUT:
                            raise TransportUnknown()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise TransportUnknown()
        return bytes(output)
    except BaseException:
        raise TransportUnknown() from None
    finally:
        selector.close()
        if process is not None:
            _reap(process)


def send(approved, credentials, body, *, deadline):
    """Internal only. No URL/header/user credentials arguments or automatic retry."""
    try:
        contract.validate_manifest(approved)
        if type(credentials) is not Credentials:
            contract.deny()
        credentials.validate(approved)
        operation = approved["operation"]
        scope_key = contract.SPECS[operation]["scope_request"]
        business = dict(body)
        scopes = business.pop(scope_key)
        expected = contract.request_body(operation, business, scopes,
                                          business["skipCount"], business["maxResultCount"])
        if body != expected or scopes != approved["scope_ids"]:
            contract.deny()
        remaining = min(approved["timeout_seconds"], deadline - time.monotonic())
        if remaining <= 0:
            raise TransportUnknown("transport_deadline", (0, 0))
        packet = {"operation": operation, "body": body, "token": credentials.token,
                  "tenant_id": credentials.tenant_id, "tenancy": credentials.tenancy,
                  "timeout_seconds": remaining, "max_bytes": approved["max_response_bytes"]}
        payload = json.dumps(packet, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
        if len(payload) > 24000:
            contract.deny()
        raw_output = _exchange(payload, min(deadline, time.monotonic() + remaining))
        reply = json.loads(raw_output)
        if type(reply) is not dict or set(reply) != {"status", "raw"}:
            raise TransportUnknown()
        if reply["status"] != "returned" or type(reply["raw"]) is not str:
            raise TransportUnknown()
        raw = base64.b64decode(reply["raw"], validate=True)
        if not 0 < len(raw) <= approved["max_response_bytes"]:
            raise TransportUnknown()
        return RawReceipt(raw, sha256(raw).hexdigest())
    except contract.ReadDenied:
        raise
    except Exception:
        raise TransportUnknown() from None


def _request(packet):
    """Runs ONLY inside disposable exec child. Never prints upstream exceptions."""
    connection = None
    try:
        if type(packet) is not dict or set(packet) != {"operation", "body", "token",
                "tenant_id", "tenancy", "timeout_seconds", "max_bytes"}:
            raise ValueError()
        operation, body = packet["operation"], packet["body"]
        if operation not in contract.SPECS or type(body) is not dict:
            raise ValueError()
        maximum = contract.integer(packet["max_bytes"], 1, 65536)
        seconds = packet["timeout_seconds"]
        if type(seconds) not in (int, float) or not 0 < seconds <= 30:
            raise ValueError()
        token = packet["token"]
        if type(token) is not str or not 8 <= len(token) <= 8192 or any(not 33 <= ord(ch) <= 126 for ch in token):
            raise ValueError()
        contract.identifier(packet["tenant_id"]); contract.identifier(packet["tenancy"])
        scope_key = contract.SPECS[operation]["scope_request"]
        params = dict(body); scopes = params.pop(scope_key)
        if contract.request_body(operation, params, scopes, params["skipCount"], params["maxResultCount"]) != body:
            raise ValueError()
        deadline = time.monotonic() + seconds
        context = ssl.create_default_context()
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError()
        # http.client ignores environment proxies and never follows redirects.
        connection = http.client.HTTPSConnection(contract.HOST, port=443, context=context,
                                                  timeout=max(0.001, deadline-time.monotonic()))
        connection.connect()
        if time.monotonic() >= deadline:
            raise ValueError()
        connection.request("POST", contract.SPECS[operation]["path"],
            body=json.dumps(body, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode(),
            headers={"Authorization": "Bearer " + token, "Abp.TenantId": packet["tenant_id"],
                     "Content-Type": "application/json", "Accept": "application/json",
                     "Accept-Encoding": "identity", "Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError()
        headers = response.getheaders()
        def values(name):
            return [v for k, v in headers if k.lower() == name]
        lengths = values("content-length")
        types = values("content-type")
        encodings = values("content-encoding")
        if (len(lengths) != 1 or values("transfer-encoding") or len(types) != 1
                or types[0].split(";", 1)[0].strip().lower() != "application/json"
                or (encodings and encodings != ["identity"])):
            raise ValueError()
        length = lengths[0]
        if type(length) is not str or not length.isascii() or not length.isdecimal() or len(length) > 7:
            raise ValueError()
        length = int(length)
        if not 0 < length <= maximum:
            raise ValueError()
        raw = response.read(maximum + 1)
        if type(raw) is not bytes or len(raw) != length or time.monotonic() >= deadline:
            raise ValueError()
        return {"status": "returned", "raw": base64.b64encode(raw).decode("ascii")}
    except BaseException:
        return {"status": "unknown", "raw": ""}
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass


def main():
    try:
        raw = sys.stdin.buffer.read(24001)
        if not 0 < len(raw) <= 24000:
            raise ValueError()
        packet = json.loads(raw)
        reply = _request(packet)
        sys.stdout.buffer.write(json.dumps(reply, separators=(",", ":")).encode())
        sys.stdout.buffer.flush()
        return 0
    except BaseException:
        return 1
