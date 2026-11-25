"""Private durable consumption, never an authorization or a retry facility.

The trusted Host owns the root and its ancestors. Receipts contain business
identities and must stay private. Same-UID storage destruction is outside this
primitive's threat model; deployment must preserve consumed acquisition keys.
"""
from datetime import datetime, timezone
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path
import re
import stat

from .live_call_ledger import _root_fd


class JournalDenied(Exception):
    def __init__(self):
        super().__init__('sales_ranking_journal_denied')


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def _family(binding):
    """Only fixed Host query metadata; no ranking rows or response bytes."""
    try:
        query = binding['query']
        day = query['endDate']
        tenancy_sha256 = binding['tenancy_sha256']
        result = {'store_id': binding['store_id'], 'tenant_id': binding['tenant_id'],
                  'beijing_day': day}
        if (type(result['store_id']) is not str or not result['store_id']
                or type(result['tenant_id']) is not int or result['tenant_id'] <= 0
                or type(tenancy_sha256) is not str
                or re.fullmatch('[0-9a-f]{64}', tenancy_sha256) is None
                or type(day) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) is None
                or datetime.fromisoformat(day).date().isoformat() != day):
            raise ValueError()
        return result
    except (KeyError, TypeError, ValueError):
        raise JournalDenied() from None


def _old_family(parent, name):
    """Read one prior reservation header through a protected directory FD."""
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise JournalDenied()
        record = os.open('000000.json', os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
        try:
            info = os.fstat(record)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 8192):
                raise JournalDenied()
            raw = os.read(record, 8193)
            if len(raw) != info.st_size:
                raise JournalDenied()
        finally:
            os.close(record)
        event = json.loads(raw)
        if type(event) is not dict or event.get('state') != 'reserved':
            raise JournalDenied()
        return _family(event['binding'])
    finally:
        os.close(fd)


class RankingJournal:
    """Exclusive acquisition reservation; reopening can inspect, never resume."""
    def __init__(self, root, operation_id, binding, *, capture_utc=None, enforce_family=False):
        self._root = Path(root)
        self._parent = self._fd = -1
        self._states = {}
        self._sequence = 0
        self._pid = os.getpid()
        self._failed = False
        try:
            if type(operation_id) is not str or not 1 <= len(operation_id) <= 200 or not operation_id.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in operation_id):
                raise JournalDenied()
            # An altered operation ID cannot replay the same frozen acquisition.
            self._key = sha256(encode(binding)).hexdigest()
            self._parent = _root_fd(self._root)
            if enforce_family:
                family = _family(binding)
                lock = os.open('.family.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                               0o600, dir_fd=self._parent)
                try:
                    info = os.fstat(lock)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or stat.S_IMODE(info.st_mode) & 0o077):
                        raise JournalDenied()
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    for name in os.listdir(self._parent):
                        if re.fullmatch('[0-9a-f]{64}', name):
                            if _old_family(self._parent, name) == family:
                                raise JournalDenied()
                    family_name = 'family-' + sha256(encode(family)).hexdigest()
                    marker_fd = os.open(family_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                        0o600, dir_fd=self._parent)
                    try:
                        os.fsync(marker_fd)
                    finally:
                        os.close(marker_fd)
                    os.fsync(self._parent)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    os.close(lock)
            # Consume operation identity independently of the query binding.
            # Changing dates/store cannot recycle an immutable operation ID.
            marker = 'operation-' + sha256(operation_id.encode('ascii')).hexdigest()
            marker_fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600, dir_fd=self._parent)
            try:
                os.fsync(marker_fd)
            finally:
                os.close(marker_fd)
            os.fsync(self._parent)
            os.mkdir(self._key, mode=0o700, dir_fd=self._parent)
            os.fsync(self._parent)
            self._fd = os.open(self._key, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._parent)
            self._append({'state': 'reserved', 'binding': binding, 'operation_id': operation_id,
                          'capture_utc': capture_utc})
        except BaseException:
            self.close()
            raise JournalDenied() from None

    def _verify(self):
        if self._failed or self._pid != os.getpid() or self._fd < 0:
            raise JournalDenied()
        fresh = _root_fd(self._root)
        try:
            a, b = os.fstat(fresh), os.fstat(self._parent)
            own = os.fstat(self._fd)
            named = os.stat(self._key, dir_fd=fresh, follow_symlinks=False)
            if ((a.st_dev, a.st_ino) != (b.st_dev, b.st_ino) or
                    (own.st_dev, own.st_ino) != (named.st_dev, named.st_ino) or
                    not stat.S_ISDIR(named.st_mode) or named.st_uid != os.getuid() or
                    stat.S_IMODE(named.st_mode) & 0o077):
                raise JournalDenied()
        finally:
            os.close(fresh)

    def _write(self, name, raw):
        self._verify()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
        try:
            pending = memoryview(raw)
            while pending:
                size = os.write(fd, pending)
                if size <= 0:
                    raise JournalDenied()
                pending = pending[size:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self._fd)
        self._verify()
        # Read back the exact bytes before recording successful persistence.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise JournalDenied()
            restored = bytearray()
            while len(restored) <= len(raw):
                part = os.read(fd, min(65536, len(raw) + 1 - len(restored)))
                if not part:
                    break
                restored.extend(part)
            if bytes(restored) != raw:
                raise JournalDenied()
        finally:
            os.close(fd)

    def _append(self, event):
        try:
            event = dict(event, utc=datetime.now(timezone.utc).isoformat())
            self._write(f'{self._sequence:06d}.json', encode(event))
            self._sequence += 1
        except BaseException:
            self._failed = True
            raise JournalDenied() from None

    def intent(self, request_key, descriptor):
        if request_key in self._states:
            raise JournalDenied()
        self._append({'state': 'intent', 'request': request_key, 'descriptor': descriptor})
        self._states[request_key] = 'intent'

    def entered(self, request_key):
        if self._states.get(request_key) != 'intent':
            raise JournalDenied()
        self._append({'state': 'entered', 'request': request_key})
        self._states[request_key] = 'entered'

    def returned(self, request_key, raw, *, authentication=False):
        if self._states.get(request_key) != 'entered' or type(raw) is not bytes or not 0 < len(raw) <= 2_000_000:
            raise JournalDenied()
        digest = sha256(raw).hexdigest()
        # Auth bodies contain bearer tokens: retain only digest/length, never raw.
        if not authentication:
            self._write(f'{self._sequence:06d}.raw', raw)
        self._append({'state': 'returned', 'request': request_key, 'sha256': digest,
                      'size': len(raw), 'raw_retained': not authentication})
        self._states[request_key] = 'returned'
        return digest

    def page(self, request_key, page):
        if self._states.get(request_key) != 'returned':
            raise JournalDenied()
        self._append({'state': 'validated_page', 'request': request_key,
                      'total_count': page.total_count, 'skip': page.skip,
                      'identities': [[r.product_id, r.standard_id, r.store_id] for r in page.rows]})

    def comparison(self, first, second):
        self._append({'state': 'comparison', 'first_sha256': first, 'second_sha256': second,
                      'matches': first == second, 'atomic_snapshot_verified': False})

    def unknown(self, request_key):
        if self._states.get(request_key) == 'entered':
            self._append({'state': 'unknown', 'request': request_key})
            self._states[request_key] = 'unknown'

    def close(self):
        for name in ('_fd', '_parent'):
            fd = getattr(self, name, -1)
            if fd >= 0:
                os.close(fd)
                setattr(self, name, -1)
