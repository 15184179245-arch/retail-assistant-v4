"""Host-only fixed source selection. Integrity pins never confer authority.

No database descriptors, network, grants or model-facing credential serializer.
Keep the selection open and revalidate in the Host's authority callback before
and after every protected send. Closing it permanently invalidates selection.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import os
import re
import stat

from backend.app.modules.agents.source_environment import SourceEnvironment
from . import live_loader as protected
from .work_source import _material
from .sales_ranking_host import HostMaterial


class SalesSourceDenied(ValueError):
    def __init__(self):
        super().__init__('sales_ranking_source_rejected')


def _deny():
    raise SalesSourceDenied() from None


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _private(info, directory=False):
    if (info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or not directory and info.st_nlink != 1):
        _deny()


@dataclass(frozen=True)
class SourcePin:
    """Safe immutable new-Run integrity binding; explicitly not a grant."""
    environment_sha256: str
    material_sha256: str
    organization_id: str
    store_id: str
    requester_id: str
    agent_id: str
    machine_id: str
    tenant_id: int
    tenancy_sha256: str
    shop_id: str
    expires_at: str
    version: int = 1


def validate_pin(value):
    """Validate a serialized integrity pin; this does not authorize a Run.

    store_id is the internal authority scope; shop_id is the upstream report
    filter. They are independently bound and must never substitute each other.
    No credentials, paths or plaintext tenancy belong in this JSON schema.
    """
    try:
        if type(value) is dict and type(value.get('version')) is int and value['version']==3:
            from backend.app.modules.agents.local_sales_runtime import validate_source
            return validate_source(value)
        fields = set(SourcePin.__dataclass_fields__)
        if type(value) is not dict or set(value) != fields:
            _deny()
        if type(value['version']) is not int or value['version'] != 1:
            _deny()
        for key in ('environment_sha256', 'material_sha256', 'tenancy_sha256'):
            protected._digest(value[key])
        for key in ('organization_id', 'store_id', 'requester_id', 'agent_id', 'machine_id'):
            if type(value[key]) is not str or re.fullmatch(r'[A-Za-z0-9_-]{1,120}', value[key]) is None:
                _deny()
        protected._text(value['shop_id'])
        # Explicitly reject invalid Unicode as well as control characters.
        value['shop_id'].encode('utf-8')
        protected._integer(value['tenant_id'])
        protected._instant(value['expires_at'])
        return asdict(SourcePin(**value))
    except (ValueError, TypeError, KeyError, UnicodeError, protected.LoaderDenied):
        raise SalesSourceDenied() from None


class SourceSelection:
    __slots__ = ('_environment', '_rootfd', '_fd', '_root_identity', '_original',
                 '_raw', '_pin', '_pid')

    def __repr__(self):
        return '<SalesRankingSourceSelection>'

    def __init__(self, environment, material_sha256, expected_tenant_id, tenancy_name=None, *, tenancy_sha256=None):
        self._rootfd = self._fd = None
        try:
            if type(environment) is not SourceEnvironment or environment.trial_name is not None:
                _deny()
            protected._digest(material_sha256)
            protected._integer(expected_tenant_id)
            if tenancy_name is not None: protected._text(tenancy_name, 4096)
            if tenancy_sha256 is not None: protected._digest(tenancy_sha256)
            if tenancy_name is None and tenancy_sha256 is None: _deny()
            environment.revalidate()
            self._environment = environment
            root = environment.root
            if root.resolve(strict=True) != root:
                _deny()
            self._rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(self._rootfd)
            _private(info, True)
            self._root_identity = (info.st_dev, info.st_ino)
            self._fd = os.open('material.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                               dir_fd=self._rootfd)
            info = os.fstat(self._fd)
            _private(info)
            if not 0 < info.st_size <= 32768:
                _deny()
            self._original = _stamp(info)
            self._raw = os.pread(self._fd, 32769, 0)
            if sha256(self._raw).hexdigest() != material_sha256:
                _deny()
            material = _material(self._raw)
            values = environment.values
            if ((material['organization_id'], material['store_id'], material['agent_subject_id'],
                 material['machine_token']) != (values['organization_id'], values['store_id'],
                    values['agent_id'], environment.machine_token)
                    or material['expected_tenant_id'] != expected_tenant_id
                    or tenancy_name is not None and material['tenancy_name'] != tenancy_name
                    or tenancy_sha256 is not None and sha256(material['tenancy_name'].encode()).hexdigest() != tenancy_sha256):
                _deny()
            self._pin = SourcePin(environment.sha256, material_sha256,
                *(values[k] for k in ('organization_id', 'store_id', 'requester_id', 'agent_id', 'machine_id')),
                expected_tenant_id, sha256(material['tenancy_name'].encode()).hexdigest(),
                material['shop_id'], material['expires_at'])
            self._pid = os.getpid()
            self.revalidate()
        except BaseException:
            self.close()
            raise SalesSourceDenied() from None

    def pin(self):
        self.revalidate()
        return self._pin

    def pin_dict(self):
        """Fresh JSON-safe pin for trusted Run binding, never a permission."""
        return validate_pin(asdict(self.pin()))

    def revalidate(self):
        try:
            if self._fd is None or self._rootfd is None or self._pid != os.getpid():
                _deny()
            self._environment.revalidate()
            root = self._environment.root
            if root.resolve(strict=True) != root:
                _deny()
            for info in (os.fstat(self._rootfd), os.stat(root, follow_symlinks=False)):
                _private(info, True)
                if (info.st_dev, info.st_ino) != self._root_identity:
                    _deny()
            for info in (os.fstat(self._fd), os.stat('material.json', dir_fd=self._rootfd, follow_symlinks=False)):
                _private(info)
                if _stamp(info) != self._original:
                    _deny()
            raw = os.pread(self._fd, 32769, 0)
            if raw != self._raw or sha256(raw).hexdigest() != self._pin.material_sha256:
                _deny()
            if _stamp(os.fstat(self._fd)) != self._original:
                _deny()
            # _material rechecks expiry every time, not only at Run creation.
            _material(raw)
        except BaseException:
            raise SalesSourceDenied() from None

    def host_material(self):
        self.revalidate()
        material = _material(self._raw)
        # HostMaterial.store_id names the UPSTREAM filter, not internal scope.
        return HostMaterial(material['shop_id'], material['username'], material['password'],
            material['tenancy_name'], material['expected_tenant_id'],
            str(self._environment.root / 'sales-ranking-journal'))

    def close(self):
        for key in ('_fd', '_rootfd'):
            fd = getattr(self, key, None)
            if fd is not None:
                os.close(fd)
                setattr(self, key, None)
        self._raw = b''

    def __del__(self):
        try:
            self.close()
        except BaseException:
            pass

    def __enter__(self):
        self.revalidate()
        return self

    def __exit__(self, *_):
        self.close()


def select_source(environment, *, material_sha256, expected_tenant_id, tenancy_name):
    """Trusted Host inputs only; never register as an Agent tool."""
    return SourceSelection(environment, material_sha256, expected_tenant_id, tenancy_name)


# Host-only mapping, never serialized into model frames. It opens no SQLite FD.
from threading import RLock
from weakref import WeakKeyDictionary
_SOURCES = WeakKeyDictionary()
_LOCK = RLock()


def _check_engine(selected, engine):
    selected.revalidate()
    environment = selected._environment
    info = os.stat(environment.database, follow_symlinks=False)
    if (engine.url.drivername != 'sqlite' or engine.url.database != str(environment.database)
            or (info.st_dev, info.st_ino) != environment.database_identity):
        _deny()


def configure(engine, environment, *, material_sha256, expected_tenant_id, tenancy_sha256):
    engine = getattr(engine, 'engine', engine)
    selected = SourceSelection(environment, material_sha256, expected_tenant_id,
                               tenancy_sha256=tenancy_sha256)
    try:
        _check_engine(selected, engine)
        with _LOCK:
            old = _SOURCES.get(engine)
            if old is not None:
                _check_engine(old, engine)
                if old.pin_dict() != selected.pin_dict(): _deny()
                selected.close()
                return old
            _SOURCES[engine] = selected
        return selected
    except BaseException:
        selected.close()
        raise


def select_local_source(environment, *, descriptor_sha256):
    from .local_sales_source import LocalSourceSelection
    return LocalSourceSelection(environment,descriptor_sha256)


def configure_local(engine,environment,*,descriptor_sha256):
    engine=getattr(engine,'engine',engine)
    selected=select_local_source(environment,descriptor_sha256=descriptor_sha256)
    try:
        _check_engine(selected,engine)
        with _LOCK:
            old=_SOURCES.get(engine)
            if old is not None:
                _check_engine(old,engine)
                if old.pin_dict()!=selected.pin_dict():_deny()
                selected.close()
                return old
            _SOURCES[engine]=selected
        return selected
    except BaseException:
        selected.close()
        raise


def current(engine, actor=None, pin=None):
    engine = getattr(engine, 'engine', engine)
    with _LOCK:
        selected = _SOURCES.get(engine)
    if selected is None: _deny()
    _check_engine(selected, engine)
    value = selected.pin_dict()
    if actor is not None and (actor.organization_id, actor.store_id, actor.subject_id) != tuple(
            value[k] for k in ('organization_id', 'store_id', 'requester_id')): _deny()
    if pin is not None and validate_pin(pin) != value: _deny()
    return selected


def citation(engine, actor, reference):
    try:
        from . import sales_ranking_process
        result = sales_ranking_process.consume(current(engine, actor), actor, reference)
        if result is None: return None
        return dict(kind='guanjia_sales_ranking', object_id=reference['id'],
            version_ref=reference['sha256'], version=reference['sha256'],
            label=('管家婆本地逐笔销量分析' if result.get('version')==3 else '管家婆本月商品销售排行候选（口径待核）'),
            source_ref='guanjia-sales-ranking:'+reference['id'],
            observed_at=(result['collection']['captured_through'] or result['collection']['queried_at']) if result.get('version')==3 else result['collection']['as_of_utc'])
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        return None
