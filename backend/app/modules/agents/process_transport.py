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
- :func:`cleanup` kills the child's whole new session and reaps it.
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
    return bytes(out), bytes(err), process.returncode


def cleanup(process) -> None:
    """Kill our child's whole new session and reap it; never leak FDs."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
