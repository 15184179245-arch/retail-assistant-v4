"""Host-only private versioned day archive. No transport or credential loading.

An entered operation is durable before dispatch. Unknown operations hold the day;
this module never retries requests. Integrity hashes are not authentication.
"""
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import fcntl
from hashlib import sha256
import json
import os
import re
import stat
from uuid import UUID, uuid4

from .sales_ranking_contract import _object, _bounded_json, _decimal
from .retail_sales_detail_contract import _bill_day
from .sales_history_transport import history_body

VERSION = 'erp-sales-day-archive/1'
SOURCE = 'erp_posted_sales_detail_v1'
ABORT_VERSION = 'erp-sales-day-abort/1'
FAILURE_VERSION = 'erp-sales-page-failure/1'
ABORT_REASONS = frozenset({'source_changed', 'source_returned_failure',
                           'schema_rejected', 'authority_revoked'})
MAX_RAW = 1048576
_NOFOLLOW = os.O_NOFOLLOW
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z', re.ASCII)
_SHA = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_SECRET = re.compile(r'password|token|cookie|authorization|credential|headers|secret', re.I)


class SalesHistoryArchiveError(ValueError):
    """Finite diagnostic, never embeds paths or source values."""


def _deny():
    raise SalesHistoryArchiveError('sales_history_archive_rejected') from None


def _hash(value):
    if type(value) is not str or _SHA.fullmatch(value) is None:
        _deny()
    return value


def _day(value):
    if type(value) is not str or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value) is None:
        _deny()
    return date.fromisoformat(value)


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        _deny()
    return value


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        _deny()
    return value


def _time(value):
    if type(value) is not str or len(value) > 64:
        _deny()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _deny()
    return value


def _json(raw, *, business=False):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RAW:
        _deny()
    def integer(value):
        if len(value) > 200:
            _deny()
        return int(value)
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                       parse_float=_decimal, parse_int=integer,
                       parse_constant=lambda _: _deny())
    _bounded_json(value)
    if business:
        pending = [value]
        while pending:
            node = pending.pop()
            if type(node) is dict:
                if any(_SECRET.search(key) for key in node):
                    _deny()
                pending.extend(node.values())
            elif type(node) is list:
                pending.extend(node)
    return value


def _encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()


def _binding(value):
    fields = {'organization_id', 'store_id', 'shop_id', 'tenant_id', 'source_namespace', 'contract_sha256'}
    if type(value) is not dict or set(value) != fields or value['source_namespace'] != SOURCE:
        _deny()
    for key in ('organization_id', 'store_id', 'shop_id'):
        if type(value[key]) is not str or _ID.fullmatch(value[key]) is None:
            _deny()
    _integer(value['tenant_id'], 1, 2**63 - 1)
    _hash(value['contract_sha256'])
    return dict(value)


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink, info.st_uid, info.st_mode)


def _canonical(value):
    # Exact mathematical Decimal equality without ambient-context arithmetic.
    # JSON number 1 and 1.0 compare equal; strings remain distinct wire data.
    if type(value) in (int, Decimal):
        dec = _decimal(value)
        parts = dec.as_tuple()
        digits = list(parts.digits)
        exp = parts.exponent
        while len(digits) > 1 and digits[-1] == 0:
            digits.pop(); exp += 1
        if not any(digits):
            return ['number', 0, [0], 0]
        return ['number', parts.sign, digits, exp]
    if type(value) is dict:
        return ['object', [[key, _canonical(child)] for key, child in sorted(value.items())]]
    if type(value) is list:
        return ['array', [_canonical(child) for child in value]]
    return ['scalar', value]


