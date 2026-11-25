"""Bounded fresh-exec transport for an already committed native source intent.

Call only after releasing the preflight transaction. A prepared return is
internal: it still needs the result-root guard, commit and current readback.
No retries, configurable command/material path, stderr capture or publication.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

from backend.app.modules.agents import process_transport

from . import work_source as source
from . import observation_receipts as observations

_ROOT = Path(__file__).resolve().parents[5]
_COMMAND = (sys.executable, '-I', '-c',
    'import sys,runpy;sys.path.insert(0,' + repr(str(_ROOT)) + ');'
    'runpy.run_module("backend.app.workers.guanjia_work_source",run_name="__main__")')
_TIMEOUT = 40.0  # two ten-second HTTPS calls plus interpreter/authority checks
_MAX_OUTPUT = 132096
_UNKNOWN = ((0, 1), (0, 1), (0, 1))


@dataclass(frozen=True, repr=False)
class SourceProcessResult:
    code: str
    prepared: observations.PreparedObservation | None = field(default=None, repr=False)
    # authenticate / live_prices / live_stock. Unknown never means not sent.
    call_bounds: tuple = _UNKNOWN


def _bounds(value):
    if type(value) is not dict or set(value) != set(source._ACTIONS):
        raise ValueError()
    result = []
    for action in source._ACTIONS:
        pair = value[action]
        if (type(pair) is not list or len(pair) != 2 or any(type(x) is not int for x in pair)
                or pair not in ([0, 0], [0, 1], [1, 1])):
            raise ValueError()
        result.append(tuple(pair))
    return tuple(result)


def _decode(raw, status, call):
    value = source.protected._decode(raw)
    if (type(value) is not dict or type(value.get('version')) is not int or value['version'] != 1
            or value.get('disclosure_ready') is not False):
        raise ValueError()
    if status == 6 and value.get('code') in ('denied_or_failed', 'protected_source_unavailable'):
        if set(value) != {'version', 'code', 'call_bounds', 'disclosure_ready', 'result'} or value['result'] is not None:
            raise ValueError()
        bounds = _UNKNOWN if value['call_bounds'] is None else _bounds(value['call_bounds'])
        return SourceProcessResult('source_denied_or_failed', call_bounds=bounds)
    if (status != 0 or set(value) != {'version', 'code', 'request_sha256', 'call_bounds', 'disclosure_ready', 'result'}
            or value['code'] != 'prepared' or value['request_sha256'] != source._digest(call.wire())):
        raise ValueError()
    operation = next(k for k, v in observations._TOOLS.items() if v[0] == call.tool_key)
    bounds = _bounds(value['call_bounds'])
    expected = tuple((1, 1) if k in ('authenticate', operation) else (0, 0) for k in source._ACTIONS)
    result = value['result']
    if (bounds != expected or type(result) is not dict or set(result) != {'observation_id', 'observation'}
            or result['observation_id'] != observations._id(call.claim.run_id, call.step_id)
            or type(result['observation']) is not dict
            or result['observation'].get('operation') != operation
            or result['observation'].get('contract') != observations.CONTRACT
            or result['observation'].get('authority') != 'source_field_observation_only'):
        raise ValueError()
    # The fixed trusted child owns field projection; this layer binds its IPC
    # envelope. No arbitrary executable/handler can supply a trusted result.
    prepared = observations.PreparedObservation(observations._json(result))
    return SourceProcessResult('prepared', prepared, bounds)


def _exchange(process, raw, deadline):
    """Delegate to the shared bounded transport; keep this protocol's limits."""
    try:
        output, _stderr, status = process_transport.exchange(
            process, raw, deadline, max_stdout=_MAX_OUTPUT, max_stderr=0)
    except process_transport.TransportTimeout as error:
        raise TimeoutError() from error
    except process_transport.TransportError as error:
        raise ValueError() from error
    return output, status


def _cleanup(process):
    process_transport.cleanup(process)


def execute(call):
    process = None
    result = SourceProcessResult('source_process_unavailable')
    try:
        if type(call) is not source.SourceCall:
            raise ValueError()
        raw = source._json(call.wire()).encode('utf-8')
        if source.decode_call(raw) != call:
            raise ValueError()
        deadline = time.monotonic() + _TIMEOUT
        process = subprocess.Popen(_COMMAND, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, cwd=_ROOT,
            env={'PATH': os.defpath, 'LANG': 'C.UTF-8'})
        output, status = _exchange(process, raw, deadline)
        result = _decode(output, status, call)
    except BaseException:
        # Includes cancellation. No output/stderr/exception can leak source
        # values, and a transport failure never authorizes another dispatch.
        result = SourceProcessResult('source_process_unavailable')
    finally:
        if process is not None:
            try:
                _cleanup(process)
            except BaseException:
                result = SourceProcessResult('source_process_cleanup_failed')
    return result
