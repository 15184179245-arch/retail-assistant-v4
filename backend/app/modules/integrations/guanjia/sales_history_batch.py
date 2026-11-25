"""Host-only durable day-by-day backfill; no password or environment loading.

Optional batch journal objects coexist with v1 immutable day archives. Their
names intentionally do not match the archive's day dispatch/receipt scanners.
No automatic auth retry, new acquisition on failure, or background scheduling.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import fcntl
import os
from uuid import uuid4
from zoneinfo import ZoneInfo

from .sales_history_archive import (
    SalesHistoryArchive, MAX_RAW, _binding, _encode, _hash, _integer, _json, _time, _uuid,
)
from .sales_history_collector import ReadCredentials, collect_day
from .sales_history_transport import _header_material

VERSION = 'erp-sales-history-batch/1'
_LEASE = 'batch-execution.lock'
_BEIJING = ZoneInfo('Asia/Shanghai')


class SalesHistoryBatchError(ValueError):
    """Finite hold; journal, rather than exception text, is recovery authority."""


def _deny():
    raise SalesHistoryBatchError('sales_history_batch_hold') from None


@dataclass(frozen=True, repr=False)
class AuthenticatedRead:
    credentials: ReadCredentials | None
    response_sha256: str
    response_bytes: int
    http_status: int
    observed_at: str

    def __repr__(self):
        return 'AuthenticatedRead(<protected>)'


class SalesHistoryBatch:
    """A single lease covers authentication and all daily work in one call.

