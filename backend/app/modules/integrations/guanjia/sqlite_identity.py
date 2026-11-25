"""Actual SQLite main-file identity. Dedicated main thread only, no fallback.

The artifact is trusted application code built separately from our fixed C
source. Materials, environment and callers cannot select a loadable extension.
The native VFS hook is removed before returning the ordinary SQLite connection.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sqlite3
import stat
import threading

_ROOT = Path(__file__).resolve().parents[5]
_SOURCE = Path(__file__).parent / 'native/sqlite_open_guard.c'
_SQLITE = '3.53.1'
_SOURCE_ID = '2026-05-05 10:34:17 c88b22011a54b4f6fbd149e9f8e4de77658ce58143a1af0e3785e4e6475127e9'
_BUSY = False


class IdentityDenied(Exception):
    def __init__(self):
        super().__init__('protected_database_identity_unavailable')


def _process():
    if threading.current_thread() is not threading.main_thread() or threading.active_count() != 1:
        raise IdentityDenied()


def _layout():
    if platform.system() != 'Darwin' or platform.machine() != 'x86_64' or sqlite3.sqlite_version != _SQLITE:
        raise IdentityDenied()
    if _SOURCE.is_symlink() or not _SOURCE.is_file():
        raise IdentityDenied()
    source = _SOURCE.read_bytes()
    if not 0 < len(source) <= 32768:
        raise IdentityDenied()
    digest = sha256(source).hexdigest()
    base = _ROOT / 'build/guanjia-sqlite-identity'
    return source, digest, base / (digest + '-darwin-x86_64-sqlite' + _SQLITE)


def _owned(path, *, directory=False):
    info = path.lstat()
    if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise IdentityDenied()


def _read(path):
    _owned(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 4*1024*1024:
            raise IdentityDenied()
        raw = stream.read(4*1024*1024+1)
        after = os.fstat(stream.fileno())
    fields = ('st_dev','st_ino','st_mode','st_uid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
    current = path.lstat()
    if len(raw) != before.st_size or any(getattr(before,k)!=getattr(after,k) or
            getattr(after,k)!=getattr(current,k) for k in fields):
        raise IdentityDenied()
    _owned(path)
    return raw


def _artifact():
    try:
        _, digest, directory = _layout()
        for path in (directory.parent.parent, directory.parent, directory):
            _owned(path, directory=True)
        def unique(pairs):
            out = {}
            for key,value in pairs:
                if key in out: raise IdentityDenied()
                out[key] = value
            return out
        manifest = json.loads(_read(directory/'manifest.json'), object_pairs_hook=unique)
        binary = directory/'guard.dylib'
        raw = _read(binary)
        expected = dict(version=1, source_sha256=digest, binary_sha256=sha256(raw).hexdigest(),
                        bytes=len(raw), platform='Darwin', machine='x86_64', sqlite_version=_SQLITE,
                        sqlite_source_id=_SOURCE_ID)
        if manifest != expected or type(manifest.get('version')) is not int or type(manifest.get('bytes')) is not int:
            raise IdentityDenied()
        return binary
    except (OSError, ValueError, TypeError):
        raise IdentityDenied() from None


def connect(path, expected):
    """Open exactly the pre-held database inode; caller retains its FD and gate.

    No path/URI options or SQL come from material. A failed open is never retried.
    Native post-open identity or hook-restore failures exit this dedicated
    process with 78. Never catch-and-continue or restart the operation: closing
    a rejected FD could release another live SQLite transaction's POSIX locks.
    """
    global _BUSY
    _process()
    if (_BUSY or not isinstance(path, Path) or not path.is_absolute()
            or path.name != 'authority.sqlite3' or '..' in path.parts
            or type(expected) is not tuple or len(expected) != 2
            or any(type(v) is not int for v in expected) or not 0 <= expected[0] < 2**63
            or not 0 < expected[1] < 2**63):
        raise IdentityDenied()
    _BUSY = True
    control = connection = None
    owner = os.getpid()
    try:
        binary = _artifact()
        control = sqlite3.connect(':memory:')
        control.enable_load_extension(True)
        try:
            control.load_extension(str(binary), entrypoint='sqlite3_v4identity_init')
        finally:
            control.enable_load_extension(False)
        if _artifact() != binary:
            raise IdentityDenied()
        if control.execute('SELECT v4_identity_arm(?,?,?)', (str(path), *expected)).fetchone() != (1,):
            raise IdentityDenied()
        try:
            _process()
            if os.getpid() != owner: raise IdentityDenied()
            connection = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True)
        finally:
            valid = control.execute('SELECT v4_identity_finish()').fetchone()
        _process()
        if valid != (1,) or os.getpid() != owner:
            raise IdentityDenied()
        control.close(); control = None
        result = connection; connection = None
        return result
    finally:
        try:
            if control is not None: control.close()
        finally:
            try:
                if connection is not None: connection.close()
            finally:
                _BUSY = False
