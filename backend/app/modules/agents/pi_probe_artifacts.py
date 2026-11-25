"""Host-only, exclusive, private preservation of diagnostic or bounded-trial Go responses.

This is evidence, never a completion/usage/permission oracle. Caller persists
send intent first and must close unknown if preservation fails. No credential,
header, request body or provider diagnostics are accepted by this interface.
Partially written files are deliberately retained and can never be overwritten.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat

from backend.app.modules.agents.pi_online_transport import _refuse_extended_acl


class ProbeArtifactError(RuntimeError):
    pass


def _reject():
    raise ProbeArtifactError('pi_probe_artifact_unavailable')


def _directory(directory):
    """Open each component without following symlinks; retain the final fd."""
    descriptor = None
    try:
        if (type(directory) is not str or not os.path.isabs(directory)
                or directory != os.path.normpath(directory) or '\x00' in directory):
            _reject()
        repository = Path(__file__).absolute().parents[4]
        if Path(directory).is_relative_to(repository):
            _reject()
        descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        for component in Path(directory).parts[1:]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            _reject()
        _refuse_extended_acl(descriptor)
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        _reject()


def validate_directory(directory):
    descriptor = _directory(directory)
    os.close(descriptor)


def read_response(*, directory, organization_id, store_id, run_id, model_step_id,
                  request_sha256, response_sha256):
    """Read one exact private archive. Never accept a caller-selected filename.

    Integrity is anchored by digests already persisted by the Host; file
    permissions alone do not establish provenance. No response is logged.
    """
    parent = descriptor = None
    try:
        values = (organization_id, store_id, run_id, model_step_id)
        if any(type(v) is not str or not 1 <= len(v) <= 200
               or not all('!' <= c <= '~' for c in v) for v in values):
            _reject()
        if any(type(v) is not str or not re.fullmatch('[0-9a-f]{64}', v)
               for v in (request_sha256, response_sha256)):
            _reject()
        name = sha256(json.dumps(list(values), separators=(',', ':'),
                                 ensure_ascii=False).encode()).hexdigest() + '.json'
        parent = _directory(directory)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600
                or not 0 < before.st_size <= 1024 * 1024):
            _reject()
        _refuse_extended_acl(descriptor)
        chunks, size = [], 0
        while block := os.read(descriptor, 65536):
            size += len(block)
            if size > 1024 * 1024:
                _reject()
            chunks.append(block)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            _reject()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    _reject()
                result[key] = value
            return result
        envelope = json.loads(b''.join(chunks), object_pairs_hook=unique,
                              parse_constant=lambda _: _reject())
        keys = {'version', 'mode', 'organization_id', 'store_id', 'run_id', 'model_step_id',
                'request_sha256', 'response_sha256', 'status', 'response_body', 'recorded_at'}
        if (type(envelope) is not dict or set(envelope) != keys
                or type(envelope['version']) is not int or envelope['version'] != 1
                or type(envelope['mode']) is not str or envelope['mode'] not in (
                    'real-probe', 'real-readonly-trial', 'real-memory-trial', 'real-assistant-trial')
                or tuple(envelope[k] for k in ('organization_id', 'store_id', 'run_id',
                                               'model_step_id')) != values
                or envelope['request_sha256'] != request_sha256
                or envelope['response_sha256'] != response_sha256
                or type(envelope['status']) is not int or envelope['status'] != 200
                or type(envelope['response_body']) is not str
                or type(envelope['recorded_at']) is not str):
            _reject()
        raw = envelope['response_body'].encode('utf-8')
        recorded = datetime.fromisoformat(envelope['recorded_at'])
        if (not 0 < len(raw) <= 128 * 1024 or sha256(raw).hexdigest() != response_sha256
                or recorded.tzinfo is None or recorded.utcoffset() is None):
            _reject()
        return envelope
    except Exception:
        raise ProbeArtifactError('pi_probe_artifact_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def record_response(*, directory, organization_id, store_id, run_id,
                    model_step_id, request_sha256, response, mode='real-probe'):
    """Losslessly archive raw UTF-8 SSE in a private JSON envelope, then fsync.

The envelope has safe binding metadata and exact response text. It is not a
public DTO; reading it must remain an explicit private forensic action.
"""
    parent = descriptor = None
    try:
        if type(mode) is not str or mode not in ('real-probe', 'real-readonly-trial', 'real-memory-trial', 'real-assistant-trial'):
            _reject()
        values = (organization_id, store_id, run_id, model_step_id)
        if any(type(value) is not str or not 1 <= len(value) <= 200
               or not all('!' <= char <= '~' for char in value) for value in values):
            _reject()
        if type(request_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', request_sha256):
            _reject()
        if (type(response) is not dict or set(response) != {'status', 'request_id', 'body'}
                or type(response['status']) is not int or not 200 <= response['status'] <= 599
                or type(response['body']) is not str):
            _reject()
        raw = response['body'].encode('utf-8')
        if len(raw) > 128 * 1024:
            _reject()
        identity = json.dumps(list(values), separators=(',', ':'), ensure_ascii=False).encode()
        name = sha256(identity).hexdigest() + '.json'
        payload = {'version': 1, 'mode': mode, 'organization_id': organization_id,
                   'store_id': store_id, 'run_id': run_id, 'model_step_id': model_step_id,
                   'request_sha256': request_sha256, 'response_sha256': sha256(raw).hexdigest(),
                   'status': response['status'], 'response_body': response['body'],
                   'recorded_at': datetime.now(timezone.utc).isoformat()}
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                             separators=(',', ':'), sort_keys=True).encode('utf-8')
        parent = _directory(directory)
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
            _reject()
        _refuse_extended_acl(descriptor)
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                _reject()
            offset += written
        os.fsync(descriptor)
        os.fsync(parent)
    except BaseException:
        _reject()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def record_runtime_report(directory, report):
    """Private launch diagnostics, no prompt, stderr, exception text or headers."""
    fields = ('stage','spawn_started','terminated','transport_ok','exit','disposition',
              'elapsed_seconds','stdout_bytes','stderr_bytes','received_frames','sent_frames')
    safe = {key: report[key] for key in fields if key in report}
    primary = report.get('primary')
    if isinstance(primary,dict):
        safe['primary'] = {key:primary[key] for key in ('stage','type','code') if key in primary}
    data = json.dumps(safe, ensure_ascii=True, allow_nan=False).encode()
    parent = _directory(directory)
    try:
        fd = os.open('runtime-report.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)
