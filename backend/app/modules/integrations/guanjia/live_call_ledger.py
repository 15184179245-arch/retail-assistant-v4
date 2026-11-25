"""Durable one-shot consumption, NOT an authorization or permit issuing API.

Only a protected Host may supply binding/configuration after authorization. No
model/CLI adapter is provided. A binding value alone proves no authority. Never
remove reservations during rollback. Call network_entered BEFORE network entry;
only returned/failed establishes a completed entry. Incomplete entry is [0,1].
The configured root must remain owned exclusively by this Host UID, without
group/world access, and its identity must remain stable. Same-UID administrators
can destroy or replace storage; preventing that requires deployment isolation.
Root/ancestor ACLs and mount lifecycle must likewise be protected by the Host.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat

_OPERATIONS = frozenset({'authenticate', 'live_prices', 'live_stock'})
_ERRORS = frozenset({'none', 'transport_failed', 'authorization_changed', 'storage_failed', 'unknown'})
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_REPOSITORY = Path(__file__).resolve().parents[5]


class LedgerDenied(Exception):
    def __init__(self):
        super().__init__('ledger_denied')


@dataclass(frozen=True, repr=False)
class HostPermitBinding:
    scope_sha256: str
    product_binding_sha256: str
    nonce_sha256: str
    authorization_epoch: int
    expires_at: datetime
    operations: frozenset[str]

    def _payload(self):
        if (any(type(v) is not str or not _DIGEST.fullmatch(v) for v in
                (self.scope_sha256, self.product_binding_sha256, self.nonce_sha256))
                or type(self.authorization_epoch) is not int or self.authorization_epoch < 1
                or type(self.expires_at) is not datetime
                or self.expires_at.tzinfo != timezone.utc
                or type(self.operations) is not frozenset
                or not self.operations or not self.operations <= _OPERATIONS):
            raise LedgerDenied()
        return {'scope_sha256': self.scope_sha256,
                'product_binding_sha256': self.product_binding_sha256,
                'nonce_sha256': self.nonce_sha256,
                'authorization_epoch': self.authorization_epoch,
                'expires_at': self.expires_at.isoformat(),
                'operations': sorted(self.operations)}

    def digest(self):
        return sha256(_encode(self._payload())).hexdigest()


def _encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')


def _root_fd(root):
    path = Path(root)
    if not path.is_absolute() or '..' in path.parts:
        raise LedgerDenied()
    # Reject the current checkout and its descendants, including lexical aliases.
    if path == _REPOSITORY or _REPOSITORY in path.parents:
        raise LedgerDenied()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
            try:
                os.stat('.git', dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise LedgerDenied()
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise LedgerDenied()
        return fd
    except BaseException:
        os.close(fd)
        raise


@dataclass(repr=False)
class LiveCallLedger:
    _fd: int
    _binding: HostPermitBinding = field(repr=False)
    _events: list = field(default_factory=list, repr=False)
    _frozen: bool = False
    _pid: int = field(default_factory=os.getpid)
    _root_handle: int = -1
    _root_path: Path | None = field(default=None, repr=False)

    def _verify_storage(self):
        if self._root_handle < 0 or self._root_path is None:
            raise LedgerDenied()
        fresh = _root_fd(self._root_path)
        try:
            root_info = os.fstat(self._root_handle)
            fresh_info = os.fstat(fresh)
            own = os.fstat(self._fd)
            named = os.stat(self._binding.nonce_sha256, dir_fd=fresh,
                            follow_symlinks=False)
            if ((root_info.st_dev, root_info.st_ino) !=
                    (fresh_info.st_dev, fresh_info.st_ino)
                    or (own.st_dev, own.st_ino) != (named.st_dev, named.st_ino)
                    or not stat.S_ISDIR(named.st_mode)
                    or own.st_uid != os.getuid()
                    or stat.S_IMODE(own.st_mode) & 0o077):
                raise LedgerDenied()
        finally:
            os.close(fresh)

    @classmethod
    def reserve(cls, root, binding):
        parent = directory = None
        try:
            if type(binding) is not HostPermitBinding:
                raise LedgerDenied()
            binding.digest()
            if binding.expires_at <= datetime.now(timezone.utc):
                raise LedgerDenied()
            parent = _root_fd(root)
            # Nonce alone is the consumption identity; changed scope/epoch/run
            # cannot provide another reservation for an already consumed nonce.
            os.mkdir(binding.nonce_sha256, mode=0o700, dir_fd=parent)
            os.fsync(parent)
            directory = os.open(binding.nonce_sha256,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            ledger = cls(directory, binding, _root_handle=parent, _root_path=Path(root))
            ledger._append({'operation': 'permit', 'state': 'reserved',
                            'binding_sha256': binding.digest()})
            directory = parent = None
            return ledger
        except (OSError, ValueError, TypeError, LedgerDenied):
            raise LedgerDenied() from None
        finally:
            if parent is not None: os.close(parent)
            if directory is not None: os.close(directory)

    def _append(self, event):
        event = dict(event, utc=datetime.now(timezone.utc).isoformat())
        fd = None
        try:
            self._verify_storage()
            fd = os.open(f'{len(self._events):04d}.json',
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=self._fd)
            data = _encode(event)
            while data:
                written = os.write(fd, data)
                if written <= 0: raise OSError()
                data = data[written:]
            os.fsync(fd)
            os.fsync(self._fd)
            self._verify_storage()
            self._events.append(event)
        except BaseException:
            self._frozen = True
            raise
        finally:
            if fd is not None: os.close(fd)

    def record(self, operation, state, *, response_sha256=None, error_code='none'):
        try:
            if (self._frozen or self._fd < 0 or self._pid != os.getpid()
                    or self._binding.expires_at <= datetime.now(timezone.utc)
                    or type(operation) is not str or operation not in self._binding.operations
                    or type(state) is not str or type(error_code) is not str
                    or error_code not in _ERRORS
                    or (response_sha256 is not None and
                        (type(response_sha256) is not str or not _DIGEST.fullmatch(response_sha256)))):
                raise LedgerDenied()
            previous = [e['state'] for e in self._events if e['operation'] == operation]
            allowed = ('send_intent',) if not previous else (
                ('network_entered',) if previous[-1] == 'send_intent' else
                ('returned', 'failed', 'unknown') if previous[-1] == 'network_entered' else ())
            if state not in allowed or (response_sha256 is not None and state != 'returned'):
                raise LedgerDenied()
            self._append({'operation': operation, 'state': state,
                          'response_sha256': response_sha256, 'error_code': error_code})
        except (OSError, ValueError, TypeError, LedgerDenied):
            self._frozen = True
            raise LedgerDenied() from None

    def counts(self):
        if self._frozen:
            return {operation: (0, 1) for operation in _OPERATIONS}
        result = {}
        for operation in _OPERATIONS:
            states = [e['state'] for e in self._events if e['operation'] == operation]
            result[operation] = ((1, 1) if states and states[-1] in {'returned', 'failed'} else
                                 (0, 1) if states else (0, 0))
        return result

    def close(self):
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        if self._root_handle >= 0:
            os.close(self._root_handle)
            self._root_handle = -1

    @classmethod
    def inspect(cls, root, binding):
        """Read-only restart receipt; never reopens a write capability."""
        parent = directory = None
        try:
            if type(binding) is not HostPermitBinding: raise LedgerDenied()
            binding.digest()
            parent = _root_fd(root)
            directory = os.open(binding.nonce_sha256,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            names = sorted(os.listdir(directory))
            if not names or names != [f'{i:04d}.json' for i in range(len(names))]:
                raise LedgerDenied()
            replay = cls(-1, binding)
            for index, name in enumerate(names):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode): raise LedgerDenied()
                    raw = os.read(fd, 4097)
                finally:
                    os.close(fd)
                event = json.loads(raw)
                if _encode(event) != raw: raise LedgerDenied()
                instant = datetime.fromisoformat(event['utc'])
                if instant.tzinfo != timezone.utc: raise LedgerDenied()
                if index == 0:
                    if set(event) != {'operation', 'state', 'binding_sha256', 'utc'} or event != dict(
                            operation='permit', state='reserved', binding_sha256=binding.digest(), utc=event['utc']):
                        raise LedgerDenied()
                else:
                    if set(event) != {'operation', 'state', 'response_sha256', 'error_code', 'utc'}:
                        raise LedgerDenied()
                    operation, state = event['operation'], event['state']
                    prior = [e['state'] for e in replay._events if e['operation'] == operation]
                    allowed = ('send_intent',) if not prior else (
                        ('network_entered',) if prior[-1] == 'send_intent' else
                        ('returned', 'failed', 'unknown') if prior[-1] == 'network_entered' else ())
                    if (operation not in binding.operations or state not in allowed
                            or event['error_code'] not in _ERRORS
                            or (event['response_sha256'] is not None and
                                (state != 'returned' or not _DIGEST.fullmatch(event['response_sha256'])))):
                        raise LedgerDenied()
                replay._events.append(event)
            replay._fd = directory
            replay._root_handle = parent
            replay._root_path = Path(root)
            replay._verify_storage()
            return replay.counts()
        except (OSError, ValueError, TypeError, KeyError, LedgerDenied):
            return {operation: (0, 1) for operation in _OPERATIONS}
        finally:
            if parent is not None: os.close(parent)
            if directory is not None: os.close(directory)