``authenticate(gate)`` is a trusted Host callback using its fixed one-shot
transport. It must invoke gate immediately before dispatch, and return only
credentials in memory plus non-sensitive response metadata. A process restart
needs a new explicit authentication UUID, never a replay of the old one.
"""

    def __init__(self, archive, *, authority_check, _clock=lambda: datetime.now(timezone.utc),
                 _collector=collect_day):
        try:
            if (type(archive) is not SalesHistoryArchive or not callable(authority_check) or
                    not callable(_clock) or not callable(_collector)):
                _deny()
            self._archive = archive
            self._binding = _binding(archive.binding)
            self._authority = authority_check
            self._clock = _clock
            self._collector = _collector
            self._lease_fd = None
        except Exception:
            _deny()

    def _check(self):
        now = self._clock()
        if (type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None or
                _encode(_binding(self._archive.binding)) != _encode(self._binding) or
                self._authority() is not True):
            _deny()
        self._archive._read_binding()
        if self._lease_fd is not None:
            self._archive._validate_file(_LEASE, self._lease_fd)
        self._archive._finish_reads()
        return now

    def _exists(self, name):
        self._archive._check_root()
        return name in os.listdir(self._archive._root)

    def _load(self, name):
        return _json(self._archive._read(name))

    def _record(self, name, value):
        self._check()
        raw = _encode(value)
        if self._exists(name):
            if self._archive._read(name) != raw:
                _deny()
        else:
            self._archive._write(name, raw)
        self._check()

    @contextmanager
    def _lease(self):
        if self._lease_fd is not None:
            _deny()
        owned, fd = False, None
        try:
            with self._archive.writer():
                self._record(_LEASE, {'version': VERSION})
                fd = os.open(_LEASE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=self._archive._root)
                self._archive._validate_file(_LEASE, fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                owned = True
                self._lease_fd = fd
            yield
        finally:
            if owned:
                self._lease_fd = None
                fcntl.flock(fd, fcntl.LOCK_UN)
            if fd is not None:
                os.close(fd)

    def _auth_intent(self, auth_id):
        _uuid(auth_id)
        value = self._load(auth_id+'.batch-auth-intent.json')
        if type(value) is not dict or set(value) != {
                'version', 'binding_sha256', 'batch_id', 'authentication_id', 'runtime_sha256', 'action'}:
            _deny()
        _uuid(value['batch_id']); _hash(value['runtime_sha256'])
        if (value['version'] != VERSION or value['binding_sha256'] != self._archive.binding_sha or
                value['authentication_id'] != auth_id or value['action'] != 'authenticate'):
            _deny()
        return value

    def _check_auth_outcomes(self):
        names = os.listdir(self._archive._root)
        for name in names:
            if not name.endswith(('.batch-auth-dispatch.json', '.batch-auth-return.json')):
                continue
            auth_id = name.split('.', 1)[0]
            intent = self._auth_intent(auth_id)
            digest = sha256(_encode(intent)).hexdigest()
            entered = self._load(auth_id+'.batch-auth-dispatch.json')
            if (type(entered) is not dict or set(entered) != {'intent_sha256', 'observed_at'} or
                    entered['intent_sha256'] != digest):
                _deny()
            _time(entered['observed_at'])
            # Missing return or invalid metadata is an unresolved authentication.
            receipt = self._load(auth_id+'.batch-auth-return.json')
            if (type(receipt) is not dict or set(receipt) != {'intent_sha256', 'response_sha256',
                    'response_bytes', 'http_status', 'observed_at', 'credentials_present'} or
                    receipt['intent_sha256'] != digest or type(receipt['credentials_present']) is not bool):
                _deny()
            _hash(receipt['response_sha256']); _integer(receipt['response_bytes'], 0, MAX_RAW)
            _integer(receipt['http_status'], 100, 599); _time(receipt['observed_at'])

    def _authenticate(self, batch_id, auth_id, runtime_sha256, authenticate):
        intent = dict(version=VERSION, binding_sha256=self._archive.binding_sha,
            batch_id=batch_id, authentication_id=auth_id, runtime_sha256=runtime_sha256,
            action='authenticate')
        with self._archive.writer():
            self._check_auth_outcomes()
            if self._exists(auth_id+'.batch-auth-return.json'):
                # No token is on disk. This operation cannot be sent again.
                _deny()
            self._record(auth_id+'.batch-auth-intent.json', intent)
        entered = False

        def gate():
            nonlocal entered
            if entered:
                _deny()
            with self._archive.writer():
                self._check()
                self._check_auth_outcomes()
                observed_at = self._check().isoformat()
                # Durable dispatch marking is the last gate action. Do not
                # turn a later authority callback into an avoidable unknown.
                self._archive._write(auth_id+'.batch-auth-dispatch.json', _encode(dict(
                    intent_sha256=sha256(_encode(intent)).hexdigest(), observed_at=observed_at)))
                entered = True

        response = authenticate(gate)
        if not entered or type(response) is not AuthenticatedRead:
            _deny()
        _hash(response.response_sha256); _integer(response.response_bytes, 0, MAX_RAW)
        _integer(response.http_status, 100, 599); _time(response.observed_at)
        with self._archive.writer():
            # As for business pages, retain a known response before checking
            # post-dispatch revocation. No secret raw response is accepted.
            self._archive._write(auth_id+'.batch-auth-return.json', _encode(dict(
                intent_sha256=sha256(_encode(intent)).hexdigest(),
                response_sha256=response.response_sha256, response_bytes=response.response_bytes,
                http_status=response.http_status, observed_at=response.observed_at,
                credentials_present=type(response.credentials) is ReadCredentials)))
            now = self._check()
        credential = response.credentials
        if (response.http_status != 200 or type(credential) is not ReadCredentials or
                type(credential.tenant_id) is not int or credential.tenant_id != self._binding['tenant_id'] or
                credential.shop_id != self._binding['shop_id'] or
                not _header_material(credential.token, 4096) or not _header_material(credential.tenancy_name, 4096) or
                type(credential.expires_at) is not datetime or credential.expires_at.tzinfo is None or
                credential.expires_at.utcoffset() is None or now >= credential.expires_at):
            _deny()
        return credential

    def _mapping(self, batch_id, day, refresh):
        name = batch_id+'.day-'+day+'.batch-map.json'
        latest = self._archive.read_latest(day)
        if not self._exists(name):
            prior = None if latest is None else latest['acquisition']
            acquisition = prior if prior is not None and not refresh else str(uuid4())
            self._record(name, dict(version=VERSION, binding_sha256=self._archive.binding_sha,
                batch_id=batch_id, day=day, acquisition=acquisition, base_latest=prior))
        value = self._load(name)
        if type(value) is not dict or set(value) != {
                'version', 'binding_sha256', 'batch_id', 'day', 'acquisition', 'base_latest'}:
            _deny()
        _uuid(value['acquisition'])
        if value['base_latest'] is not None:
            _uuid(value['base_latest'])
        if _encode(value) != _encode(dict(version=VERSION, binding_sha256=self._archive.binding_sha,
                batch_id=batch_id, day=day, acquisition=value['acquisition'], base_latest=value['base_latest'])):
            _deny()
        return value, latest

    def _done(self, mapping):
        acquisition = mapping['acquisition']
        raw = self._archive._read(acquisition+'.manifest.json')
        manifest = self._archive._verify_snapshot(acquisition)
        if _encode(manifest) != raw or manifest['day'] != mapping['day']:
            _deny()
        return dict(version=VERSION, binding_sha256=self._archive.binding_sha,
            batch_id=mapping['batch_id'], day=mapping['day'], acquisition=acquisition,
            manifest_sha256=sha256(raw).hexdigest(), total_count=manifest['total_count'])

    def run(self, *, batch_id, authentication_id, start, end, runtime_sha256,
            authenticate, take=25, refresh=False):
        """Resume a fixed Host batch. Each invocation authenticates at most once.