def _guarded(method):
    def run(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception:
            _deny()
    return run


class SalesHistoryArchive:
    """Flat 0700 directory, 0600 files, nonblocking process writer lock.

Use as a context manager to close FDs. All mutations require ``with writer()``.
Resource overrides are trusted Host/test configuration, never model arguments.
"""

    @_guarded
    def __init__(self, path, binding, *, create=False, _space_floor=10 * 1024**3,
                 _max_bytes=2 * 1024**3):
        self.binding = _binding(binding)
        self.binding_sha = sha256(_encode(self.binding)).hexdigest()
        self._fds = []
        self._ancestors = []
        self._active = False
        self._pid = os.getpid()
        self._space_floor = _integer(_space_floor, 0, 2**63 - 1)
        self._max_bytes = _integer(_max_bytes, 1, 2**63 - 1)
        self._read_set = {}
        try:
            if type(path) is not str or not path.startswith('/'):
                _deny()
            parts = path.split('/')[1:]
            if not parts or any(not p or p in ('.', '..') or '\x00' in p for p in parts):
                _deny()
            fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
            self._fds.append(fd)
            for index, name in enumerate(parts):
                if index == len(parts) - 1 and create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=fd)
                self._fds.append(child)
                self._ancestors.append((fd, name, child))
                fd = child
            self._root = fd
            self._check_root()
            if create:
                try:
                    lock = os.open('writer.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                                   0o600, dir_fd=self._root)
                    os.fsync(lock); os.close(lock); os.fsync(self._root)
                except FileExistsError:
                    pass
            self._lock = os.open('writer.lock', os.O_RDONLY | _NOFOLLOW, dir_fd=self._root)
            self._fds.append(self._lock)
            self._validate_file('writer.lock', self._lock)
            if create:
                with self.writer():
                    if 'binding.json' not in os.listdir(self._root):
                        if set(os.listdir(self._root)) != {'writer.lock'}:
                            _deny()
                        self._write('binding.json', _encode({'version': VERSION, 'binding': self.binding}))
                    self._read_binding()
            else:
                self._read_binding()
                self._finish_reads()
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        for fd in reversed(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds = []
        self._active = False

    def _check_root(self):
        if os.getpid() != self._pid or not self._fds:
            _deny()
        for parent, name, child in self._ancestors:
            a, b = os.fstat(child), os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(a.st_mode) or _fingerprint(a) != _fingerprint(b):
                _deny()
        info = os.fstat(self._root)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            _deny()

    def _validate_file(self, name, fd):
        info = os.fstat(fd)
        named = os.stat(name, dir_fd=self._root, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or
                _fingerprint(info) != _fingerprint(named)):
            _deny()
        return _fingerprint(info)

    @contextmanager
    def writer(self):
        owned = False
        try:
            self._check_root()
            if self._active:
                _deny()
            self._validate_file('writer.lock', self._lock)
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            owned = True
            self._active = True
            self._read_set = {}
            yield self
            self._finish_reads()
        except Exception:
            _deny()
        finally:
            if owned:
                self._active = False
                fcntl.flock(self._lock, fcntl.LOCK_UN)

    def _require_writer(self):
        self._check_root()
        self._validate_file('writer.lock', self._lock)
        if not self._active:
            _deny()

    def _read(self, name):
        self._check_root()
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK, dir_fd=self._root)
        try:
            before = self._validate_file(name, fd)
            if before[2] > MAX_RAW:
                _deny()
            data = b''
            while len(data) <= MAX_RAW:
                chunk = os.read(fd, min(65536, MAX_RAW + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            if len(data) != before[2] or self._validate_file(name, fd) != before:
                _deny()
            if name in self._read_set and self._read_set[name] != before:
                _deny()
            self._read_set[name] = before
            return data
        finally:
            os.close(fd)

    def _finish_reads(self):
        self._check_root()
        self._validate_file('writer.lock', self._lock)
        for name, expected in self._read_set.items():
            if expected is None:  # A read-only lookup pinned this named pointer as absent.
                try:
                    os.stat(name, dir_fd=self._root, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                _deny()
            info = os.stat(name, dir_fd=self._root, follow_symlinks=False)
            if _fingerprint(info) != expected:
                _deny()

    def _space(self, count):
        self._require_writer()
        used = 0
        for name in os.listdir(self._root):
            fd = os.open(name, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK, dir_fd=self._root)
            try:
                used += self._validate_file(name, fd)[2]
            finally:
                os.close(fd)
        free = os.fstatvfs(self._root)
        if used + count > self._max_bytes or free.f_bavail * free.f_frsize - count < self._space_floor:
            _deny()

    def _write(self, name, raw):
        self._space(len(raw))
        if not 0 < len(raw) <= MAX_RAW:
            _deny()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                     0o600, dir_fd=self._root)
        try:
            self._validate_file(name, fd)
            remaining = memoryview(raw)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    _deny()
                remaining = remaining[written:]
            os.fsync(fd)
            self._validate_file(name, fd)
            os.fsync(self._root)
        finally:
            os.close(fd)
        # Read back and pin new entered/receipt objects through transaction end.
        if self._read(name) != raw:
            _deny()

    def _read_binding(self):
        value = _json(self._read('binding.json'))
        if type(value) is not dict or set(value) != {'version', 'binding'}:
            _deny()
        _binding(value['binding'])
        if _encode(value) != _encode({'version': VERSION, 'binding': self.binding}):
            _deny()

    def _prefix(self, acquisition):
        _uuid(acquisition)
        record = _json(self._read(acquisition + '.reserve.json'))
        if (type(record) is not dict or set(record) != {'version', 'binding_sha256', 'day', 'acquisition'} or
                record['version'] != VERSION or record['binding_sha256'] != self.binding_sha or
                record['acquisition'] != acquisition):
            _deny()
        _day(record['day'])
        return record

    @_guarded
    def reserve(self, day, acquisition):
        self._require_writer(); self._read_binding(); _day(day); _uuid(acquisition)
        # Every entered must have a returned receipt. Unsealed known attempts
        # are resumed by the same acquisition, never silently superseded.
        for name in os.listdir(self._root):
            if name.endswith('.reserve.json'):
                other = name[:-13]
                record = self._prefix(other)
                if record['day'] == day and other != acquisition:
                    if other + '.manifest.json' not in os.listdir(self._root):
                        self._verify_abort(other)
                    elif _encode(_json(self._read(other + '.manifest.json'))) != _encode(self._verify_snapshot(other)):
                        _deny()
        name = acquisition + '.reserve.json'
        if name in os.listdir(self._root):
            if self._prefix(acquisition)['day'] != day:
                _deny()
            return acquisition
        self._write(name, _encode(dict(version=VERSION, binding_sha256=self.binding_sha,
                                      day=day, acquisition=acquisition)))
        return acquisition

    def _key(self, acquisition, pass_number, skip):
        _uuid(acquisition)
        if pass_number is None:
            if skip is not None:
                _deny()
            return acquisition + '.auth'
        _integer(pass_number, 1, 2); _integer(skip, 0, 100000)
        return acquisition + '.p' + str(pass_number) + '-s' + str(skip)

    def _unsealed(self, acquisition):
        names = os.listdir(self._root)
        if acquisition + '.manifest.json' in names or acquisition + '.abort.json' in names:
            _deny()

    def _operation_state(self, acquisition, pass_number, skip):
        self._prefix(acquisition)
        key = self._key(acquisition, pass_number, skip)
        names = set(os.listdir(self._root))
        suffixes = {suffix for suffix in ('intent.json', 'entered.json', 'raw.json', 'returned.json', 'failure.json')
                    if key + '.' + suffix in names}
        if not suffixes:
            return {'state': 'absent'}
        if 'intent.json' not in suffixes:
            _deny()
        _, intent = self._intent(acquisition, pass_number, skip)
        if 'entered.json' not in suffixes:
            if suffixes != {'intent.json'}:
                _deny()
            return {'state': 'intended', 'intent': intent}
        self._entered(key, intent)
        if 'failure.json' in suffixes:
            if pass_number is None or suffixes != {'intent.json', 'entered.json', 'failure.json'}:
                _deny()
            receipt = _json(self._read(key+'.failure.json'))
            if (type(receipt) is not dict or set(receipt) != {'version', 'intent_sha256',
                    'response_sha256', 'response_bytes', 'http_status', 'observed_at'} or
                    receipt['version'] != FAILURE_VERSION or
                    receipt['intent_sha256'] != sha256(_encode(intent)).hexdigest()):
                _deny()
            _hash(receipt['response_sha256']); _integer(receipt['response_bytes'], 0, MAX_RAW)
            _integer(receipt['http_status'], 100, 599); _time(receipt['observed_at'])
            return {'state': 'returned_failure', 'intent': intent, 'receipt': receipt}
        if 'returned.json' not in suffixes:
            if pass_number is None and 'raw.json' in suffixes:
                _deny()
            if 'raw.json' in suffixes:
                _json(self._read(key + '.raw.json'), business=True)
            return {'state': 'raw_unreceipted' if 'raw.json' in suffixes else 'unknown',
                    'intent': intent}
        receipt = _json(self._read(key + '.returned.json'))
        if (type(receipt) is not dict or set(receipt) != {'response_sha256', 'response_bytes',
                'http_status', 'observed_at', 'intent_sha256'} or
                receipt['intent_sha256'] != sha256(_encode(intent)).hexdigest()):
            _deny()
        _hash(receipt['response_sha256'])
        _integer(receipt['response_bytes'], 0 if pass_number is None else 1, MAX_RAW)
        _integer(receipt['http_status'], 100, 599); _time(receipt['observed_at'])
        result = {'state': 'returned', 'intent': intent, 'receipt': receipt}
        if pass_number is None:
            if 'raw.json' in suffixes:
                _deny()
        else:
            raw = self._read(key + '.raw.json')
            if len(raw) != receipt['response_bytes'] or sha256(raw).hexdigest() != receipt['response_sha256']:
                _deny()
            _json(raw, business=True)
            result['raw'] = raw
        return result

    @_guarded
    def operation_state(self, acquisition, *, pass_number=None, skip=None):
        """Host-only recovery view. Never returns authentication raw bytes."""
        self._require_writer(); self._read_binding()
        result = self._operation_state(acquisition, pass_number, skip)
        self._finish_reads()
        return result

    def _abort_record(self, acquisition, reason):
        record = self._prefix(acquisition)
        if type(reason) is not str or reason not in ABORT_REASONS:
            _deny()
        names = os.listdir(self._root)
        if acquisition + '.manifest.json' in names:
            _deny()
        operations = []
        # Check every artifact, including orphan raw/receipts and unsent intents.
        keys = set()
        for name in names:
            if not name.startswith(acquisition + '.'):
                continue
            if name.endswith(('.intent.json', '.entered.json', '.returned.json', '.raw.json', '.failure.json')):
                keys.add(name.rsplit('.', 2)[0])
        for key in sorted(keys):
            match = re.fullmatch(re.escape(acquisition) + r'\.(auth|p([12])-s([0-9]+))', key)
            if match is None:
                _deny()
            kind, p, skip = match.groups()
            state = self._operation_state(acquisition, None if kind == 'auth' else int(p),
                                          None if kind == 'auth' else int(skip))
            if state['state'] not in ('intended', 'returned', 'returned_failure'):
                _deny()
            operations.append({'key': key, 'state': state['state'],
                'intent_sha256': sha256(_encode(state['intent'])).hexdigest(),
                'receipt_sha256': (sha256(_encode(state['receipt'])).hexdigest()
                                   if state['state'] in ('returned', 'returned_failure') else None)})
        return dict(version=ABORT_VERSION, binding_sha256=self.binding_sha,
                    acquisition=acquisition, day=record['day'], reason=reason, operations=operations)

    def _verify_abort(self, acquisition):
        value = _json(self._read(acquisition + '.abort.json'))
        if type(value) is not dict or 'reason' not in value:
            _deny()
        if _encode(value) != _encode(self._abort_record(acquisition, value['reason'])):
            _deny()
        return value

    @_guarded
    def abort_known(self, acquisition, *, reason):
        """Close only fully known attempts; never resolve an unknown dispatch."""
        self._require_writer(); self._read_binding()
        value = self._abort_record(acquisition, reason)
        name = acquisition + '.abort.json'
        if name in os.listdir(self._root):
            existing = self._verify_abort(acquisition)
            if _encode(existing) != _encode(value):
                _deny()
            return existing
        self._finish_reads()
        self._write(name, _encode(value))
        return self._verify_abort(acquisition)

    def _descriptor(self, record, skip, take):
        day = _day(record['day'])
        return {'method': 'POST', 'action': 'erp_detail', 'body': history_body(
            operation='erp_detail', store_id=self.binding['shop_id'], start=day, end=day,
            now=datetime.now(timezone.utc), skip=skip, take=take)}

    def _known_outcomes(self, target_acquisition, *, snapshot=False):
        """A filename alone cannot turn an unknown dispatch into a return."""
        target_day = self._prefix(target_acquisition)['day']
        for name in os.listdir(self._root):
            if not name.endswith('.entered.json'):
                continue
            key = name[:-13]
            match = re.fullmatch(r'([0-9a-f-]{36})\.(auth|p([12])-s([0-9]+))', key)
            if match is None:
                _deny()
            acquisition, kind, pass_number, skip = match.groups()
            if snapshot:
                if acquisition != target_acquisition:
                    continue
            elif kind != 'auth' and self._prefix(acquisition)['day'] != target_day:
                continue
            _, intent = self._intent(acquisition, None if kind == 'auth' else int(pass_number),
                                     None if kind == 'auth' else int(skip))
            self._entered(key, intent)
            if key+'.failure.json' in os.listdir(self._root):
                outcome = self._operation_state(acquisition, None if kind == 'auth' else int(pass_number),
                                                None if kind == 'auth' else int(skip))
                if outcome['state'] != 'returned_failure':
                    _deny()
                continue
            receipt = _json(self._read(key + '.returned.json'))
            if (type(receipt) is not dict or set(receipt) != {'response_sha256', 'response_bytes',
                    'http_status', 'observed_at', 'intent_sha256'} or
                    receipt['intent_sha256'] != sha256(_encode(intent)).hexdigest()):
                _deny()
            _hash(receipt['response_sha256'])
            _integer(receipt['response_bytes'], 0 if kind == 'auth' else 1, MAX_RAW)
            _integer(receipt['http_status'], 100, 599)
            _time(receipt['observed_at'])
            if kind != 'auth':
                raw = self._read(key + '.raw.json')
                if len(raw) != receipt['response_bytes'] or sha256(raw).hexdigest() != receipt['response_sha256']:
                    _deny()

    @_guarded
    def intent(self, acquisition, *, pass_number=None, skip=None, take=None):
        self._require_writer(); self._read_binding()
        record = self._prefix(acquisition)
        self._unsealed(acquisition)
        key = self._key(acquisition, pass_number, skip)
        if pass_number is None:
            if take is not None:
                _deny()
            descriptor = {'action': 'authenticate'}
        else:
            _integer(take, 1, 100)
            descriptor = self._descriptor(record, skip, take)
        value = dict(version=VERSION, binding_sha256=self.binding_sha,
                     acquisition=acquisition, day=record['day'], descriptor=descriptor)
        self._write(key + '.intent.json', _encode(value))
        return key

    def _intent(self, acquisition, pass_number, skip):
        record = self._prefix(acquisition)
        key = self._key(acquisition, pass_number, skip)
        value = _json(self._read(key + '.intent.json'))
        expected = dict(version=VERSION, binding_sha256=self.binding_sha,
                        acquisition=acquisition, day=record['day'], descriptor={'action': 'authenticate'})
        if pass_number is not None:
            take = value['descriptor']['body']['maxResultCount']
            expected['descriptor'] = self._descriptor(record, skip, take)
        if _encode(value) != _encode(expected):
            _deny()
        return key, value

    @_guarded
    def entered(self, acquisition, *, pass_number=None, skip=None, observed_at):
        self._require_writer(); _time(observed_at)
        self._unsealed(acquisition)
        key, intent = self._intent(acquisition, pass_number, skip)
        # No further dispatch while any prior request is unknown.
        self._known_outcomes(acquisition)
        self._write(key + '.entered.json', _encode({'intent_sha256': sha256(_encode(intent)).hexdigest(),
                                                   'observed_at': observed_at}))

    def _entered(self, key, intent):
        value = _json(self._read(key + '.entered.json'))
        if type(value) is not dict or set(value) != {'intent_sha256', 'observed_at'}:
            _deny()
        if value['intent_sha256'] != sha256(_encode(intent)).hexdigest():
            _deny()
        _time(value['observed_at'])

    @_guarded
    def returned_auth(self, acquisition, *, response_sha256, response_bytes, http_status, observed_at):
        self._require_writer(); _hash(response_sha256); _integer(response_bytes, 0, MAX_RAW)
        self._unsealed(acquisition)
        _integer(http_status, 100, 599); _time(observed_at)
        key, intent = self._intent(acquisition, None, None)
        self._entered(key, intent)
        self._write(key + '.returned.json', _encode(dict(response_sha256=response_sha256,
            response_bytes=response_bytes, http_status=http_status, observed_at=observed_at,
            intent_sha256=sha256(_encode(intent)).hexdigest())))

    @_guarded
    def returned(self, acquisition, *, pass_number, skip, raw, http_status, observed_at):
        self._require_writer(); _integer(http_status, 100, 599); _time(observed_at)
        _integer(pass_number, 1, 2); _integer(skip, 0, 100000)
        self._unsealed(acquisition)
        key, intent = self._intent(acquisition, pass_number, skip)
        self._entered(key, intent)
        if key+'.failure.json' in os.listdir(self._root):
            _deny()
        value = _json(raw, business=True)
        if (type(value) is not dict or not {'success', 'result'} <= value.keys() or
                value.keys() - {'success', 'result', 'error', 'targetUrl', 'unAuthorizedRequest', '__abp'} or
                type(value['success']) is not bool or
                'unAuthorizedRequest' in value and type(value['unAuthorizedRequest']) is not bool or
                '__abp' in value and value['__abp'] is not True):
            _deny()
        # Crash after raw durability but before receipt: same-key local recovery
        # may reuse identical saved bytes; it cannot dispatch another request.
        name = key + '.raw.json'
        if name in os.listdir(self._root):
            if self._read(name) != raw:
                _deny()
        else:
            self._write(name, raw)
        self._write(key + '.returned.json', _encode(dict(response_sha256=sha256(raw).hexdigest(),
            response_bytes=len(raw), http_status=http_status, observed_at=observed_at,
            intent_sha256=sha256(_encode(intent)).hexdigest())))

    @_guarded
    def returned_failure(self, acquisition, *, pass_number, skip, response_sha256,
                         response_bytes, http_status, observed_at):
        """Complete HTTP source failure; only a digest, never its body, is kept.

The trusted Host must have the actual complete response. This is not an API to
resolve timeouts, unreceived bodies, partial raw files or failed filesystem IO.
"""
        self._require_writer(); self._unsealed(acquisition)
        _integer(pass_number, 1, 2); _integer(skip, 0, 100000)
        _hash(response_sha256); _integer(response_bytes, 0, MAX_RAW)
        _integer(http_status, 100, 599); _time(observed_at)
        state = self._operation_state(acquisition, pass_number, skip)
        if state['state'] != 'unknown':
            _deny()
        key, intent = self._intent(acquisition, pass_number, skip)
        self._write(key+'.failure.json', _encode(dict(version=FAILURE_VERSION,
            intent_sha256=sha256(_encode(intent)).hexdigest(), response_sha256=response_sha256,
            response_bytes=response_bytes, http_status=http_status, observed_at=observed_at)))

    def _pass(self, acquisition, pass_number):
        rows, receipts, offset, total, take = [], [], 0, None, None
        while True:
            key, intent = self._intent(acquisition, pass_number, offset)
            self._entered(key, intent)
            receipt = _json(self._read(key + '.returned.json'))
            if (type(receipt) is not dict or set(receipt) != {'response_sha256', 'response_bytes',
                    'http_status', 'observed_at', 'intent_sha256'} or receipt['http_status'] != 200 or
                    receipt['intent_sha256'] != sha256(_encode(intent)).hexdigest()):
                _deny()
            _time(receipt['observed_at']); _hash(receipt['response_sha256'])
            _integer(receipt['response_bytes'], 1, MAX_RAW)
            raw = self._read(key + '.raw.json')
            if len(raw) != receipt['response_bytes'] or sha256(raw).hexdigest() != receipt['response_sha256']:
                _deny()
            value = _json(raw, business=True)
            if (type(value) is not dict or value.get('success') is not True or
                    value.get('error') is not None or value.get('unAuthorizedRequest', False) is not False):
                _deny()
            result = value['result']
            if type(result) is not dict or set(result) != {'items', 'totalCount', 'reportSummaryStat'}:
                _deny()
            if type(result['reportSummaryStat']) is not dict:
                _deny()
            count = _integer(result['totalCount'], 0, 100000)
            size = intent['descriptor']['body']['maxResultCount']
            if total is None:
                total, take = count, size
            if (count != total or size != take or type(result['items']) is not list or
                    len(result['items']) != min(take, total - offset)):
                _deny()
            for item in result['items']:
                if (type(item) is not dict or item.get('shopId') != self.binding['shop_id'] or
                        type(item.get('shopId')) is not str or type(item.get('billState')) is not int or
                        item['billState'] != 12 or _bill_day(item.get('billDate')) != _day(intent['day'])):
                    _deny()
                rows.append(item)
            receipts.append({'key': key, **receipt})
            offset += len(result['items'])
            if offset == total:
                break
        return rows, receipts, take

    def _verify_snapshot(self, acquisition):
        names = os.listdir(self._root)
        if acquisition + '.abort.json' in names or acquisition + '.auth.raw.json' in names:
            _deny()
        self._known_outcomes(acquisition, snapshot=True)
        record = self._prefix(acquisition)
        a, ar, take = self._pass(acquisition, 1)
        b, br, btake = self._pass(acquisition, 2)
        if take != btake:
            _deny()
        items = Counter(_encode(_canonical(row)) for row in a)
        if items != Counter(_encode(_canonical(row)) for row in b):
            _deny()
        total_count = len(a)
        del a, b  # The verified multiset is the only input needed for its digest.
        expected_keys = {r['key'] for r in ar + br}
        auth_receipt = None
        auth_key = acquisition + '.auth'
        if auth_key + '.intent.json' in os.listdir(self._root):
            key, intent = self._intent(acquisition, None, None)
            self._entered(key, intent)
            auth_raw = self._read(key + '.returned.json')
            auth_receipt = _json(auth_raw)
            if (type(auth_receipt) is not dict or set(auth_receipt) != {'response_sha256',
                    'response_bytes', 'http_status', 'observed_at', 'intent_sha256'} or
                    auth_receipt['intent_sha256'] != sha256(_encode(intent)).hexdigest()):
                _deny()
            _hash(auth_receipt['response_sha256'])
            _integer(auth_receipt['response_bytes'], 0, MAX_RAW)
            _integer(auth_receipt['http_status'], 100, 599)
            _time(auth_receipt['observed_at'])
        for name in os.listdir(self._root):
            if name.startswith(acquisition + '.') and name.endswith('.intent.json'):
                key = name[:-12]
                if key not in expected_keys and key != acquisition + '.auth':
                    _deny()
            if (name.startswith(acquisition + '.') and name.endswith('.entered.json') and
                    name[:-13] + '.returned.json' not in os.listdir(self._root)):
                _deny()
        return dict(version=VERSION, binding_sha256=self.binding_sha, day=record['day'],
            acquisition=acquisition, page_receipts=ar + br, total_count=total_count, take=take,
            authentication_receipt=auth_receipt,
            items_multiset_sha256=sha256(_encode(sorted((k.decode(), v) for k, v in items.items()))).hexdigest(),
            structural_complete=True, business_semantics_verified=False,
            stable_source_line_identity_verified=False, atomic_snapshot_verified=False)

    def _latest(self, day):
        name = day + '.latest.json'
        if name not in os.listdir(self._root):
            return None
        value = _json(self._read(name))
        if type(value) is not dict or set(value) != {'version', 'binding_sha256', 'day', 'acquisition', 'manifest_sha256'}:
            _deny()
        if value['version'] != VERSION or value['binding_sha256'] != self.binding_sha or value['day'] != day:
            _deny()
        _uuid(value['acquisition']); _hash(value['manifest_sha256'])
        return value

    @_guarded
    def seal(self, acquisition, *, expected_latest):
        self._require_writer(); self._read_binding()
        if acquisition + '.abort.json' in os.listdir(self._root):
            _deny()
        if expected_latest is not None:
            _uuid(expected_latest)
        manifest = self._verify_snapshot(acquisition)
        day = manifest['day']
        current = self._latest(day)
        if current and current['acquisition'] == acquisition:
            return self._read_latest(day)
        if (None if current is None else current['acquisition']) != expected_latest:
            _deny()
        manifest_name = acquisition + '.manifest.json'
        data = _encode(manifest)
        if manifest_name in os.listdir(self._root):
            # An existing sealed observation is never a replacement for a
            # different published generation, even with a caller-supplied CAS.
            # A first-publication crash with no latest can still be recovered.
            if current is not None:
                _deny()
            if self._read(manifest_name) != data:
                _deny()
        else:
            self._write(manifest_name, data)
        if self._read(manifest_name) != data:
            _deny()
        pointer = _encode(dict(version=VERSION, binding_sha256=self.binding_sha, day=day,
                              acquisition=acquisition, manifest_sha256=sha256(data).hexdigest()))
        self._finish_reads()
        temp = 'pointer-' + str(uuid4()) + '.tmp'
        self._write(temp, pointer)
        # Recheck the CAS and all read objects immediately before publication.
        if self._latest(day) != current:
            _deny()
        self._finish_reads()
        os.replace(temp, day + '.latest.json', src_dir_fd=self._root, dst_dir_fd=self._root)
        os.fsync(self._root)
        self._read_set.pop(temp, None)
        self._read_set.pop(day + '.latest.json', None)
        return self._read_latest(day)

    def _read_latest(self, day):
        self._read_binding()
        pointer = self._latest(day)
        if pointer is None:
            if not self._active:
                name = day + '.latest.json'
                if self._read_set.get(name) is not None:
                    _deny()
                self._read_set[name] = None
            return None
        data = self._read(pointer['acquisition'] + '.manifest.json')
        if sha256(data).hexdigest() != pointer['manifest_sha256']:
            _deny()
        expected = self._verify_snapshot(pointer['acquisition'])
        if _encode(_json(data)) != _encode(expected) or expected['day'] != day:
            _deny()
        self._finish_reads()
        return expected

    @_guarded
    def read_latest(self, day):
        _day(day)
        self._check_root()
        if self._active:
            return self._read_latest(day)
        self._validate_file('writer.lock', self._lock)
        self._read_set = {}
        # Immutable objects can be read while another day is being collected.
        # A concurrent same-day pointer replacement fails closed at set-end.
        result = self._read_latest(day)
        if result is None:
            self._finish_reads()
        return result
