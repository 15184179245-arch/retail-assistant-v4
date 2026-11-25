"""Host-only bridge from one committed native intent to reviewed bounded reads.

Pinned research descriptions are not permissions. Only this fixed source worker
may load independent approved contracts/material. Authentication is permanently
consumed before sending. Historical metadata is bound to the native output hash,
not inferred from a current contract name or a model-supplied citation.
"""
from datetime import datetime,timezone
from hashlib import sha256
import os,re,stat,time
from pathlib import Path
from sqlalchemy.orm import Session
from . import research_contracts as c,research_catalog as catalog
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.integrations.guanjia import research_read_contract as contract
from backend.app.modules.integrations.guanjia import research_read_broker as broker
from backend.app.modules.integrations.guanjia import research_auth_transport as authentication
from backend.app.modules.integrations.guanjia import work_source


def deny():raise ValueError('research_read_host_rejected') from None

def digest(value):return sha256(c.canonical(value)).hexdigest()

def name(value):
    if type(value) is not str or re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,63}',value) is None:deny()
    return value


def validate_read_pin(value):
    c.exact(value,('version','material_sha256','contracts'))
    if type(value['version']) is not int or value['version']!=1:deny()
    c.fingerprint(value['material_sha256'])
    if type(value['contracts']) is not list or not 1<=len(value['contracts'])<=4:deny()
    seen=set()
    for item in value['contracts']:
        c.exact(item,('directory_name','manifest_sha256'));name(item['directory_name']);c.fingerprint(item['manifest_sha256'])
        if item['directory_name'] in seen:deny()
        seen.add(item['directory_name'])
    if value['contracts']!=sorted(value['contracts'],key=lambda x:x['directory_name']):deny()
    return c.decode(c.canonical(value),16384)


def private_root(path,*,create=False):
    path=Path(path)
    if create:
        try:
            os.mkdir(path,0o700)
            parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(parent)
            finally:os.close(parent)
        except FileExistsError:pass
    fd=catalog._open_root(str(path));info=os.fstat(fd)
    if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        os.close(fd);deny()
    return fd


def _private_read(fd,name_,expected,limit):
    before=os.stat(name_,dir_fd=fd,follow_symlinks=False)
    def check(info):
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1:deny()
    check(before)
    raw=catalog._read_exact(fd,name_,expected,limit)
    after=os.stat(name_,dir_fd=fd,follow_symlinks=False);check(after)
    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):deny()
    return raw


def _read(path,name_,expected,limit=65536):
    fd=private_root(path)
    try:return _private_read(fd,name_,expected,limit)
    finally:os.close(fd)


