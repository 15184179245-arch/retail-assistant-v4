"""Strict result-ABI foundation; not a writer, permission grant or replay loop.

B must bind this codec to the actual result root and current per-item authority.
A recovery classification here never means that a body is currently available.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from hashlib import sha256
import json
import re
from types import MappingProxyType

RESULT_ABI = 'pi-tool-result/1'
RESULT_WIRE = 'v4.pi-readonly/3'
RESULT_PROFILES = frozenset('host-pi-readonly-node24-'+p+'-online-stream-result-v1' for p in ('darwin','linux'))
MAX_BYTES = 262144
MAX_DEPTH = 32
SAFE_INT = 9007199254740991
ERROR_PAYLOAD = MappingProxyType({
    'code': 'memory_query_invalid', 'field': 'query',
    'hint': '请使用1至256个有效字符和不超过16个查询词，移除空字符后重试。',
})
TOOLS = {'tasks.list_mine':'v4_tasks_list_mine_v1','knowledge.search':'v4_knowledge_search_v1',
         'catalog.search':'v4_catalog_search_v1','memory.search':'v4_memory_search_v1'}
_ENVELOPE = {'record_version','result_abi','runtime_record','scope','binding','result_kind','payload',
             'payload_sha256','output_ref','source_refs','source_refs_sha256','captured_at'}
_RUNTIME = {'contract_version','wire_protocol','launch_profile','source_manifest_sha256'}
_SCOPE = {'organization_id','store_id','requester_id','agent_id'}
_BINDING = {'run_id','model_step_id','tool_step_id','pi_receipt_id','native_call_id','tool_key','tool_name',
            'tool_version','toolset_version','input_digest','execution_epoch'}


class CanonicalResultError(ValueError):
    """Finite code only, never payload or exception diagnostics."""


def _fail(code):
    raise CanonicalResultError(code)


def _shape(value, depth=0):
    if type(value) in (dict,list):
        if depth+1>MAX_DEPTH:_fail('canonical_depth')
        if type(value)is dict:
            if any(type(key)is not str for key in value):_fail('canonical_key')
            children=[*value.keys(),*value.values()]
        else:children=value
        for child in children:_shape(child,depth+1)
    elif type(value)is str:
        if any(0xD800<=ord(c)<=0xDFFF for c in value):_fail('canonical_unicode')
    elif type(value)is int:
        if not -SAFE_INT<=value<=SAFE_INT:_fail('canonical_integer')
    elif value is not None and type(value)is not bool:
        _fail('canonical_json_type')


def canonical_bytes(value):
    _shape(value)
    encoded=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
    if not encoded or len(encoded)>MAX_BYTES:_fail('canonical_bytes')
    return encoded


def _digest(value):
    return sha256(canonical_bytes(value)).hexdigest()


def _exact(value,keys):
    if type(value)is not dict or set(value)!=keys:_fail('canonical_fields')


def _identifier(value,maximum=36):
    if type(value)is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,'+str(maximum)+'}',value):
        _fail('canonical_identifier')


def _hex(value,prefixed=False):
    if type(value)is not str or not re.fullmatch(('sha256:' if prefixed else '')+r'[0-9a-f]{64}',value):
        _fail('canonical_digest')


def _timestamp(value):
    if (type(value)is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)',value)):
        _fail('canonical_timestamp')
    try:parsed=datetime.fromisoformat(value)
    except ValueError:_fail('canonical_timestamp')
    if parsed.utcoffset()!=timedelta(0):_fail('canonical_timestamp')
    return parsed


def _validated_bytes(value):
    _shape(value)
    _exact(value,_ENVELOPE)
    if type(value['record_version'])is not int or value['record_version']!=1:_fail('canonical_version')
    if value['result_abi']!=RESULT_ABI:_fail('canonical_abi')
    record=value['runtime_record'];_exact(record,_RUNTIME)
    if (type(record['contract_version'])is not int or record['contract_version']!=3
            or record['wire_protocol']!=RESULT_WIRE or type(record['launch_profile'])is not str or record['launch_profile']not in RESULT_PROFILES):
        _fail('canonical_profile')
    _hex(record['source_manifest_sha256'])
    scope=value['scope'];_exact(scope,_SCOPE)
    for item in scope.values():_identifier(item)
    binding=value['binding'];_exact(binding,_BINDING)
    for key in ('run_id','model_step_id','tool_step_id','pi_receipt_id'):_identifier(binding[key])
    if type(binding['native_call_id'])is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',binding['native_call_id']):
        _fail('canonical_native_id')
    if (type(binding['tool_key'])is not str or binding['tool_key']not in TOOLS
            or binding['tool_name']!=TOOLS[binding['tool_key']] or binding['tool_version']!='1'):
        _fail('canonical_tool')
    if type(binding['execution_epoch'])is not int or not 1<=binding['execution_epoch']<=SAFE_INT:
        _fail('canonical_epoch')
    _hex(binding['input_digest']);_hex(binding['toolset_version'],True)
    if value['result_kind']not in ('success','tool_error'):_fail('canonical_kind')
    if type(value['payload'])is not dict:_fail('canonical_payload')
    if value['result_kind']=='tool_error':
        if binding['tool_key']!='memory.search' or value['payload']!=ERROR_PAYLOAD or value['source_refs']!=[]:
            _fail('canonical_error_payload')
    if type(value['source_refs'])is not list or any(type(item)is not dict for item in value['source_refs']):
        _fail('canonical_source_refs')
    for key in ('payload_sha256','source_refs_sha256'):_hex(value[key])
    _hex(value['output_ref'],True);_timestamp(value['captured_at'])
    if value['payload_sha256']!=_digest(value['payload']) or value['output_ref']!='sha256:'+value['payload_sha256']:
        _fail('canonical_payload_digest')
    if value['source_refs_sha256']!=_digest(value['source_refs']):_fail('canonical_refs_digest')
    return canonical_bytes(value)


def validate_envelope(value):
    """Validate format/commitments only; does not prove DB root or source ACL."""
    _validated_bytes(value)
    return value


def make_envelope(*,runtime_record,scope,binding,result_kind,payload,source_refs,captured_at):
    value={'record_version':1,'result_abi':RESULT_ABI,'runtime_record':runtime_record,'scope':scope,
        'binding':binding,'result_kind':result_kind,'payload':payload,'payload_sha256':_digest(payload),
        'output_ref':'sha256:'+_digest(payload),'source_refs':source_refs,'source_refs_sha256':_digest(source_refs),
        'captured_at':captured_at}
    validate_envelope(value)
    # Never retain mutable caller objects as a canonical fact.
    return json.loads(canonical_bytes(value))


def _reference_bytes(encoded):
    return 'sha256:'+sha256(encoded).hexdigest()


def canonical_reference(value):
    return _reference_bytes(_validated_bytes(value))


def dump_envelope(value):
    return _validated_bytes(value).decode('utf8')


def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:_fail('canonical_duplicate_key')
        result[key]=value
    return result


def _non_integer(_):
    _fail('canonical_json_type')


def _load_envelope(text,expected_ref):
    if type(text)is not str:_fail('canonical_text')
    try:raw=text.encode('utf8')
    except UnicodeError:_fail('canonical_unicode')
    if not 1<=len(raw)<=MAX_BYTES:_fail('canonical_bytes')
    # Bound containers before the standard parser allocates deep objects.
    depth=0;inside=False;escaped=False
    for char in text:
        if inside:
            if escaped:escaped=False
            elif char=='\\':escaped=True
            elif char=='"':inside=False
        elif char=='"':inside=True
        elif char in '[{':
            depth+=1
            if depth>MAX_DEPTH:_fail('canonical_depth')
        elif char in ']}':depth-=1
    try:value=json.loads(text,object_pairs_hook=_pairs,parse_float=_non_integer,parse_constant=_non_integer)
    except (ValueError,RecursionError) as error:
        if isinstance(error,CanonicalResultError):raise
        _fail('canonical_json')
    encoded=_validated_bytes(value)
    if encoded!=raw:_fail('canonical_encoding')
    if expected_ref is not None:
        _hex(expected_ref,True)
        if _reference_bytes(encoded)!=expected_ref:_fail('canonical_reference')
    return value,encoded


def load_envelope(text,*,expected_ref=None):
    value,_=_load_envelope(text,expected_ref)
    return value


def columns_for_envelope(value):
    """Convenience values, not persistence/authorization; B validates one row."""
    return _columns(value,_validated_bytes(value))


def _columns(value,encoded):
    binding=value['binding']
    return {**value['scope'],**{key:binding[key] for key in ('run_id','model_step_id','tool_step_id',
        'pi_receipt_id','native_call_id','tool_key','tool_name','tool_version','toolset_version','input_digest','execution_epoch')},
        'model_kind':'model','tool_kind':'tool','record_version':1,'result_abi':RESULT_ABI,
        'runtime_record_sha256':_digest(value['runtime_record']),'result_kind':value['result_kind'],
        'output_ref':value['output_ref'],'payload_sha256':value['payload_sha256'],
        'source_refs_sha256':value['source_refs_sha256'],'canonical_ref':_reference_bytes(encoded),
        'canonical_json':encoded.decode('utf8'),'created_at':_timestamp(value['captured_at'])}


def validate_columns(row):
    get=(lambda key:row[key]) if isinstance(row,Mapping) else (lambda key:getattr(row,key))
    try:
        value,encoded=_load_envelope(get('canonical_json'),get('canonical_ref'))
        expected=_columns(value,encoded)
        for key,item in expected.items():
            actual=get(key)
            if type(item)in(str,int) and type(actual)is not type(item):_fail('canonical_column_binding')
            if isinstance(item,datetime) and (not isinstance(actual,datetime) or actual.utcoffset()!=timedelta(0)):_fail('canonical_column_binding')
            if actual!=item:_fail('canonical_column_binding')
    except (KeyError,AttributeError):_fail('canonical_column_binding')
    return value


def recovery_requirements(value):
    """Structural prerequisites only. Never returns available/authorized."""
    validate_envelope(value)
    if value['result_kind']=='tool_error':return 'safe_error_requires_current_tool_auth'
    key=value['binding']['tool_key'];payload=value['payload']
    if key=='tasks.list_mine':return 'withheld_no_reliable_source_refs'
    if key in ('knowledge.search','catalog.search'):
        if key=='knowledge.search':entries=payload.get('citations')
        else:
            exact,candidates=payload.get('exact'),payload.get('candidates')
            entries=(exact+candidates) if type(exact)is list and type(candidates)is list else None
        if not entries:return 'withheld_no_reliable_source_refs'
        return 'withheld_source_validator_not_enabled'
    items=payload.get('items')
    if not isinstance(items,list) or not items:return 'withheld_no_reliable_source_refs'
    references={}
    for ref in value['source_refs']:
        if ref.get('kind')=='memory':
            identity=ref.get('id');version=ref.get('version')
            if type(identity)is not str or type(version)is not int or version<=0 or identity in references:
                return 'withheld_source_refs_incomplete'
            references[identity]=version
    seen=set()
    for item in items:
        if (type(item)is not dict or type(item.get('id'))is not str or type(item.get('version'))is not int
                or item['version']<=0 or type(item.get('content'))is not str
                or references.get(item['id'])!=item['version'] or item['id']in seen):
            return 'withheld_source_refs_incomplete'
        seen.add(item['id'])
    if seen!=set(references):return 'withheld_source_refs_incomplete'
    return 'source_validation_required'
