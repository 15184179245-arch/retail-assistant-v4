"""Explicit new-Run corpus pin; threaded Host never opens another SQLite FD."""
from dataclasses import dataclass
from weakref import WeakKeyDictionary
from threading import RLock
import os,re

_SOURCES=WeakKeyDictionary()
_LOCK=RLock()

def validate_pin(pin):
    if (type(pin) is not dict or set(pin)!=({'version','directory_name','manifest_sha256'}|({'trial_name'} if 'trial_name' in pin else set())|({'read'} if 'read' in pin else set()))
        or type(pin['version']) is not int or pin['version']!=1
        or type(pin['directory_name']) is not str or re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,63}',pin['directory_name']) is None
        or type(pin['manifest_sha256']) is not str or re.fullmatch('[a-f0-9]{64}',pin['manifest_sha256']) is None):
        raise ValueError('research_source_unavailable')
    if 'trial_name' in pin:
        from .source_environment import trial_name
        trial_name(pin['trial_name'])
    if 'read' in pin:
        from .research_broker_host import validate_read_pin
        if 'trial_name' in pin:raise ValueError('research_source_unavailable')
        validate_read_pin(pin['read'])
    return dict(pin)

@dataclass(frozen=True,repr=False)
class HostResearchSource:
    environment: object
    directory_name: str
    manifest_sha256: str
    read_pin: dict | None = None
    def pin(self):
        value=dict(version=1,directory_name=self.directory_name,manifest_sha256=self.manifest_sha256)
        if self.environment.trial_name is not None:value['trial_name']=self.environment.trial_name
        if self.read_pin is not None:value['read']=self.read_pin
        return validate_pin(value)
    def config(self):
        from .research_catalog import CorpusConfig
        v=self.environment.values
        return CorpusConfig(str(self.environment.root/self.directory_name),self.manifest_sha256,
                            v['organization_id'],v['store_id'],v['requester_id'])
    def check(self,engine):
        from . import research_catalog
        self.environment.revalidate()
        st=os.stat(self.environment.database,follow_symlinks=False)
        if (engine.url.drivername!='sqlite' or engine.url.database!=str(self.environment.database)
            or (st.st_dev,st.st_ino)!=self.environment.database_identity):raise ValueError('research_source_unavailable')
        data,_=research_catalog._load(self.config())
        # Source/2 tool frames retain the existing safe-integer grammar. Do
        # not silently round numeric schema bounds or rewrite a pinned schema.
        from .runtime_protocol import _shape
        _shape(data)
        if self.read_pin is not None:
            from . import research_broker_host
            for item in self.read_pin['contracts']:
                obj=research_broker_host.contract.ApprovedReadContract(str(self.environment.root/item['directory_name']),item['manifest_sha256'])
                research_broker_host.approved(self,obj.load()['contract_id'])
            research_broker_host._read(self.environment.root,'material.json',self.read_pin['material_sha256'])

def configure(engine,environment,directory_name,manifest_sha256,*,read=None):
    from .source_environment import SourceEnvironment
    if type(environment) is not SourceEnvironment:raise ValueError('research_source_unavailable')
    validate_pin(dict(version=1,directory_name=directory_name,manifest_sha256=manifest_sha256))
    selected=HostResearchSource(environment,directory_name,manifest_sha256,read)
    selected.check(engine)
    with _LOCK:
        old=_SOURCES.get(engine)
        if old is not None:
            if old.pin()!=selected.pin() or old.environment.sha256!=environment.sha256:raise ValueError('research_source_unavailable')
            return old
        _SOURCES[engine]=selected
    return selected

def current(engine,actor=None,pin=None):
    engine=getattr(engine,'engine',engine)
    with _LOCK:source=_SOURCES.get(engine)
    if source is None:raise ValueError('research_source_unavailable')
    source.check(engine)
    if actor is not None and (actor.organization_id,actor.store_id,actor.subject_id)!=tuple(
            source.environment.values[k] for k in ('organization_id','store_id','requester_id')):raise ValueError('research_source_unavailable')
    if pin is not None and validate_pin(pin)!=source.pin():raise ValueError('research_source_unavailable')
    return source

def citation(engine,actor,reference):
    try:
        from . import research_process
        return research_process.consume(current(engine,actor),actor,reference)
    except (ValueError,TypeError,KeyError,AttributeError,OSError):return None


def citations(engine,actor,references,*,deadline=None):
    try:
        from . import research_process
        return research_process.consume_batch(current(engine,actor),actor,references,deadline=deadline)
    except (ValueError,TypeError,KeyError,AttributeError,OSError):return None