``runtime_sha256`` records the current audited executable, independently of the
immutable dataset contract binding. All-complete invocations need no token.
"""
        try:
            _uuid(batch_id); _uuid(authentication_id); _hash(runtime_sha256)
            _integer(take, 1, 100)
            if type(refresh) is not bool or not callable(authenticate) or type(start) is not str or type(end) is not str:
                _deny()
            a, b = date.fromisoformat(start), date.fromisoformat(end)
            now = self._check()
            if (a.isoformat() != start or b.isoformat() != end or
                    not a <= b <= now.astimezone(_BEIJING).date() or (b-a).days >= 3660):
                _deny()
            plan = dict(version=VERSION, binding_sha256=self._archive.binding_sha,
                        batch_id=batch_id, start=start, end=end, take=take, refresh=refresh)
            completed, credential = [], None
            with self._lease():
                with self._archive.writer():
                    self._record(batch_id+'.batch-plan.json', plan)
                    self._record(authentication_id+'.batch-invocation.json', dict(
                        version=VERSION, binding_sha256=self._archive.binding_sha,
                        batch_id=batch_id, authentication_id=authentication_id,
                        runtime_sha256=runtime_sha256))
                    self._check_auth_outcomes()
                for n in range((b-a).days+1):
                    day = (a+timedelta(days=n)).isoformat()
                    checkpoint = batch_id+'.day-'+day+'.batch-done.json'
                    with self._archive.writer():
                        self._check()
                        mapping, latest = self._mapping(batch_id, day, refresh)
                        if self._exists(checkpoint):
                            done = self._done(mapping)
                            self._record(checkpoint, done)
                            completed.append(done)
                            continue
                        if latest is not None and latest['acquisition'] == mapping['acquisition']:
                            done = self._done(mapping)
                            self._record(checkpoint, done)
                            completed.append(done)
                            continue
                        current = None if latest is None else latest['acquisition']
                        if current != mapping['base_latest']:
                            _deny()
                        if (latest is None and mapping['base_latest'] is None and
                                self._exists(mapping['acquisition']+'.manifest.json')):
                            # Crash after the first manifest but before its
                            # pointer: publish the same verified observation,
                            # without a new auth or upstream page traversal.
                            self._archive.seal(mapping['acquisition'], expected_latest=None)
                            done = self._done(mapping)
                            self._record(checkpoint, done)
                            completed.append(done)
                            continue
                        self._archive.reserve(day, mapping['acquisition'])
                        self._archive._unsealed(mapping['acquisition'])
                        self._archive._known_outcomes(mapping['acquisition'])
                    if credential is None:
                        credential = self._authenticate(batch_id, authentication_id, runtime_sha256, authenticate)
                    self._check()
                    self._collector(self._archive, day=day, acquisition=mapping['acquisition'],
                        credentials=credential, authority_check=lambda: bool(self._check()),
                        take=take, refresh=refresh, expected_latest=mapping['base_latest'], _clock=self._clock)
                    with self._archive.writer():
                        self._check()
                        latest = self._archive.read_latest(day)
                        if latest is None or latest['acquisition'] != mapping['acquisition']:
                            _deny()
                        done = self._done(mapping)
                        self._record(checkpoint, done)
                        completed.append(done)
                self._check()
            return dict(status='complete', batch_id=batch_id, start=start, end=end,
                        completed_days=len(completed), total_rows=sum(d['total_count'] for d in completed),
                        checkpoints_sha256=sha256(_encode(completed)).hexdigest())
        except Exception:
            _deny()
