"""Internal category candidate configuration; production version13 stays held.

Host metadata checks select a frozen candidate but never attest pooled SQLite
FDs. Execution and historical reads go through a fixed dedicated subprocess;
each worker connection uses a sqlite_identity creator for receipt reads.
Configuration opens no auxiliary database connection in the threaded Host.
"""
from dataclasses import dataclass
from pathlib import Path
import os
import re
from threading import RLock
from weakref import WeakKeyDictionary

from .category_source_loader import load_category_source

_LOCK = RLock()
_SOURCES = WeakKeyDictionary()


class CategoryHostDenied(ValueError):
    def __init__(self):
        super().__init__('guanjia_category_host_unavailable')


def _deny():
    raise CategoryHostDenied()


def validate_pin(pin):
    if (type(pin) is not dict or set(pin) != {'version','directory_name','artifact_sha256','content_sha256','observed_at'}
            or type(pin['version']) is not int or pin['version'] != 1
            or type(pin['directory_name']) is not str or re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,63}',pin['directory_name']) is None
            or any(type(pin[k]) is not str or re.fullmatch('[a-f0-9]{64}',pin[k]) is None
                   for k in ('artifact_sha256','content_sha256'))):
        _deny()
    from datetime import datetime
    observed = datetime.fromisoformat(pin['observed_at'])
    if observed.tzinfo is None or observed.utcoffset() is None:
        _deny()
    return dict(pin)


@dataclass(frozen=True, repr=False)
class HostCategorySource:
    environment: object
    directory_name: str
    artifact_sha256: str
    content_sha256: str
    observed_at: str

    def pin(self):
        return validate_pin(dict(version=1,directory_name=self.directory_name,
            artifact_sha256=self.artifact_sha256,content_sha256=self.content_sha256,observed_at=self.observed_at))

    def binding(self):
        from .category_receipts import CategorySourceBinding
        values = self.environment.values
        return CategorySourceBinding(values['organization_id'],values['store_id'],
            self.environment.root/self.directory_name,self.artifact_sha256)

    def check(self, engine):
        # Expected pathname metadata only, never a claim about pooled SQLite FDs.
        self.environment.revalidate()
        info = os.stat(self.environment.database,follow_symlinks=False)
        if (engine.url.drivername != 'sqlite' or engine.url.database != str(self.environment.database)
                or (info.st_dev,info.st_ino) != self.environment.database_identity):
            _deny()
        source = load_category_source(self.environment.root/self.directory_name,self.artifact_sha256)
        if source.content_sha256 != self.content_sha256 or source.directory.observed_at.isoformat() != self.observed_at:
            _deny()


def configure(engine, environment, directory_name, artifact_sha256):
    """Internal candidate assembly; production bootstrap must not call this."""
    from backend.app.modules.agents.source_environment import SourceEnvironment
    if type(environment) is not SourceEnvironment:
        _deny()
    # Validate path components before using the filesystem.
    validate_pin(dict(version=1,directory_name=directory_name,artifact_sha256=artifact_sha256,
                      content_sha256='0'*64,observed_at='2026-01-01T00:00:00+00:00'))
    source = load_category_source(environment.root/directory_name,artifact_sha256)
    selected = HostCategorySource(environment,directory_name,artifact_sha256,source.content_sha256,
                                  source.directory.observed_at.isoformat())
    selected.check(engine)
    with _LOCK:
        old = _SOURCES.get(engine)
        if old is not None:
            if old.pin() != selected.pin() or old.environment.sha256 != environment.sha256:
                _deny()
            return old
        _SOURCES[engine] = selected
    return selected


def current(engine, actor=None, pin=None):
    engine = getattr(engine,'engine',engine)
    with _LOCK:
        source = _SOURCES.get(engine)
    if source is None:
        _deny()
    source.check(engine)
    if actor is not None and (actor.organization_id,actor.store_id) != (
            source.environment.values['organization_id'],source.environment.values['store_id']):
        _deny()
    if pin is not None and validate_pin(pin) != source.pin():
        _deny()
    return source


def citation(engine, actor, reference):
    try:
        source = current(engine,actor)
        # The child checks the complete persisted Run pin in consume's SAME
        # fresh read root as the authorized result, on an attested connection.
        from . import category_process
        # Even a source worker can already hold a writer transaction. Closing
        # an auxiliary SQLite FD there could release its POSIX locks. History
        # always uses another fresh process, never another local connection.
        result = category_process.consume(source,actor,reference)
        if result is None:
            return None
        value = result['category_result']
        return dict(kind='guanjia_category',object_id=reference['id'],version_ref=reference['sha256'],
            version=reference['sha256'],label='管家婆分类及商品目录（本地来源快照）',
            source_ref='guanjia-category:'+reference['id'],observed_at=value['directory_observed_at'])
    except (ValueError,TypeError,KeyError,AttributeError,OSError):
        return None
