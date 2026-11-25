"""Unwired Host-internal raw HTTPS primitive for ERP-009 L2c1.

This is NOT an authorization boundary or a product/CLI entry point. A future
trusted Host must durably reserve the L2a operation, write send_intent and
network_entered, and recheck permission in host_gate BEFORE this primitive runs.
A callback alone does not establish that those requirements were met. No caller
currently wires this module to L2a, a loader, Pi, Web, or real credentials.

A disposable worker covers DNS/TLS/connect/write/read under one deadline. Zero
means no HTTP business/auth request entered; TLS connection traffic can occur.
Once request() is entered, any incomplete outcome is conservatively [0,1].
"""
from dataclasses import dataclass, field
import hashlib
import http.client
import json
import math
import multiprocessing
from multiprocessing.process import BaseProcess
import os
import re
import signal
import ssl
import threading
import time

__all__ = ()
MAX_RESPONSE_BYTES = 1024 * 1024
_HOST = 'erp.example.invalid'
_PATHS = {
    'authenticate': '/api/TokenAuth/Authenticate',
    'live_stock': '/api/services/app/V1/Erp/Stock/GetByProductIds',
    'live_prices': '/api/services/app/V1/Erp/Selector/Price/GetAll',
}
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z', re.ASCII)


class OnceTransportError(Exception):
    """Fixed, non-sensitive error and conservative request-count interval."""

    def __init__(self, code, call_interval=(0, 0)):
        self.code = code
        self.call_interval = call_interval
        super().__init__(code)


@dataclass(frozen=True)
class _RawReceipt:
    raw: bytes = field(repr=False)
    sha256: str
    call_interval: tuple[int, int] = (1, 1)
    http_status: int = 200
    json_body: bool = True


def _text(value):
    return (type(value) is str and 0 < len(value) <= 4096
            and all(ord(c) >= 32 and ord(c) != 127 for c in value))


