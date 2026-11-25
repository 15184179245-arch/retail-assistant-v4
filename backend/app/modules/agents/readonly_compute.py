"""Fixed read-only projection ranking envelope for P1-C (corrected chain).

The worker runs as a fresh subprocess with a minimal environment: no database
URL/session, credentials, product service handle or network tool.  A Host
snapshot carries the database's exact token scores plus compact citation
metadata (never a body or ``lower_search_bytes``); the worker only applies the
original scope regex, stable order and 1..20 cap.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from backend.app.modules.agents import process_transport
from backend.app.modules.knowledge import ranking

PROFILE_KNOWLEDGE_RANK = ranking.SNAPSHOT_PROFILE
PROFILES = (PROFILE_KNOWLEDGE_RANK,)
ENVELOPE_KEYS = {'profile', 'input_sha256', 'snapshot_path', 'snapshot_inode',
                 'snapshot_device', 'snapshot_size', 'query_sha256', 'limit'}
OUTPUT_KEYS = {'profile', 'input_sha256', 'result', 'output_sha256'}
MAX_INPUT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_CANDIDATES = 10_000
WORKER_MODULE = 'backend.app.modules.agents.readonly_compute'
_REPO_ROOT = Path(__file__).resolve().parents[4]
_ENV_WHITELIST = ('PATH', 'LANG', 'LC_ALL', 'TZ', 'PYTHONHASHSEED', 'SYSTEMROOT')
_CANDIDATE_KEYS = set(ranking.PROJECTION_CANDIDATE_FIELDS)
_CITATION_FIELDS = set(ranking.CITATION_FIELDS)
_CITATION_SCOPE = {'store', 'product', 'category', 'global'}
_CITATION_SOURCES = {'manual', 'file', 'link', 'import'}


class WorkerError(RuntimeError):
    def __init__(self, code: str, detail: str = ''):
        super().__init__(code if not detail else f'{code}: {detail}')
        self.code = code
        self.detail = detail


def _validate_result(result, *, query: str, limit: int) -> None:
    """Reject semantically foreign worker output at the B boundary."""
    if not isinstance(result, dict) or set(result) != {'query', 'citations', 'insufficient',
                                                        'reason', 'answer'}:
        raise WorkerError('worker_result_keys')
    if result['query'] != query or type(result['insufficient']) is not bool:
        raise WorkerError('worker_result_metadata')
    if result['answer'] is not None:
        raise WorkerError('worker_result_answer')
    citations = result['citations']
    if not isinstance(citations, list) or len(citations) > max(1, min(limit, 20)):
        raise WorkerError('worker_result_citations')
    if bool(citations) == bool(result['insufficient']):
        raise WorkerError('worker_result_insufficient')
    if citations:
        if result['reason'] is not None:
            raise WorkerError('worker_result_reason')
    elif result['reason'] not in ('empty_query', 'insufficient_evidence'):
        raise WorkerError('worker_result_reason')
    seen = set()
    for citation in citations:
        if not isinstance(citation, dict) or set(citation) != _CITATION_FIELDS:
            raise WorkerError('worker_citation_fields')
        if not isinstance(citation['entry_id'], str) or not isinstance(citation['revision_id'], str):
            raise WorkerError('worker_citation_identity')
        if citation['entry_id'] in seen:
            raise WorkerError('worker_citation_duplicate')
        seen.add(citation['entry_id'])
        if citation['scope_type'] not in _CITATION_SCOPE or citation['source_type'] not in _CITATION_SOURCES:
            raise WorkerError('worker_citation_enum')
        if len(citation['excerpt'] or '') > 400 or type(citation['version']) is not int:
            raise WorkerError('worker_citation_shape')
        if citation['approved_by'] is not None and not isinstance(citation['approved_by'], str):
            raise WorkerError('worker_citation_approval')
        for field in ('approved_at', 'expires_at'):
            value = citation[field]
            if value is not None:
                try:
                    datetime.fromisoformat(value)
                except (TypeError, ValueError) as error:
                    raise WorkerError('worker_citation_time') from error


def _reject_constant(value):
    raise ValueError(f'non-finite number: {value}')


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate key: {key}')
        result[key] = value
    return result


def _loads(data: bytes):
    return json.loads(data.decode('utf-8'), object_pairs_hook=_strict_object,
                      parse_constant=_reject_constant)


def _validate_envelope(envelope) -> dict:
    if not isinstance(envelope, dict) or set(envelope) != ENVELOPE_KEYS:
        raise WorkerError('invalid_envelope')
    if envelope['profile'] not in PROFILES:
        raise WorkerError('unknown_profile')
    sha = envelope['query_sha256']
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
        raise WorkerError('invalid_query_hash')
    if type(envelope['limit']) is not int:
        raise WorkerError('invalid_limit')
    if (type(envelope['snapshot_inode']) is not int or type(envelope['snapshot_device']) is not int
            or type(envelope['snapshot_size']) is not int):
        raise WorkerError('invalid_snapshot_stat')
    return envelope


def _read_snapshot(envelope: dict) -> tuple[dict, list[dict]]:
    path = Path(envelope['snapshot_path'])
    if not path.is_absolute():
        raise WorkerError('invalid_snapshot_path')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    directory_flags = (os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
                       | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0))
    try:
        directory = os.open(path.parent, directory_flags)
    except OSError as error:
        raise WorkerError('snapshot_open_failed', str(error)) from error
    try:
        parent = os.fstat(directory)
        if not stat.S_ISDIR(parent.st_mode):
            raise WorkerError('snapshot_not_regular')
        if stat.S_IMODE(parent.st_mode) & 0o077 or parent.st_uid != os.geteuid():
            raise WorkerError('snapshot_permissions')
        try:
            descriptor = os.open(path.name, flags, dir_fd=directory)
        except OSError as error:
            raise WorkerError('snapshot_open_failed', str(error)) from error
    finally:
        os.close(directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise WorkerError('snapshot_not_regular')
        if stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.geteuid():
            raise WorkerError('snapshot_permissions')
        if (info.st_ino != envelope['snapshot_inode'] or info.st_dev != envelope['snapshot_device']
                or info.st_size != envelope['snapshot_size']):
            raise WorkerError('snapshot_stat_changed')
        if info.st_size > MAX_SNAPSHOT_BYTES:
            raise WorkerError('snapshot_too_large')
        chunks = []
        remaining = info.st_size + 1
        while remaining > 0:
            block = os.read(descriptor, min(65536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        data = b''.join(chunks)
    finally:
        os.close(descriptor)
    if len(data) != info.st_size:
        raise WorkerError('snapshot_short_read')
    if hashlib.sha256(data).hexdigest() != envelope['input_sha256']:
        raise WorkerError('snapshot_hash_mismatch')
    lines = [line for line in data.split(b'\n') if line]
    if not lines:
        raise WorkerError('snapshot_empty')
    header = _loads(lines[0])
    if (not isinstance(header, dict) or set(header) != set(ranking.HEADER_FIELDS)
            or header.get('format') != ranking.SNAPSHOT_FORMAT
            or header.get('profile') != PROFILE_KNOWLEDGE_RANK
            or header.get('count') != len(lines) - 1
            or not 0 <= header.get('count', -1) <= MAX_CANDIDATES
            or not isinstance(header.get('query'), str)):
        raise WorkerError('snapshot_header')
    if hashlib.sha256(header['query'].encode('utf-8')).hexdigest() != envelope['query_sha256']:
        raise WorkerError('snapshot_query_mismatch')
    candidates = []
    for line in lines[1:]:
        candidate = _loads(line)
        if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_KEYS:
            raise WorkerError('candidate_keys')
        for name in ('entry_id', 'revision_id', 'title', 'excerpt', 'source_type',
                     'scope_type', 'submitted_at'):
            if not isinstance(candidate[name], str):
                raise WorkerError('candidate_type')
        for name in ('source_ref', 'scope_ref', 'approved_by', 'approved_at', 'expires_at'):
            if candidate[name] is not None and not isinstance(candidate[name], str):
                raise WorkerError('candidate_type')
        for name in ('entry_version', 'revision_version'):
            if type(candidate[name]) is not int or candidate[name] < 1:
                raise WorkerError('candidate_version')
        if type(candidate['score']) is not int or candidate['score'] < 1:
            raise WorkerError('candidate_score')
        if len(candidate['excerpt']) > 400:
            raise WorkerError('candidate_excerpt')
        try:
            datetime.fromisoformat(candidate['submitted_at'])
        except ValueError as error:
            raise WorkerError('candidate_time') from error
        candidates.append(candidate)
    return header, candidates


def serve(envelope: dict) -> dict:
    envelope = _validate_envelope(envelope)
    header, candidates = _read_snapshot(envelope)
    result = ranking.rank_candidates(candidates, query=header['query'],
                                     limit=envelope['limit'])
    return {
        'profile': PROFILE_KNOWLEDGE_RANK,
        'input_sha256': envelope['input_sha256'],
        'result': result,
        'output_sha256': hashlib.sha256(ranking.result_bytes(result)).hexdigest(),
    }


def _error(code: str, detail: str = '') -> int:
    sys.stdout.write(json.dumps({'error': code, **({'detail': detail} if detail else {})},
                                ensure_ascii=False))
    return 2


def main() -> int:
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        return _error('input_too_large')
    try:
        envelope = _loads(data)
        output = serve(envelope)
    except WorkerError as error:
        return _error(error.code, error.detail)
    except Exception as error:  # noqa: BLE001 - worker must never leak a stack
        return _error('worker_failure', type(error).__name__)
    sys.stdout.write(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


def _worker_env() -> dict:
    env = {name: os.environ[name] for name in _ENV_WHITELIST if name in os.environ}
    env['PYTHONNOUSERSITE'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


def invoke(envelope: dict, *, deadline: float, cancel_event=None, command=None) -> dict:
    """Run one worker under the caller's absolute monotonic deadline."""
    payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode('utf-8')
    if len(payload) > MAX_INPUT_BYTES:
        raise WorkerError('input_too_large')
    argv = list(command) if command is not None else [sys.executable, '-m', WORKER_MODULE]
    process = subprocess.Popen(argv, cwd=str(_REPO_ROOT), env=_worker_env(),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        try:
            stdout, _stderr, status = process_transport.exchange(
                process, payload, deadline, max_stdout=MAX_OUTPUT_BYTES,
                max_stderr=MAX_STDERR_BYTES, cancel_event=cancel_event)
        except process_transport.TransportCancelled as error:
            raise WorkerError('worker_cancelled') from error
        except process_transport.TransportTimeout as error:
            raise WorkerError('worker_timeout') from error
        except process_transport.TransportOutputTooLarge as error:
            raise WorkerError('worker_output_too_large') from error
        except process_transport.TransportError as error:
            raise WorkerError('worker_transport_failed', error.code) from error
    finally:
        try:
            process_transport.cleanup(process)
        except process_transport.TransportCleanupError as error:
            # Even an otherwise valid worker reply is unconfirmed after failed cleanup.
            raise WorkerError('worker_transport_failed', error.code) from error
    if status != 0:
        detail = ''
        try:
            error = _loads(stdout or b'{}')
            detail = str(error.get('error') or '')[:120]
        except Exception:  # noqa: BLE001
            detail = ''
        raise WorkerError('worker_failed', detail)
    try:
        output = _loads(stdout)
    except ValueError as error:
        raise WorkerError('worker_output_invalid', str(error)[:120]) from error
    if not isinstance(output, dict) or set(output) != OUTPUT_KEYS:
        raise WorkerError('worker_output_keys')
    if output['profile'] != PROFILE_KNOWLEDGE_RANK:
        raise WorkerError('worker_profile_mismatch')
    if output['input_sha256'] != envelope['input_sha256']:
        raise WorkerError('worker_input_mismatch')
    result = output['result']
    if not isinstance(result, dict) or hashlib.sha256(ranking.result_bytes(result)).hexdigest() != output['output_sha256']:
        raise WorkerError('worker_output_hash')
    if hashlib.sha256(str(result.get('query', '')).encode('utf-8')).hexdigest() != envelope['query_sha256']:
        raise WorkerError('worker_query_mismatch')
    _validate_result(result, query=result.get('query'), limit=envelope['limit'])
    return output


if __name__ == '__main__':
    raise SystemExit(main())
