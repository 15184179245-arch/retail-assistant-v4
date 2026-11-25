"""Fixed Go HTTPS egress; callers MUST persist authorization/send intent first.

This transport does not authorize, settle usage, retry, or persist anything.
Production credentials must use HostFileCredentialSource; its file is read only
inside the deadline-bound Host worker. Callable resolvers remain synthetic-only
compatibility and cannot provide production boundedness. Sources and the optional
connection factory are Host-only, never child-frame values. No environment
configuration is read. This module does not enable the real HTTPS release gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import ctypes
import codecs
import errno
import http.client
import json
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import queue
import re
import ssl
import threading
import time
from collections.abc import Callable

from backend.app.modules.agents.runtime_protocol import (
    PI_PROVIDER_BYTES, PI_PROVIDER_PATH, _pi_provider_body, _shape,
)

HOST = 'opencode.ai'
MODEL = 'deepseek-v4.1-flash'
TIMEOUT_SECONDS = 180.0
USER_AGENT = 'retail-agent-runtime/0.1.0'


def go_session_id(organization_id, store_id, run_id):
    values = (organization_id, store_id, run_id)
    if any(type(v) is not str or not 1 <= len(v) <= 200
           or not all('!' <= c <= '~' for c in v) for v in values):
        raise PiOnlineTransportError('pi_online_session_invalid')
    raw = json.dumps(['go-session-v1', *values], ensure_ascii=False,
                     separators=(',', ':'), allow_nan=False).encode()
    return 'sxpi-' + sha256(raw).hexdigest()

_REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')

_ERROR_CODES = frozenset({'rate_limit_exceeded', 'rate_limit_error', 'too_many_requests',
    'too_many_concurrent_requests', 'quota_exceeded', 'insufficient_quota',
    'overloaded_error', 'invalid_api_key', 'authentication_error', 'permission_denied',
    'context_length_exceeded', 'invalid_request_error', 'server_error'})
_ERROR_TERMS = ('session', 'concurrent', 'rate limit', 'quota', 'credit', 'billing',
                '5 hour', 'weekly', 'monthly', 'temporarily', 'retry', 'too many requests')


def _safe_error_observation(response, secret):
    """Finite metadata only. Returned body is a labelled projection, not raw HTTP.

    Arbitrary provider text is never returned or stored. This does not establish
    usage, the cause of a rejection, permission to retry, or a recovery deadline.
    """
    observation = {}
    retry = response.getheader('retry-after')
    if (type(retry) is str and 0 < len(retry) <= 5 and retry.isascii()
            and retry.isdecimal() and int(retry) <= 86400):
        observation['retry_after_seconds'] = int(retry)
    try:
        raw = response.read(8193)
        if type(raw) is not bytes or len(raw) > 8192 or secret.encode() in raw:
            raise ValueError
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError
                result[key] = value
            return result
        def invalid_constant(_):
            raise ValueError
        payload = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
        error = payload.get('error') if type(payload) is dict else None
        if type(error) is dict:
            for field in ('code', 'type'):
                value = error.get(field)
                if type(value) is str and value in _ERROR_CODES:
                    observation['provider_error_' + field] = value
            message = error.get('message')
            if type(message) is str:
                terms = [term for term in _ERROR_TERMS if term in message.lower()]
                if terms: observation['message_terms'] = terms
    except Exception:
        pass  # The known HTTP status survives unreadable or untrusted diagnostics.
    if not observation:
        return ''
    return json.dumps({'capture': 'retail-safe-http-error-v1', **observation},
                      sort_keys=True, separators=(',', ':'))


# Only these Host-authored labels may cross IPC or become persisted failure codes.
_OPERATION_PHASES = frozenset({'credentials', 'pre_connect', 'request',
                               'response_headers', 'response_body'})
FAILURE_PHASES = _OPERATION_PHASES | frozenset({'parent_ipc', 'parent_deadline'}) | frozenset(
    'parent_deadline_' + phase for phase in _OPERATION_PHASES)


class PiOnlineTransportError(RuntimeError):
    """Stable safe code plus conservative physical-send state, never diagnostics."""

    def __init__(self, code, *, send_started=False, failure_phase=None):
        super().__init__(code)
        self.failure_phase = (failure_phase if type(failure_phase) is str
                              and failure_phase in FAILURE_PHASES else None)
        self.send_started = send_started
        self.outcome = 'unknown' if send_started else 'not_sent'


@dataclass(frozen=True, repr=False)
class HostFileCredentialSource:
    """Host-only descriptor; no file access or secret parsing in the parent.

    Production assembly must use this source, never a callable resolver. The
    absolute path names a private owner-only regular file outside this checkout.
    Symlinks in every path component, hard-linked files and extended ACLs are
    refused. Even restrictive ACLs are refused to keep the owner-only policy
    independent of platform-specific ACL inheritance and evaluation rules.
    """
    path: str

    def _read(self):
        directory = None
        descriptor = None
        try:
            path = self.path
            if (type(path) is not str or not os.path.isabs(path)
                    or path != os.path.normpath(path) or '\x00' in path):
                raise ValueError
            repository = str(Path(__file__).absolute().parents[4])
            if os.path.commonpath((repository, path)) == repository:
                raise ValueError
            parts = path.split('/')[1:]
            directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
            for component in parts[:-1]:
                following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory)
                os.close(directory)
                directory = following
            parent = os.fstat(directory)
            if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
                raise ValueError
            _refuse_extended_acl(directory)
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) & 0o177 or before.st_nlink != 1
                    or not 1 <= before.st_size <= 4096):
                raise ValueError
            _refuse_extended_acl(descriptor)
            raw = os.read(descriptor, 4097)
            after = os.fstat(descriptor)
            if (len(raw) != before.st_size or
                    (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError
            secret = raw.decode('ascii')
            _validate_secret(secret)
            return secret
        except Exception:
            raise PiOnlineTransportError('pi_online_credentials_unavailable') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                os.close(directory)


def _refuse_extended_acl(descriptor):
    """Check the opened inode, failing closed when ACL inspection is unavailable."""
    if sys.platform == 'darwin':
        library = ctypes.CDLL(None, use_errno=True)
        library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        library.acl_get_fd_np.restype = ctypes.c_void_p
        library.acl_free.argtypes = [ctypes.c_void_p]
        library.acl_free.restype = ctypes.c_int
        ctypes.set_errno(0)
        acl = library.acl_get_fd_np(descriptor, 0x100)  # ACL_TYPE_EXTENDED
        if acl:
            library.acl_free(acl)
            raise ValueError
        # Darwin reports ENOENT for an inode without an extended ACL.
        if ctypes.get_errno() != errno.ENOENT:
            raise ValueError
    elif sys.platform.startswith('linux'):
        try:
            os.getxattr(descriptor, 'system.posix_acl_access')
        except OSError as error:
            if error.errno != errno.ENODATA:
                raise
        else:
            raise ValueError
    else:
        raise ValueError


def _validate_secret(secret):
    if (type(secret) is not str or not 1 <= len(secret) <= 4096
            or not all('!' <= c <= '~' for c in secret)):
        raise ValueError


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_number(value):
    raise ValueError


def _request_bytes(body):
    try:
        if type(body) is not str:
            raise ValueError
        raw = body.encode('utf-8')
        if len(raw) > PI_PROVIDER_BYTES:
            raise ValueError
        parsed = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_number)
        if (type(parsed) is not dict or parsed.get('model') != MODEL
                or parsed.get('stream') is not True):
            raise ValueError
        _shape(parsed, finite_numbers=True)
        _pi_provider_body(parsed)
        return raw
    except Exception:
        raise PiOnlineTransportError('pi_online_request_invalid') from None


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    """Recheck after DNS/TCP/TLS and before every physical HTTP send."""

    def __init__(self, *args, deadline, **kwargs):
        super().__init__(*args, **kwargs)
        self._deadline = deadline

    def send(self, data):
        if time.monotonic() >= self._deadline:
            raise TimeoutError
        if self.sock is None:
            self.connect()
        if time.monotonic() >= self._deadline:
            raise TimeoutError
        # Disable HTTPConnection's implicit reconnect between the check and send.
        self.auto_open = 0
        return super().send(data)


def _exchange(raw, secret, deadline, factory, channel, entered, session_id, streaming=False):
    connection = None
    phase = 'pre_connect'
    try:
        channel.send(('phase', phase))
        # Preserve CPython 3.12's verified-client trust setup and fallback.
        # Skip only create_default_context's optional environment key logging.
        paths = ssl.get_default_verify_paths()
        cafile, capath = paths.openssl_cafile, paths.openssl_capath
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        if cafile or capath:
            context.load_verify_locations(cafile, capath, None)
        elif context.verify_mode != ssl.CERT_NONE:
            context.load_default_certs(ssl.Purpose.SERVER_AUTH)
        if factory is http.client.HTTPSConnection:
            connection = _DeadlineHTTPSConnection(
                HOST, port=443, timeout=max(0.001, deadline - time.monotonic()),
                context=context, deadline=deadline)
        else:
            connection = factory(
                HOST, port=443, timeout=max(0.001, deadline - time.monotonic()), context=context)

        if time.monotonic() >= deadline:
            raise PiOnlineTransportError('pi_online_timeout')
        phase = 'request'
        channel.send(('phase', phase))
        entered.value = True
        channel.send(('entered', None))
        if time.monotonic() >= deadline:
            raise PiOnlineTransportError('pi_online_timeout')
        connection.request('POST', PI_PROVIDER_PATH, body=raw, headers={
            'Authorization': 'Bearer ' + secret,
            'Content-Type': 'application/json', 'Accept': 'text/event-stream',
            'Accept-Encoding': 'identity',
            'x-opencode-session': session_id, 'User-Agent': USER_AGENT,
        })
        phase = 'response_headers'
        channel.send(('phase', phase))
        response = connection.getresponse()
        if time.monotonic() >= deadline:
            raise PiOnlineTransportError('pi_online_timeout')
        status = response.status
        if type(status) is not int or not 200 <= status <= 599:
            raise PiOnlineTransportError('pi_online_response_invalid')
        if 300 <= status < 400:
            raise PiOnlineTransportError('pi_online_redirect_refused')
        request_id = response.getheader('x-request-id')
        if (type(request_id) is not str or not _REQUEST_ID.fullmatch(request_id)
                or secret in request_id):
            request_id = None
        # Error text may reflect credentials. Only finite safe metadata crosses
        # this boundary; it is explicitly labelled as a projection, never raw.
        phase = 'response_body'
        channel.send(('phase', phase))
        if not 200 <= status < 300:
            return {'status': status, 'request_id': request_id,
                    'body': _safe_error_observation(response, secret)}
        if (response.getheader('content-type', '').split(';')[0].strip().lower()
                != 'text/event-stream'
                or response.getheader('content-encoding', 'identity').lower() != 'identity'):
            raise PiOnlineTransportError('pi_online_content_type_invalid')
        length = response.getheader('content-length')
        if length is not None and (not length.isascii() or not length.isdecimal()
                                   or int(length) > PI_PROVIDER_BYTES):
            raise PiOnlineTransportError('pi_online_response_too_large')
        if streaming:
            if not callable(getattr(response, 'read1', None)):
                raise PiOnlineTransportError('pi_online_response_invalid')
            channel.send(('stream_open', {'status': status, 'request_id': request_id}))
            full = bytearray()
            held = b''
            decoder = codecs.getincrementaldecoder('utf-8')('strict')
            protected = secret.encode()
            while True:
                chunk = response.read1(4096)
                if type(chunk) is not bytes or len(chunk) > 4096:
                    raise PiOnlineTransportError('pi_online_response_invalid')
                if time.monotonic() >= deadline:
                    raise PiOnlineTransportError('pi_online_timeout')
                if not chunk:
                    decoder.decode(b'', final=True)
                    if held: channel.send(('stream_chunk', held))
                    break
                full.extend(chunk)
                if len(full) > PI_PROVIDER_BYTES:
                    raise PiOnlineTransportError('pi_online_response_too_large')
                decoder.decode(chunk)
                held += chunk
                if protected in held:
                    raise PiOnlineTransportError('pi_online_response_rejected')
                count = max(0, len(held) - len(protected) + 1)
                if count:
                    channel.send(('stream_chunk', held[:count]))
                    held = held[count:]
            data = bytes(full)
        else:
            data = response.read(PI_PROVIDER_BYTES + 1)
        if len(data) > PI_PROVIDER_BYTES:
            raise PiOnlineTransportError('pi_online_response_too_large')
        # HTTPResponse.read(amt) can return EOF without IncompleteRead.
        # A declared body must be complete before any success is returned.
        if length is not None and len(data) != int(length):
            raise PiOnlineTransportError('pi_online_io_unknown')
        if time.monotonic() >= deadline:
            raise PiOnlineTransportError('pi_online_timeout')
        text = data.decode('utf-8', errors='strict')
        if secret in text:
            raise PiOnlineTransportError('pi_online_response_rejected')
        return {'status': status, 'request_id': request_id, 'body': text}
    except PiOnlineTransportError as error:
        raise PiOnlineTransportError(str(error), failure_phase=phase) from None
    except TimeoutError:
        raise PiOnlineTransportError('pi_online_timeout', failure_phase=phase) from None
    except Exception:
        raise PiOnlineTransportError('pi_online_io_unknown', failure_phase=phase) from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _worker(channel, factory, entered, streaming=False):
    """Spawn receives a Host file descriptor configuration or synthetic secret."""
    try:
        channel.send(('ready', None))
        raw, credential, deadline, session_id = channel.recv()
        if time.monotonic() >= deadline:
            raise PiOnlineTransportError('pi_online_timeout')
        channel.send(('phase', 'credentials'))
        secret = credential._read() if type(credential) is HostFileCredentialSource else credential
        if time.monotonic() >= deadline:
            raise PiOnlineTransportError('pi_online_timeout')
        result = _exchange(raw, secret, deadline, factory, channel, entered, session_id, streaming)
        channel.send(('result', result))
    except PiOnlineTransportError as exc:
        phase = exc.failure_phase
        if phase is None and str(exc) == 'pi_online_credentials_unavailable':
            phase = 'credentials'
        channel.send(('error', (str(exc), phase)))
    except TimeoutError:
        channel.send(('error', ('pi_online_timeout', None)))
    except BaseException:
        # Exception text, upstream body and credentials never cross back.
        try:
            channel.send(('error', ('pi_online_io_unknown', None)))
        except Exception:
            pass
    finally:
        channel.close()


class _InProcessSendState:
    """Minimal send_started cell for the in-process synthetic exchange."""
    __slots__ = ('value',)

    def __init__(self):
        self.value = False


class _InProcessChannel:
    """Phase-noise sink that forwards streaming events to the caller in-process.

    Mirrors the spawn path's on_stream contract: only stream events carry a
    payload; phase and entry receipts stay internal to the exchange.
    """
    __slots__ = ('_on_stream',)

    def __init__(self, on_stream):
        self._on_stream = on_stream

    def send(self, message):
        kind = message[0] if type(message) is tuple and message else None
        if kind in ('stream_open', 'stream_chunk') and self._on_stream is not None:
            self._on_stream(kind, message[1])


@dataclass(frozen=True, init=False, repr=False)
class PiOnlineTransport:
    _credential_resolver: Callable | HostFileCredentialSource
    _connection_factory: Callable
    _run_scope: tuple | None
    _session_id: str | None

    def __init__(self, credential_resolver: Callable[[], str] | HostFileCredentialSource, *, _connection_factory=None):
        object.__setattr__(self, '_credential_resolver', credential_resolver)
        object.__setattr__(self, '_connection_factory', _connection_factory or http.client.HTTPSConnection)
        object.__setattr__(self, '_run_scope', None)
        object.__setattr__(self, '_session_id', None)

    def bind_run(self, organization_id, store_id, run_id):
        scope = (organization_id, store_id, run_id)
        session_id = go_session_id(*scope)
        if self._run_scope is not None:
            self.require_run(*scope)
            return self
        bound = PiOnlineTransport(self._credential_resolver, _connection_factory=self._connection_factory)
        object.__setattr__(bound, '_run_scope', scope)
        object.__setattr__(bound, '_session_id', session_id)
        return bound

    def require_run(self, organization_id, store_id, run_id):
        scope = (organization_id, store_id, run_id)
        if self._run_scope != scope or self._session_id != go_session_id(*scope):
            raise PiOnlineTransportError('pi_online_session_binding')

    def send(self, body: str, *, cancel_event=None, on_stream=None) -> dict:
        """One POST maximum; terminate and reap the Host worker before returning.

        A timeout with send_started=True is unknown, never safe to retry.
        For HostFileCredentialSource the deadline includes file access, spawn,
        CA loading, DNS, connect, send and receive. Callable compatibility is
        synthetic only and cannot bound execution inside the parent resolver.
        """
        raw = _request_bytes(body)
        if self._run_scope is None:
            raise PiOnlineTransportError('pi_online_session_binding')
        self.require_run(*self._run_scope)
        if cancel_event is not None and cancel_event.is_set():
            raise PiOnlineTransportError('pi_online_cancelled', send_started=False)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        credential = self._credential_resolver
        if type(credential) is not HostFileCredentialSource:
            # Synthetic compatibility only: synchronous arbitrary callables are
            # not interruptible and provide NO production boundedness guarantee.
            try:
                credential = credential()
                _validate_secret(credential)
            except Exception:
                raise PiOnlineTransportError('pi_online_credentials_unavailable', failure_phase='credentials') from None
        if self._connection_factory is not http.client.HTTPSConnection:
            # Synthetic compatibility only: a scripted non-HTTPS factory runs
            # in-process so the caller's own objects observe every request
            # directly. Production online IO (HTTPS) keeps the isolated worker
            # below with its cancellation, reaping and send_started accounting.
            entered = _InProcessSendState()
            try:
                result = _exchange(raw, credential, deadline, self._connection_factory,
                    _InProcessChannel(on_stream), entered, self._session_id,
                    on_stream is not None)
            except PiOnlineTransportError as error:
                raise PiOnlineTransportError(str(error), send_started=entered.value,
                    failure_phase=getattr(error, 'failure_phase', None)) from None
            except Exception:
                raise PiOnlineTransportError('pi_online_io_unknown', send_started=entered.value) from None
            if time.monotonic() >= deadline:
                raise PiOnlineTransportError('pi_online_timeout', send_started=entered.value) from None
            return result
        pipes = ()
        process = None
        start_entered = False
        entered = False
        feeder = None
        reader = None
        result = None
        error = None
        failure_phase = None
        operation_phase = None
        try:
            context = multiprocessing.get_context('spawn')
            pipes = context.Pipe()
            parent, child = pipes
            send_state = context.Value('b', False, lock=False)
            process = context.Process(
                target=_worker, args=(child, self._connection_factory, send_state, True) if on_stream is not None else (child, self._connection_factory, send_state), daemon=True)
            # From the start boundary onward, keep the original send uncertainty.
            start_entered = True
            process.start()
            child.close()
            # Use a bounded-lifetime feeder: even a blocked pipe write is released
            # by terminating the only reader. Never block the deadline watcher.
            def feed():
                try:
                    parent.send((raw, credential, deadline, self._session_id))
                except Exception:
                    pass

            messages = queue.Queue(maxsize=4 if on_stream is not None else 0)
            reader_stop = threading.Event()

            def receive():
                try:
                    while True:
                        message = parent.recv()
                        while not reader_stop.is_set():
                            try:
                                messages.put(message, timeout=0.1)
                                break
                            except queue.Full: continue
                        if reader_stop.is_set(): return
                        if message[0] in ('error', 'result'):
                            return
                except Exception:
                    while not reader_stop.is_set():
                        try: messages.put(('ipc_error', None), timeout=0.1); break
                        except queue.Full: continue

            reader = threading.Thread(target=receive, daemon=True)
            reader.start()
            while True:
                remaining = deadline - time.monotonic()
                try:
                    if remaining <= 0:
                        raise queue.Empty
                    # Drain a ready terminal result before observing cancellation:
                    # a known physical bill must not be erased by a late stop.
                    try:
                        kind, value = messages.get_nowait()
                    except queue.Empty:
                        if cancel_event is not None and cancel_event.is_set():
                            error = 'pi_online_cancelled'
                            break
                        kind, value = messages.get(timeout=min(remaining, 0.1) if cancel_event is not None else remaining)
                except queue.Empty:
                    if cancel_event is not None and time.monotonic() < deadline:
                        continue
                    error = 'pi_online_timeout'
                    failure_phase = ('parent_deadline_' + operation_phase
                                     if operation_phase else None)
                    break
                if kind == 'phase':
                    if type(value) is not str or value not in _OPERATION_PHASES:
                        error = 'pi_online_io_unknown'
                        break
                    operation_phase = value
                elif kind == 'ready':
                    feeder = threading.Thread(target=feed, daemon=True)
                    feeder.start()
                elif kind == 'entered':
                    entered = True
                elif kind in ('stream_open', 'stream_chunk') and on_stream is not None:
                    try:
                        on_stream(kind, value)
                    except PiOnlineTransportError as exc:
                        # The constructor does not constrain the public code.
                        if (type(exc) is not PiOnlineTransportError or len(exc.args) != 1
                                or type(exc.args[0]) is not str or exc.args[0] != 'pi_online_timeout'):
                            raise
                        phase = getattr(exc, 'failure_phase', None)
                        failure_phase = phase if type(phase) is str and phase in FAILURE_PHASES else None
                        error = 'pi_online_timeout'
                        entered = process.pid is not None
                        break
                elif kind == 'result':
                    result = value
                    break
                elif kind == 'error':
                    # Malformed or missing phase data cannot provide diagnostics.
                    if (type(value) is tuple and len(value) == 2
                            and type(value[0]) is str and value[0] in {
                                'pi_online_timeout', 'pi_online_io_unknown',
                                'pi_online_credentials_unavailable', 'pi_online_response_invalid',
                                'pi_online_redirect_refused', 'pi_online_content_type_invalid',
                                'pi_online_response_too_large', 'pi_online_response_rejected'}
                            and type(value[1]) is str and value[1] in _OPERATION_PHASES):
                        error, failure_phase = value
                    else:
                        error = 'pi_online_io_unknown'
                    break
                elif kind == 'ipc_error':
                    error = 'pi_online_io_unknown'
                    # A lost worker is not evidence of its failure location.
                    break
                else:
                    error = 'pi_online_io_unknown'
                    break
        except BaseException as setup_error:
            if not start_entered:
                # No worker can send. Clean up only the handles already returned.
                interrupted = None
                for handle in (process, *reversed(pipes)):
                    if handle is None:
                        continue
                    try:
                        handle.close()
                    except Exception:
                        pass
                    except BaseException as cleanup_interrupt:
                        if interrupted is None:
                            interrupted = cleanup_interrupt
                if not isinstance(setup_error, Exception):
                    raise
                if interrupted is not None:
                    raise interrupted from None
                raise PiOnlineTransportError('pi_online_io_unknown', send_started=False,
                    failure_phase='parent_ipc') from None
            if not isinstance(setup_error, Exception):
                raise
            error = 'pi_online_io_unknown'
            # If a child failed before the entry receipt was observed, entry
            # cannot be ruled out. Fail conservatively after process start.
            entered = process.pid is not None
        finally:
            if start_entered:
                if reader is not None: reader_stop.set()
                child.close()
                if process.pid is not None:
                    if process.is_alive():
                        process.terminate()
                    process.join(0.1)
                    if process.is_alive():
                        process.kill()
                        process.join()
                    process.close()
                entered = entered or bool(send_state.value)
                # Reaping closes the peer endpoint, releasing blocked reads/writes.
                # Join before closing our endpoint to avoid an fd-reuse race.
                if feeder is not None:
                    feeder.join()
                if reader is not None:
                    reader.join()
                parent.close()
        if error is not None:
            raise PiOnlineTransportError(error, send_started=entered, failure_phase=failure_phase) from None
        if time.monotonic() >= deadline:
            raise PiOnlineTransportError('pi_online_timeout', send_started=entered,
                failure_phase=('parent_deadline_' + operation_phase
                               if operation_phase else None)) from None
        return result