def _worker(path, body, headers, deadline, child_pid, released, entered, state, size, buffer,
            status_out=None):
    # Never serialize an upstream exception, response body, URL or headers into
    # diagnostics. http.client uses no environment proxy and follows no redirect.
    child_pid.value = os.getpid()
    connection = None
    try:
        # An interrupted start may fork without returning a usable handle. No
        # connect/request is permitted until the parent explicitly releases it.
        while not released.value:
            if time.monotonic() >= deadline:
                return
            time.sleep(min(0.005, max(0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            return
        tls_context = ssl.create_default_context()
        if time.monotonic() >= deadline:
            return
        connection = http.client.HTTPSConnection(
            _HOST, timeout=max(0.001, deadline - time.monotonic()), context=tls_context)
        if time.monotonic() >= deadline:
            return
        connection.connect()
        if time.monotonic() >= deadline:
            return
        entered.value = 1  # immediately BEFORE any HTTP request bytes may leave
        connection.request('POST', path, body=body, headers=headers)
        response = connection.getresponse()
        if (type(response.status) is not int or not 100 <= response.status <= 599 or
                status_out is None and response.status != 200):
            return
        encoding = response.getheader('Content-Encoding', 'identity')
        if status_out is None and (type(encoding) is not str or encoding.lower().strip() != 'identity'):
            return
        content_type = response.getheader('Content-Type', '')
        if status_out is None and (type(content_type) is not str
                or content_type.split(';', 1)[0].strip().lower() != 'application/json'):
            return
        # Close-delimited bodies cannot prove that EOF was not truncation.
        # Only one explicit Content-Length is accepted in this slice. All
        # Transfer-Encoding (including valid chunked) awaits a separately frozen
        # implementation; duplicates/conflicts are rejected before body parsing.
        response_headers = response.getheaders()
        lengths = [value for name, value in response_headers
                   if name.lower() == 'content-length']
        if (len(lengths) != 1 or any(name.lower() == 'transfer-encoding'
                                    for name, _ in response_headers)):
            return
        length_header = lengths[0]
        if type(length_header) is not str:
            return
        length_header = length_header.strip(' \t')
        if (not length_header.isascii() or not length_header.isdecimal()
                or len(length_header) > 10):
            return
        length = int(length_header)
        if not 0 <= length <= MAX_RESPONSE_BYTES:
            return
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if (type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES
                or len(raw) != length):
            return
        buffer[:len(raw)] = raw
        size.value = len(raw)
        if status_out is not None:
            status_out[0] = response.status
            status_out[1] = int(type(encoding) is str and encoding.lower().strip() == 'identity' and
                type(content_type) is str and
                content_type.split(';', 1)[0].strip().lower() == 'application/json')
        state.value = 1
    except BaseException:
        # Includes intentionally hostile fake exits; child tracebacks must not
        # expose secrets. Parent reports only fixed zero/unknown error codes.
        pass
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass


def _reap_worker(worker):
    """Return proof of termination; never leak dynamic cleanup exceptions.

    Use the actual published PID, not a start-success flag. If multiprocessing's
    kill/join path fails, fall back to OS child wait/kill. The request flag may
    only be sampled as exact zero after this function returns True.
    """
    if worker._closed:
        return True
    try:
        pid = worker.pid
        if pid is None and getattr(worker, '_once_start_attempted', False):
            # Also recover a fork whose Popen constructor never published its
            # handle. The child publishes its PID before waiting for release.
            while not worker._once_child_pid.value:
                if time.monotonic() >= worker._once_deadline:
                    return False
                time.sleep(0.001)
            pid = worker._once_child_pid.value
        if pid is not None:
            try:
                while True:
                    try:
                        if BaseProcess.is_alive(worker):
                            BaseProcess.kill(worker)
                        BaseProcess.join(worker)
                        break
                    except (KeyboardInterrupt, InterruptedError):
                        continue
            except BaseException:
                # A low-level popen failure must not leave a delayed sender.
                while True:
                    try:
                        found, status = os.waitpid(pid, os.WNOHANG)
                        if found == 0:
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            found, status = os.waitpid(pid, 0)
                        if found != pid:
                            return False
                        if worker._popen is not None:
                            worker._popen.returncode = os.waitstatus_to_exitcode(status)
                        break
                    except (KeyboardInterrupt, InterruptedError):
                        continue
                    except ChildProcessError:
                        # No child with this PID remains; never signal a reused
                        # PID after it has already been reaped elsewhere.
                        break
                    except BaseException:
                        return False
        # Failure to close bookkeeping after a proven reap cannot revive the
        # worker. Suppress dynamic close errors; subsequent cleanup is harmless.
        try:
            BaseProcess.close(worker)
        except BaseException:
            pass
        return True
    except BaseException:
        return False


class _OneShotHttpsTransport:
    """Private, one-use object. Materials MUST come from a future trusted Host.

    Do not instantiate this from a model/CLI schema. No action, identity,
    headers, URL, page size or redirect destination is accepted at send time.
    The constructor validates shape, not authority or business semantics.
    Execution is restricted to a single-threaded main thread: fork from an
    application thread pool is rejected before any connection can be created.
    """

    def __init__(self, action, *, deadline_seconds=10.0, _capture_http_status=False, **materials):
        if (type(action) is not str or action not in _PATHS
                or type(_capture_http_status) is not bool
                or type(deadline_seconds) not in (int, float)
                or not math.isfinite(deadline_seconds)
                or not 0.05 <= deadline_seconds <= 30):
            raise OnceTransportError('guanjia_once_materials_invalid')
        expected = {'username', 'password', 'tenancy_name'} if action == 'authenticate' else {
            'token', 'tenant_id', 'tenancy_name', 'product_id', 'shop_id'}
        if action == 'live_stock':
            expected |= {'warehouse_id'}
        if set(materials) != expected:
            raise OnceTransportError('guanjia_once_materials_invalid')
        for key, value in materials.items():
            if key == 'tenant_id':
                valid = type(value) is int and 0 < value <= 2**63 - 1
            elif key in ('product_id', 'shop_id', 'warehouse_id'):
                valid = type(value) is str and _ID.fullmatch(value) is not None
            else:
                valid = _text(value)
                if key in ('token', 'tenancy_name'):
                    valid = valid and value.isascii()
            if not valid:
                raise OnceTransportError('guanjia_once_materials_invalid')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                   'Accept-Encoding': 'identity', 'User-Agent': 'Retail-V4-Once-Readonly',
                   'Connection': 'close'}
        if action == 'authenticate':
            body = {'tenancyName': materials['tenancy_name'],
                    'userNameOrEmailAddress': materials['username'],
                    'password': materials['password'], 'rememberClient': False}
        else:
            headers.update({'Authorization': 'Bearer ' + materials['token'],
                            'Abp.TenantId': str(materials['tenant_id']),
                            'ClientId': materials['tenancy_name'],
                            'OcelotRequestId': materials['tenancy_name']})
            body = {'productIds': [materials['product_id']], 'shopId': materials['shop_id']}
            if action == 'live_prices':
                body['billModule'] = 2
            else:
                body['whsId'] = materials['warehouse_id']
        self._path = _PATHS[action]
        self._body = json.dumps(body, separators=(',', ':'), ensure_ascii=True).encode('ascii')
        self._headers = headers
        self._deadline = float(deadline_seconds)
        self._lock = threading.Lock()
        self._consumed = False
        self._owner_pid = os.getpid()
        self._capture_http_status = _capture_http_status

    def _send_after_host_gate(self, host_gate):
        if os.getpid() != self._owner_pid:
            raise OnceTransportError('guanjia_once_consumed')
        with self._lock:
            if self._consumed:
                raise OnceTransportError('guanjia_once_consumed')
            self._consumed = True
        if (threading.current_thread() is not threading.main_thread()
                or threading.active_count() != 1):
            self._body = b''
            self._headers = {}
            raise OnceTransportError('guanjia_once_worker_context_denied')
        try:
            if not callable(host_gate):
                raise ValueError
            host_gate()
        except BaseException:
            self._body = b''
            self._headers = {}
            raise OnceTransportError('guanjia_once_host_gate_failed') from None
        # fork keeps secrets off argv/environment/pickle and permits deterministic
        # fake exit tests. No reusable worker pool can retain credentials.
        deadline = time.monotonic() + self._deadline
        try:
            context = multiprocessing.get_context('fork')
            child_pid = context.RawValue('i', 0)
            released = context.RawValue('b', 0)
            entered = context.RawValue('b', 0)
            state = context.RawValue('b', 0)
            size = context.RawValue('i', 0)
            buffer = context.RawArray('B', MAX_RESPONSE_BYTES)
            capture = getattr(self, '_capture_http_status', False)
            status_out = context.RawArray('i', 2) if capture else None
            worker_args = (
                self._path, self._body, self._headers, deadline,
                child_pid, released, entered, state, size, buffer)
            if capture:
                worker_args += (status_out,)
            worker = context.Process(target=_worker, args=worker_args, daemon=True)
        except BaseException:
            self._body = b''
            self._headers = {}
            raise OnceTransportError('guanjia_once_unsent') from None
        worker._once_child_pid = child_pid
        worker._once_deadline = deadline
        worker._once_start_attempted = False
        try:
            # The Host gate is executable code and may itself create threads.
            # Recheck immediately before fork, after gate and allocation.
            if (threading.current_thread() is not threading.main_thread()
                    or threading.active_count() != 1):
                raise OnceTransportError('guanjia_once_worker_context_denied')
            # Avoid an asynchronous SIGINT between fork and multiprocessing's
            # publication of its child handle. A start override can still raise
            # after publishing a PID; cleanup below inspects that actual handle.
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
            try:
                worker._once_start_attempted = True
                worker.start()
                # Only release after start has returned and the child PID and
                # OS process handle have both been published.
                if worker.pid is None or worker._popen is None:
                    raise RuntimeError
                if time.monotonic() < deadline:
                    released.value = 1
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            worker.join(max(0, deadline - time.monotonic()))
            if worker.is_alive() or time.monotonic() >= deadline:
                if not _reap_worker(worker):
                    raise OnceTransportError('guanjia_once_reap_unknown', (0, 1))
                raise OnceTransportError('guanjia_once_deadline',
                                         (0, 1) if entered.value else (0, 0))
            if worker.exitcode != 0 or state.value != 1:
                if not _reap_worker(worker):
                    raise OnceTransportError('guanjia_once_reap_unknown', (0, 1))
                raise OnceTransportError('guanjia_once_unknown' if entered.value
                                         else 'guanjia_once_unsent',
                                         (0, 1) if entered.value else (0, 0))
            raw = bytes(buffer[:size.value])
            return _RawReceipt(raw, hashlib.sha256(raw).hexdigest(),
                               http_status=status_out[0] if capture else 200,
                               json_body=bool(status_out[1]) if capture else True)
        except OnceTransportError:
            raise
        except BaseException:
            # A failed/interrupted start may ALREADY have forked. First kill and
            # reap by the published handle, then sample the stable request flag.
            # Sampling before cleanup races with a child entering request().
            if not _reap_worker(worker):
                raise OnceTransportError('guanjia_once_reap_unknown', (0, 1)) from None
            raise OnceTransportError('guanjia_once_unknown' if entered.value
                                     else 'guanjia_once_unsent',
                                     (0, 1) if entered.value else (0, 0)) from None
        finally:
            reaped = _reap_worker(worker)
            # Do not retain credentials or potentially sensitive business IDs in
            # this consumed object. Receipt bytes are returned only to the Host.
            self._body = b''
            self._headers = {}
            if not reaped:
                raise OnceTransportError('guanjia_once_reap_unknown', (0, 1)) from None
