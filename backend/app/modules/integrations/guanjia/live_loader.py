"""Protected L2c3 assembly; no grant/session/confirmation issuing capability.

The fixed files must be supplied by a separately verified Host authority. Their
hashes establish integrity only. Existing sx1, Agent, mapping and fence helpers
still decide current authority. This increment supports isolated SQLite only;
source semantics, actual issuance/employee confirmation and production remain
HOLD. Never delete consumed grants/ledgers to retry.

Layout (outside any checkout): material.json, grant.json, authority.sqlite3,
ledger/. JSON files and DB are UID-owned 0600, root and ledger 0700. No paths,
DSN, URL, limits or environment fallback are accepted from these schemas.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import pwd
import sqlite3
import stat
import threading

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from backend.app.modules.integrations.guanjia import live_host as live
from backend.app.modules.integrations.guanjia import sqlite_identity

# Account database, not HOME or another environment-supplied path.
_CONFIG_ROOT = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.config/retail-v4/guanjia-live'
_MAX_JSON = 32768
_MAX_DB = 256 * 1024 * 1024
_ACTIONS = ('live_prices', 'live_stock')


class LoaderDenied(Exception):
    def __init__(self):
        super().__init__('protected_material_unavailable')


def _process_gate():
    if threading.current_thread() is not threading.main_thread() or threading.active_count() != 1:
        raise LoaderDenied()


def _shape(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise LoaderDenied()
    return value


def _text(value, maximum=120):
    if (type(value) is not str or not 0 < len(value) <= maximum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise LoaderDenied()
    return value


def _digest(value):
    if not live.host._digest_valid(value):
        raise LoaderDenied()
    return value


def _integer(value):
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise LoaderDenied()
    return value


def _instant(value):
    _text(value, 40)
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise LoaderDenied() from None
    if result.tzinfo != timezone.utc or result <= datetime.now(timezone.utc):
        raise LoaderDenied()
    return result


def _decode(raw):
    def reject(*_):
        raise LoaderDenied()
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                reject()
            result[key] = value
        return result
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_float=reject, parse_constant=reject)
    except (ValueError, UnicodeError, RecursionError):
        raise LoaderDenied() from None


def _identity(info):
    return info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)


class _Files:
    """Keep open directory/file identities; revalidate paths at every DB use."""
    def __init__(self, root):
        self.root, self.dirs, self.files = root, [], []
        self.content_digests = {}
        self.pid = os.getpid()
        try:
            if (not isinstance(root, Path) or not root.is_absolute() or '..' in root.parts):
                raise LoaderDenied()
            parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.dirs.append((None, '/', parent, os.fstat(parent)))
            for name in root.parts[1:]:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                self.dirs.append((parent, name, fd, os.fstat(fd)))
                parent = fd
                try:
                    os.stat('.git', dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise LoaderDenied()
            self.fd = parent
            self._private(os.fstat(parent), directory=True)
            fd = os.open('ledger', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            self.dirs.append((parent, 'ledger', fd, os.fstat(fd)))
            self._private(os.fstat(fd), directory=True)
        except BaseException:
            self.close()
            raise LoaderDenied() from None

    @staticmethod
    def _private(info, *, directory=False):
        expected = 0o700 if directory else 0o600
        if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != expected
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or (not directory and info.st_nlink != 1)):
            raise LoaderDenied()

    def read(self, name, *, database=False):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        try:
            before = os.fstat(fd)
            self._private(before)
            limit = _MAX_DB if database else _MAX_JSON
            if not 0 < before.st_size <= limit:
                raise LoaderDenied()
            raw = os.read(fd, 16 if database else limit + 1)
            after = os.fstat(fd)
            if (self._file_stamp(before) != self._file_stamp(after)
                    or (database and raw != b'SQLite format 3\x00')
                    or (not database and len(raw) != before.st_size)):
                raise LoaderDenied()
            self.files.append((name, fd, after, database))
            if not database:
                self.content_digests[fd] = sha256(raw).digest()
            fd = None
            self.validate()
            return raw
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _file_stamp(info):
        return _identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink

    def validate(self):
        _process_gate()
        if self.pid != os.getpid():
            raise LoaderDenied()
        for parent, name, fd, original in self.dirs:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or _identity(current) != _identity(original):
                raise LoaderDenied()
            if _identity(os.fstat(fd)) != _identity(original):
                raise LoaderDenied()
        for name, fd, original, database in self.files:
            current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            self._private(current)
            opened = os.fstat(fd)
            if database:
                if (not 16 <= current.st_size <= _MAX_DB
                        or _identity(current) != _identity(original)
                        or _identity(opened) != _identity(original)):
                    raise LoaderDenied()
            elif (self._file_stamp(current) != self._file_stamp(original)
                  or self._file_stamp(opened) != self._file_stamp(original)):
                raise LoaderDenied()
            if not database:
                # Metadata alone is not an integrity check: a same-length
                # edit can fit inside one filesystem timestamp tick. Read the
                # held JSON FD without changing its offset or opening/closing
                # any descriptor that might carry a SQLite process lock.
                try:
                    raw = os.pread(fd, _MAX_JSON + 1, 0)
                    after = os.fstat(fd)
                    named = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                except OSError:
                    raise LoaderDenied() from None
                if (len(raw) != original.st_size
                        or sha256(raw).digest() != self.content_digests.get(fd)
                        or self._file_stamp(after) != self._file_stamp(original)
                        or self._file_stamp(named) != self._file_stamp(original)):
                    raise LoaderDenied()

    def close(self):
        for _, fd, _, _ in reversed(self.files):
            os.close(fd)
        self.files.clear()
        self.content_digests.clear()
        for _, _, fd, _ in reversed(self.dirs):
            os.close(fd)
        self.dirs.clear()


@dataclass(frozen=True, repr=False)
class LoadedLiveRuntime:
    runtime: live.ProtectedLiveRuntime = field(repr=False)
    operation: str
    _files: _Files = field(repr=False)

    def validate(self):
        self._files.validate()
        if self.runtime.permit.expires_at <= datetime.now(timezone.utc):
            raise LoaderDenied()


def _assemble(material, grant, raw, files):
    _shape(material, ('version', 'employee_token', 'username', 'password', 'tenancy_name', 'permit'))
    if type(material['version']) is not str or material['version'] != '1':
        raise LoaderDenied()
    p = _shape(material['permit'], ('scope', 'product', 'agent_subject_id', 'permission_epoch',
        'binding_evidence_sha256', 'time_evidence_sha256', 'expires_at', 'expected_tenant_id',
        'price_evidence_sha256', 'stock_evidence_sha256'))
    scope_data = _shape(p['scope'], ('organization_id', 'store_id', 'tenant_fingerprint', 'shop_id', 'warehouse_id'))
    scope = live.host.read_service.SourceScope(**{k: _text(v) for k, v in scope_data.items()})
    _digest(scope.tenant_fingerprint)
    product_data = _shape(p['product'], ('product_id', 'upstream_product_id', 'snapshot_id',
        'snapshot_sha256', 'source_system', 'source_version', 'confirmed_by', 'expires_at'))
    product = live.host.read_service.ConfirmedProduct(**{
        k: _instant(v) if k == 'expires_at' else _digest(v) if k == 'snapshot_sha256' else _text(v)
        for k, v in product_data.items()})
    permit_expiry = _instant(p['expires_at'])
    _shape(grant, ('version', 'material_sha256', 'nonce_sha256', 'operation', 'expires_at',
        'binding_evidence_sha256', 'permission_epoch'))
    if (type(grant['version']) is not str or grant['version'] != '1'
            or _digest(grant['material_sha256']) != sha256(raw).hexdigest()
            or type(grant['operation']) is not str or grant['operation'] not in _ACTIONS
            or _integer(grant['permission_epoch']) != _integer(p['permission_epoch'])
            or _digest(grant['binding_evidence_sha256']) != _digest(p['binding_evidence_sha256'])):
        raise LoaderDenied()
    expiry = _instant(grant['expires_at'])
    if expiry > min(permit_expiry, product.expires_at):
        raise LoaderDenied()
    permit = live.LivePermit(scope, product, _text(p['agent_subject_id']), _integer(p['permission_epoch']),
        _digest(p['binding_evidence_sha256']), _digest(p['time_evidence_sha256']), expiry,
        _integer(p['expected_tenant_id']), *[
            None if p[k] is None else _digest(p[k]) for k in ('price_evidence_sha256', 'stock_evidence_sha256')])
    if live.binding_digest(permit) != permit.binding_evidence_sha256:
        raise LoaderDenied()
    files.read('authority.sqlite3', database=True)
    original_db = next(info for name, _, info, database in files.files
                       if name == 'authority.sqlite3' and database)
    def connect():
        files.validate()
        if expiry <= datetime.now(timezone.utc):
            raise LoaderDenied()
        # mode=rw forbids SQLite's usual missing-file creation fallback.
        connection = sqlite_identity.connect(files.root / 'authority.sqlite3',
                                             (original_db.st_dev, original_db.st_ino))
        try:
            files.validate()
            connection.execute('PRAGMA foreign_keys=ON')
            return connection
        except BaseException:
            connection.close()
            raise
    engine = create_engine(URL.create('sqlite', database=str(files.root / 'authority.sqlite3')),
                           creator=connect, poolclass=NullPool, hide_parameters=True)
    try:
        runtime = live.ProtectedLiveRuntime(engine, _text(material['employee_token'], 4096), permit,
            files.root / 'ledger', _digest(grant['nonce_sha256']), _text(material['username'], 4096),
            _text(material['password'], 4096), _text(material['tenancy_name'], 4096))
        live._material_gate(runtime, grant['operation'])
        return LoadedLiveRuntime(runtime, grant['operation'], files)
    except BaseException:
        engine.dispose()
        raise


@contextmanager
def load_protected_runtime():
    """Read only fixed protected Host files; no argv/environment source selection.

    Test harnesses may replace the module constant with a private temporary root.
    There is deliberately no production material/grant creation helper.
    """
    files = loaded = None
    try:
        _process_gate()
        files = _Files(_CONFIG_ROOT)
        raw = files.read('material.json')
        grant = files.read('grant.json')
        loaded = _assemble(_decode(raw), _decode(grant), raw, files)
        loaded.validate()
        yield loaded
    except BaseException:
        raise LoaderDenied() from None
    finally:
        if loaded is not None:
            loaded.runtime.engine.dispose()
        if files is not None:
            files.close()
