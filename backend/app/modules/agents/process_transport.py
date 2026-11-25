"""Bounded process exchange shared by fresh-subprocess tools.

Extracted from the guanjia work-source transport so a tool does not copy its
own blocking ``communicate()`` framework.  Nothing here imports application
DB/session/auth code; callers decide the fixed argv and environment.

Guarantees:
- stdin is drained while stdout/stderr are read through a selector, so a chatty
  child cannot fill a pipe and deadlock the parent;
- each stream is capped *while receiving*, not after ``communicate()`` returns;
- a cancellation event wakes the loop even when the child emits no stdout;
- timeout/cancellation/oversize raise before any partial output is trusted;
- :func:`cleanup` bounds both reap waits and fails closed if cleanup is unconfirmed.
"""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time


class TransportError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TransportTimeout(TransportError):
    def __init__(self):
        super().__init__('transport_timeout')


class TransportCancelled(TransportError):
    def __init__(self):
        super().__init__('transport_cancelled')


class TransportOutputTooLarge(TransportError):
    def __init__(self):
        super().__init__('transport_output_too_large')


class TransportCleanupError(TransportError):
    def __init__(self):
        super().__init__('transport_cleanup_unconfirmed')


def exchange(process, payload: bytes, deadline: float, *, max_stdout: int,
             max_stderr: int = 0, cancel_event=None, poll_seconds: float = 0.05):
    """Write ``payload`` to stdin while draining capped stdout/stderr.

    ``deadline`` is an absolute ``time.monotonic()`` value owned by the caller.
    Returns ``(stdout_bytes, stderr_bytes, returncode)``.
    """
    out = bytearray()
    err = bytearray()
    offset = 0
    stdout = process.stdout
    stderr = process.stderr
    stdin = process.stdin
    with selectors.DefaultSelector() as selector:
        streams = [(stdin, selectors.EVENT_WRITE), (stdout, selectors.EVENT_READ)]
        if stderr is not None:
            streams.append((stderr, selectors.EVENT_READ))
        for stream, events in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, events)
        while selector.get_map():
            if cancel_event is not None and cancel_event.is_set():
                raise TransportCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportTimeout()
            wait = min(remaining, poll_seconds)
            for key, _ in selector.select(wait):
                stream = key.fileobj
                if stream is stdin:
                    try:
                        offset += os.write(stream.fileno(), payload[offset:])
                    except BlockingIOError:
                        continue
                    except (BrokenPipeError, OSError) as error:
                        raise TransportError('transport_stdin_failed') from error
                    if offset == len(payload):
                        selector.unregister(stream)
                        stream.close()
                elif stream is stdout:
                    try:
                        block = os.read(stream.fileno(), 16384)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(stream)
                        continue
                    out.extend(block)
                    if len(out) > max_stdout:
                        raise TransportOutputTooLarge()
                else:
                    try:
                        block = os.read(stream.fileno(), 16384)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(stream)
                        continue
                    if max_stderr > 0:
                        # Strict receive quota: an over-limit stderr is a
                        # protocol failure, not silently truncated evidence.
                        if len(err) + len(block) > max_stderr:
                            raise TransportOutputTooLarge()
                        err.extend(block)
                    # max_stderr <= 0 keeps the legacy source path: drain and
                    # discard stderr without buffering (no protocol change).
    # EOF is not completion: a detached child may close all stdio and keep
    # sleeping, so the final wait must stay cancellation/deadline aware.
    while process.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            raise TransportCancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransportTimeout()
        time.sleep(min(remaining, poll_seconds))
    if time.monotonic() >= deadline:
        raise TransportTimeout()
    return bytes(out), bytes(err), process.returncode


_CLEANUP_WAIT_SECONDS = 5


def _kill_owned(process, *, whole_session):
    try:
        if whole_session:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass  # Nothing remains to signal. Still reap the direct child.
    except OSError:
        return False
    return True


def _cleanup_owned(process, *, whole_session):
    incomplete = False
    interrupted_close = None
    try:
        incomplete = not _kill_owned(process, whole_session=whole_session)
        try:
            status = process.wait(timeout=_CLEANUP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            if not whole_session:
                raise
            incomplete |= not _kill_owned(process, whole_session=False)
            status = process.wait(timeout=_CLEANUP_WAIT_SECONDS)
        incomplete |= type(status) is not int
    except Exception:
        incomplete = True
    finally:
        # A failed signal, wait or close must not prevent visiting the other
        # descriptors. Never return a result if ownership remains unresolved.
        for name in ('stdin', 'stdout', 'stderr'):
            try:
                stream = getattr(process, name, None)
                if stream is not None:
                    stream.close()
            except Exception:
                incomplete = True
            except BaseException as error:
                if interrupted_close is None:
                    interrupted_close = error
        if interrupted_close is not None:
            raise interrupted_close
    if incomplete:
        raise TransportCleanupError()


def cleanup(process) -> None:
    """Signal the owned session and reap its child; unconfirmed cleanup raises."""
    _cleanup_owned(process, whole_session=True)


def _cleanup_nested(process):
    # This child shares the outer worker's group. Never signal that group.
    _cleanup_owned(process, whole_session=False)


def exchange_committed(request, *, command, cwd, nested, nested_modes, encode,
                       decode_request, decode_reply, digest, timeout, spawn,
                       communicate, cleanup_outer, deadline=None, check_deadline=False):
    """Run one fixed adapter command and bind its committed reply to the request.

    Adapters retain their own schemas, timeouts and nested operation allowlists.
    Any I/O/cleanup error is unknown (None), never a replay or an approval.
    """
    process = result = None
    try:
        if nested and request.get('mode') not in nested_modes:
            raise ValueError()
        raw = encode(request).encode()
        if decode_request(raw) != request:
            raise ValueError()
        seconds = timeout(request) if callable(timeout) else timeout
        ceiling = time.monotonic() + seconds
        deadline = ceiling if deadline is None else min(deadline, ceiling)
        if check_deadline and time.monotonic() >= deadline:
            raise ValueError()
        process = spawn(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, start_new_session=not nested,
                        close_fds=True, cwd=cwd, env={'PATH': os.defpath, 'LANG': 'C.UTF-8'})
        output, status = communicate(process, raw, deadline)
        value = decode_reply(output)
        if (status != 0 or type(value) is not dict or set(value) != {'version', 'code', 'request_sha256', 'result'}
                or type(value['version']) is not int or value['version'] != 1 or value['code'] != 'committed'
                or value['request_sha256'] != digest(request)):
            raise ValueError()
        result = value['result']
    except BaseException:
        pass
    finally:
        if process is not None:
            try:
                (_cleanup_nested if nested else cleanup_outer)(process)
            except BaseException:
                result = None
    return result