def _write(fd,name_,raw):
    out=None
    try:
        out=os.open(name_,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        info=os.fstat(out)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:deny()
        offset=0
        while offset<len(raw):
            n=os.write(out,raw[offset:])
            if n<=0:deny()
            offset+=n
        os.fsync(out);os.fsync(fd)
        after=os.stat(name_,dir_fd=fd,follow_symlinks=False)
        if (after.st_dev,after.st_ino,after.st_size,after.st_nlink,stat.S_IMODE(after.st_mode))!=(info.st_dev,info.st_ino,len(raw),1,0o600):deny()
    finally:
        if out is not None:os.close(out)


def _host_pin(selected):
    pin=selected.pin()
    if 'trial_name' in pin or 'read' not in pin:deny()
    return validate_read_pin(pin['read'])


def approved(selected,contract_id):
    """Selection only from this current Host + exact new-Run read pin."""
    frozen=_host_pin(selected);found=[];ids=set()
    for item in frozen['contracts']:
        obj=contract.ApprovedReadContract(str(selected.environment.root/item['directory_name']),item['manifest_sha256'])
        value=obj.load()
        if value['contract_id'] in ids:deny()
        ids.add(value['contract_id'])
        values=selected.environment.values
        if ((value['organization_id'],value['store_id'],value['owner_id'],value['agent_id'])!=tuple(values[k] for k in ('organization_id','store_id','requester_id','agent_id'))
            or value['corpus_digest']!=selected.manifest_sha256):deny()
        if value['contract_id']==contract_id:found.append((obj,value))
    if len(found)!=1:deny()
    return found[0]


def admitted_contracts(selected,candidate_id):
    """Safe public selectors; no source credentials or new authority are exposed."""
    rows=[]
    for item in _host_pin(selected)['contracts']:
        obj=contract.ApprovedReadContract(str(selected.environment.root/item['directory_name']),item['manifest_sha256'])
        v=obj.load();approved(selected,v['contract_id'])
        if contract.SPECS[v['operation']]['candidate']==candidate_id:
            rows.append(dict(contract_id=v['contract_id'],operation=v['operation'],fields=v['fields'],
                max_pages=v['max_pages'],max_rows=v['max_rows'],coverage='bounded_partial'))
    return rows


def _material(selected,value):
    pin=_host_pin(selected)
    raw=_read(selected.environment.root,'material.json',pin['material_sha256'],65536)
    material=work_source._material(raw)
    values=selected.environment.values
    if ((material['organization_id'],material['store_id'],material['agent_subject_id'],material['machine_token'])
        !=(values['organization_id'],values['store_id'],values['agent_id'],selected.environment.machine_token)
        or str(material['expected_tenant_id'])!=value['tenant_id'] or material['tenancy_name']!=value['tenancy']
        or datetime.fromisoformat(material['expires_at'])<=datetime.now(timezone.utc)):deny()
    return {k:material[k] for k in ('username','password','tenancy_name','expected_tenant_id','expires_at')}


def _permissions(caller,person,agent,value):
    broker._process_gate(caller)
    broker._business_authorize(caller,person,agent,value)


def _operation_root(selected,operation_id,*,create=False):
    c.fingerprint(operation_id)
    parent=selected.environment.root/'research-native-operations'
    fd=private_root(parent,create=create)
    try:
        if create:
            # Never adopt an empty/unknown prior operation, even before auth.
            os.mkdir(operation_id,0o700,dir_fd=fd);os.fsync(fd)
        child=parent/operation_id
        own=private_root(child)
        return child,own
    finally:os.close(fd)


def execute(caller,person,agent,selected,arguments,*,operation_id,context_digest,input_digest,host_gate):
    """Only called inside canonical _loaded, after the parent native intent.

    The supplied gate always opens and closes a fresh actual Host guard. This
    module adds independent business checks; a successful callback alone is not
    authorization. No catch/retry or replacement operation exists here.
    """
    from .research_probe_plan import revalidate
    c.fingerprint(operation_id);c.fingerprint(context_digest);c.fingerprint(input_digest)
    c.exact(arguments,('contract_id','proposal'))
    directory=fd=None
    obj,value=approved(selected,arguments['contract_id'])
    proposal=revalidate(caller,person,agent,selected.config(),arguments['proposal'])
    contract.validate_binding(value,proposal,selected.config(),person,agent)
    def gate(phase,page=0):
        if fd is not None:
            fresh=private_root(directory)
            try:
                a,b=os.fstat(fd),os.fstat(fresh)
                if (a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino):deny()
            finally:os.close(fresh)
        if host_gate(phase,page)!=context_digest:deny()
        selected.check(caller.get_bind())
        if approved(selected,value['contract_id'])[1]!=value:deny()
        _permissions(caller,person,agent,value)
        if host_gate(phase+'_host_checked',page)!=context_digest:deny()
        return context_digest
    gate('host_admission')
    material=_material(selected,value)
    deadline=time.monotonic()+10+value['timeout_seconds']
    directory,fd=_operation_root(selected,operation_id,create=True)
    try:
        intent=dict(version=1,operation_id=operation_id,context_digest=context_digest,input_digest=input_digest,
            contract_id=value['contract_id'],contract_sha256=obj.manifest_sha256,operation=value['operation'],
            material_sha256=_host_pin(selected)['material_sha256'],state='authentication_intent',observed_at=datetime.now(timezone.utc).isoformat())
        _write(fd,'authentication-intent.json',c.canonical(intent))
        gate('auth_intent_persisted')
        result=authentication.authenticate(material,deadline=deadline,host_gate=lambda phase:gate(phase))
        # Official frontend masks price/total when this flag is true. False is
        # not a grant: independently verified fields and current rights remain.
        if set(value['fields'])&{'price','total'} and result.visibility.get('isVisibleWholePrice') is not False:deny()
        gate('authentication_complete')
        _write(fd,'authentication-result.json',c.canonical(dict(state='authenticated',response_sha256=result.response_sha256,observed_at=datetime.now(timezone.utc).isoformat())))
        ledger=selected.environment.root/'research-read-ledger';lfd=private_root(ledger,create=True);os.close(lfd)
        prepared=broker.execute(caller,person,agent,selected.config(),proposal,approved_contract=obj,
            credentials=result.credentials,ledger_root=str(ledger),operation_id=operation_id,host_gate=gate,context_digest=context_digest)
        if (prepared.disclosure_ready or prepared.operation_id!=operation_id or prepared.context_digest!=context_digest
            or prepared.contract_sha256!=obj.manifest_sha256 or prepared.projection_sha256!=digest(prepared.projection)):deny()
        gate('prepared_before_native')
        wrapper=dict(version=1,authority='reference_only',operation='probe',corpus_digest=selected.manifest_sha256,
            metadata=dict(contract_id=value['contract_id'],contract_sha256=obj.manifest_sha256,domain=contract.SPECS[value['operation']]['domain'],
                read_operation=value['operation'],operation_id=operation_id,context_digest=context_digest,input_digest=input_digest,
                prepared_projection_sha256=prepared.projection_sha256,observed_at=datetime.now(timezone.utc).isoformat()),data=prepared.projection)
        raw=c.canonical(wrapper);c.decode(raw,65536)
        _write(fd,'result.json',raw)
        gate('wrapper_persisted')
        if _read(directory,'result.json',sha256(raw).hexdigest())!=raw:deny()
        return wrapper
    finally:os.close(fd)


def read_committed_metadata(selected,*,operation_id,context_digest,input_digest,output_digest,caller,person,agent):
    """Caller MUST first prove a successful same-Run native/Step/call binding.

    We only read its exact immutable wrapper and enforce the selected original
    contract's current permissions. This cannot promote orphan files to success,
    execute a request, re-run a G1 proposal or disclose old result bodies.
    """
    c.fingerprint(output_digest)
    path,fd=_operation_root(selected,operation_id)
    try:raw=_private_read(fd,'result.json',output_digest,65536)
    finally:os.close(fd)
    wrapper=c.decode(raw,65536)
    c.exact(wrapper,('version','authority','operation','corpus_digest','metadata','data'))
    meta=wrapper['metadata'];c.exact(meta,('contract_id','contract_sha256','domain','read_operation','operation_id','context_digest','input_digest','prepared_projection_sha256','observed_at'))
    obj,value=approved(selected,meta['contract_id'])
    if (type(wrapper['version']) is not int or wrapper['version']!=1 or wrapper['authority']!='reference_only' or wrapper['operation']!='probe'
        or wrapper['corpus_digest']!=selected.manifest_sha256
        or (meta['operation_id'],meta['context_digest'],meta['input_digest'])!=(operation_id,context_digest,input_digest)
        or meta['contract_sha256']!=obj.manifest_sha256 or meta['read_operation']!=value['operation']
        or meta['domain']!=contract.SPECS[value['operation']]['domain']
        or meta['prepared_projection_sha256']!=digest(wrapper['data'])):deny()
    c.instant(meta['observed_at']);_permissions(caller,person,agent,value)
    if _read(path,'result.json',output_digest)!=raw:deny()
    return dict(meta)


def required_permissions(selected):
    """Host admission/intake projection, never a grant or a model choice."""
    result=set()
    for item in _host_pin(selected)['contracts']:
        obj=contract.ApprovedReadContract(str(selected.environment.root/item['directory_name']),item['manifest_sha256'])
        value=obj.load();approved(selected,value['contract_id'])
        result.update(contract.permissions(value['operation']))
    return tuple(sorted(result))
