"""Host-only single authentication in fresh exec; not a tool or login manager.

The caller MUST durably consume the original native authentication intent before
calling authenticate. A timeout/exception never grants permission to authenticate
again. Credentials are sent only to the fixed vendor; never argv/env or files.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from . import live_once_transport as once
from .live_host import _authentication
from .work_source_process import _exchange
from .research_read_transport import Credentials

_ROOT = Path(__file__).resolve().parents[5]
_COMMAND = (sys.executable, '-I', '-B', '-c', 'import sys;sys.path.insert(0,'+repr(str(_ROOT))+');'
            'from backend.app.modules.integrations.guanjia.research_auth_transport import main;sys.exit(main())')
_FLAGS = ('isVisibleCostPrice','isVisibleWholePrice','isVisibleDeliveryPrice')

class AuthenticationUnknown(ValueError):
    def __init__(self):super().__init__('research_authentication_unknown_no_replay')

@dataclass(frozen=True,repr=False)
class AuthResult:
    credentials: Credentials = field(repr=False)
    visibility: dict = field(repr=False)
    response_sha256: str


def _strict(raw):
    def pairs(rows):
        value={}
        for key,item in rows:
            if key in value:raise ValueError()
            value[key]=item
        return value
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))


def _materials(value):
    if type(value) is not dict or set(value)!={'username','password','tenancy_name','expected_tenant_id','expires_at'}:raise ValueError()
    for key in ('username','password','tenancy_name'):
        if not once._text(value[key]):raise ValueError()
    if not value['tenancy_name'].isascii():raise ValueError()
    if type(value['expected_tenant_id']) is not int or not 0<value['expected_tenant_id']<=2**63-1:raise ValueError()
    expiry=datetime.fromisoformat(value['expires_at'])
    if expiry.tzinfo is None or expiry<=datetime.now(timezone.utc):raise ValueError()
    return expiry


def _request(packet):
    """Only the exec child invokes the established fixed HTTPS worker directly.

    No multiprocessing/fork occurs: its coordination values are child-local.
    Its bounded HTTP framing/TLS/parser are reused, not a new generic requester.
    """
    try:
        if type(packet) is not dict or set(packet)!={'materials','seconds'}:raise ValueError()
        material=packet['materials'];expiry=_materials(material);seconds=packet['seconds']
        if type(seconds) not in (int,float) or not 0<seconds<=10:raise ValueError()
        started=datetime.now(timezone.utc)
        body=json.dumps({'tenancyName':material['tenancy_name'],'userNameOrEmailAddress':material['username'],
                         'password':material['password'],'rememberClient':False},separators=(',',':')).encode()
        values=[SimpleNamespace(value=0),SimpleNamespace(value=True),SimpleNamespace(value=0),
                SimpleNamespace(value=0),SimpleNamespace(value=0)]
        buffer=bytearray(once.MAX_RESPONSE_BYTES)
        once._worker('/api/TokenAuth/Authenticate',body,{'Content-Type':'application/json','Accept':'application/json',
            'Accept-Encoding':'identity','Connection':'close','User-Agent':'Retail-V4-Research-Readonly'},
            time.monotonic()+seconds,*values,buffer)
        if values[3].value!=1 or not 0<values[4].value<=16384:raise ValueError()
        raw=bytes(buffer[:values[4].value]);token=_authentication(raw,material['expected_tenant_id'])
        response=_strict(raw)['result'];ttl=response.get('expireInSeconds')
        if type(ttl) is not int or not 0<ttl<=2**31-1:raise ValueError()
        until=min(expiry,started+timedelta(seconds=ttl))
        if until<=datetime.now(timezone.utc):raise ValueError()
        return {'status':'authenticated','token':token,'tenant_id':str(material['expected_tenant_id']),
                'tenancy':material['tenancy_name'],'expires_at':until.isoformat(),
                'visibility':{key:response.get(key) for key in _FLAGS},'response_sha256':sha256(raw).hexdigest()}
    except BaseException:
        return {'status':'unknown'}


def authenticate(material,*,deadline,host_gate):
    """Requires a consumed Host intent; returned token stays in this worker only."""
    process=None
    try:
        from backend.app.modules.agents import research_process
        if (research_process._IN_WORKER is not True or threading.current_thread() is not threading.main_thread()
                or threading.active_count()!=1 or not callable(host_gate)):raise ValueError()
        expiry=_materials(material)
        seconds=min(10,deadline-time.monotonic())
        if seconds<=0:raise ValueError()
        host_gate('before_authentication')
        packet=json.dumps({'materials':material,'seconds':seconds},separators=(',',':'),allow_nan=False).encode()
        if len(packet)>16384:raise ValueError()
        process=subprocess.Popen(_COMMAND,cwd=_ROOT,env={'PYTHONDONTWRITEBYTECODE':'1'},
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True,start_new_session=False)
        raw,status=_exchange(process,packet,min(deadline,time.monotonic()+seconds))
        host_gate('after_authentication')
        result=_strict(raw)
        if status!=0 or type(result) is not dict or set(result)!={'status','token','tenant_id','tenancy','expires_at','visibility','response_sha256'}:raise ValueError()
        if result['status']!='authenticated' or result['tenant_id']!=str(material['expected_tenant_id']) or result['tenancy']!=material['tenancy_name']:raise ValueError()
        until=datetime.fromisoformat(result['expires_at'])
        if until.tzinfo is None or not datetime.now(timezone.utc)<until<=expiry:raise ValueError()
        if (type(result['visibility']) is not dict or set(result['visibility'])!=set(_FLAGS)
                or any(type(v) is not bool and v is not None for v in result['visibility'].values())):raise ValueError()
        digest=result['response_sha256']
        if type(digest) is not str or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise ValueError()
        credentials=Credentials(result['token'],result['tenant_id'],result['tenancy'],until)
        credentials.validate({'tenant_id':str(material['expected_tenant_id']),'tenancy':material['tenancy_name']})
        return AuthResult(credentials,result['visibility'],digest)
    except BaseException:
        raise AuthenticationUnknown() from None
    finally:
        if process is not None:
            try:
                if process.poll() is None:process.kill()
                process.wait(timeout=5)
                for stream in (process.stdin,process.stdout):
                    if stream is not None:stream.close()
            except BaseException:
                raise AuthenticationUnknown() from None


def main():
    try:
        raw=sys.stdin.buffer.read(16385)
        if len(sys.argv)!=1 or not 0<len(raw)<=16384:raise ValueError()
        result=_request(_strict(raw))
    except BaseException:result={'status':'unknown'}
    print(json.dumps(result,separators=(',',':'),allow_nan=False))
    return 0 if result['status']=='authenticated' else 6

if __name__=='__main__':raise SystemExit(main())
