"""Fixed, private non-synthetic Host identity; no SQL, grants or network.

Do not open the authority DB here: closing an auxiliary FD can release SQLite
process locks. The source worker independently checks its actual database FD.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat

from backend.app.modules.integrations.guanjia.work_source import _ROOT

_TRIAL_ROOT = _ROOT.parent / 'pi-research-trials'
_TRIAL_MARKER = {'version': 1, 'purpose': 'isolated-pi-research', 'business_access': False}

def trial_name(value):
    if type(value) is not str or re.fullmatch(r'research-[a-z0-9-]{1,30}',value) is None:_deny()
    return value

_FIELDS = {'version','organization_id','store_id','requester_id','agent_id','machine_id','machine_token'}

class SourceEnvironmentError(ValueError):
    def __init__(self):super().__init__('pi_source_environment_rejected')

def _deny():raise SourceEnvironmentError()

def _private(info, directory=False):
    if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or not directory and info.st_nlink != 1):_deny()

def _identity(info):return info.st_dev,info.st_ino

def _stamp(info):return _identity(info),info.st_size,info.st_mtime_ns,info.st_ctime_ns

def _decode(raw, *, trial=False):
    def unique(rows):
        result={}
        for k,v in rows:
            if k in result:_deny()
            result[k]=v
        return result
    try:
        value=json.loads(raw,object_pairs_hook=unique)
        if type(value) is not dict or set(value)!=_FIELDS or type(value['version']) is not int or value['version']!=1:_deny()
        for key in _FIELDS-{'version','machine_token'}:
            text=value[key]
            if type(text) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',text) or text.startswith('agt044-dev-'):_deny()
            if trial and not text.startswith('pi-research-'):_deny()
        token=value['machine_token']
        if type(token) is not str or not re.fullmatch(r'sx2_[A-Za-z0-9_-]{43}',token) or token=='sx2_'+'C'*43:_deny()
        return value
    except (ValueError,TypeError,UnicodeError,RecursionError):raise SourceEnvironmentError() from None

@dataclass(frozen=True,repr=False)
class SourceEnvironment:
    root: Path
    database: Path
    database_identity: tuple[int,int]
    values: dict = field(repr=False)
    sha256: str
    trial_name: str | None = None

    @property
    def machine_token(self):return self.values['machine_token']

    def check_binding(self,cfg):
        for key in _FIELDS-{'version','machine_token'}:
            if cfg.get(key)!=self.values[key]:_deny()

    def revalidate(self):
        current=load(self.sha256,trial_name=self.trial_name)
        if current.database_identity!=self.database_identity or current.values!=self.values:_deny()


def load(expected_sha256, *, trial_name=None):
    """Read fixed environment.json; callers cannot choose root or DB path."""
    rootfd=fd=None
    try:
        if type(expected_sha256) is not str or not re.fullmatch('[a-f0-9]{64}',expected_sha256):_deny()
        isolated=trial_name is not None
        if isolated:
            name=globals()['trial_name'](trial_name)
            if _TRIAL_ROOT.resolve(strict=True)!=_TRIAL_ROOT:_deny()
            _private(os.stat(_TRIAL_ROOT,follow_symlinks=False),True)
            root=_TRIAL_ROOT/name
        else:root=_ROOT
        if root.resolve(strict=True)!=root:_deny()
        rootfd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        rootinfo=os.fstat(rootfd);_private(rootinfo,True)
        if os.path.lexists(root/'synthetic-only.json'):_deny()
        def marker():
            if not isolated:
                if os.path.lexists(root/'research-only.json'):_deny()
                return
            if os.path.lexists(root/'material.json'):_deny()
            markerfd=os.open('research-only.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=rootfd)
            try:
                before=os.fstat(markerfd);_private(before)
                raw=os.read(markerfd,1025)
                if not 0<len(raw)<=1024 or len(raw)!=before.st_size:_deny()
                def unique(rows):
                    value={}
                    for key,item in rows:
                        if key in value:_deny()
                        value[key]=item
                    return value
                value=json.loads(raw,object_pairs_hook=unique)
                if (value!=_TRIAL_MARKER or type(value.get('version')) is not int
                        or value.get('business_access') is not False):_deny()
                if (_stamp(before)!=_stamp(os.fstat(markerfd))
                        or _stamp(before)!=_stamp(os.stat('research-only.json',dir_fd=rootfd,follow_symlinks=False))):_deny()
            finally:os.close(markerfd)
        marker()
        fd=os.open('environment.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=rootfd)
        before=os.fstat(fd);_private(before)
        if not 0<before.st_size<=16384:_deny()
        raw=os.read(fd,16385)
        if len(raw)!=before.st_size or sha256(raw).hexdigest()!=expected_sha256:_deny()
        values=_decode(raw,trial=isolated)
        database=os.stat('authority.sqlite3',dir_fd=rootfd,follow_symlinks=False);_private(database)
        if database.st_size<16:_deny()
        named=os.stat('environment.json',dir_fd=rootfd,follow_symlinks=False);_private(named)
        if (_stamp(before)!=_stamp(os.fstat(fd)) or _stamp(before)!=_stamp(named)
                or _identity(os.stat(root,follow_symlinks=False))!=_identity(rootinfo)
                or root.resolve(strict=True)!=root):_deny()
        marker()
        return SourceEnvironment(root,root/'authority.sqlite3',_identity(database),values,expected_sha256,trial_name)
    except (OSError,ValueError,TypeError,RuntimeError):raise SourceEnvironmentError() from None
    finally:
        if fd is not None:os.close(fd)
        if rootfd is not None:os.close(rootfd)
