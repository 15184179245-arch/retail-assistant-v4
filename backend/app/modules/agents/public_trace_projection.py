"""Bounded, non-authorizing display projection for PUBLIC-TRACE-B0.

No raw transcript, free text, credentials, persistence, or permission checks.
The eventual Host caller must bind authoritative receipts and freshly authorize
every source before capture/read. Matching dictionaries are NOT authorization.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
from uuid import UUID

ABSENT = object()
MAX_RAW_BYTES = 262144
MAX_PROJECTED_BYTES = 32768
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_NATIVE = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
TEXT = ("withhold", "free_text_not_captured")
UNKNOWN = ("withhold", "unreviewed_structure")
SHA = ("sha",)
ID = ("identifier",)
UID = ("uuid",)
BOOL = ("bool",)
NATIVE_NAMES = frozenset({
    "tool_search", "v4_memory_search_v1", "v4_tasks_list_mine_v1",
    "v4_knowledge_search_v1", "v4_catalog_search_v1",
    "v4_memory_save_v1", "v4_memory_correct_v1", "v4_memory_withdraw_v1",
    "v4_guanjia_read_prices_v1", "v4_guanjia_read_stock_v1",
    "v4_guanjia_category_discovery_v1", "v4_guanjia_category_products_v1",
    "v4_guanjia_category_products_status_v1",
    "v4_research_search_v1", "v4_research_read_v1",
    "v4_research_describe_v1", "v4_research_propose_v1",
})
def enum(*values): return ("enum", frozenset(values))
def integer(low, high): return ("int", low, high)
def array(rule, high): return ("array", rule, high)
def nullable(rule): return ("nullable", rule)

MEMORY_ITEM = {"id": UID, "version": integer(1, 2**31-1), "active": BOOL,
    "retain_until_withdrawn": BOOL, "content": TEXT, "purpose": TEXT, "source": TEXT}
RESEARCH_BASE = {"version": enum(1), "authority": enum("reference_only"),
    "operation": enum("search", "read", "describe", "propose"), "corpus_digest": SHA}
RESEARCH_DATA = {
 "search": {"version": enum(1), "authority": enum("reference_only"),
    "corpus_digest": SHA, "revision": integer(1, 2**31-1),
    "items": array({"artifact_id": ID, "title": TEXT, "excerpt": TEXT,
        "evidence": UNKNOWN}, 20),
    "total_matches": integer(0, 2**31-1), "offset": integer(0, 2**31-1),
    "next_cursor": ("withhold", "opaque_cursor_not_captured"), "truncated": BOOL,
    "coverage": enum("matched_page_in_this_corpus_only")},
 "read": {"version": enum(1), "authority": enum("reference_only"),
    "text": TEXT, "evidence": UNKNOWN, "truncated": BOOL,
    "next_start": nullable(integer(0, 65536)), "candidate_ids": array(ID, 100)},
 "describe": {"version": enum(1), "authority": enum("reference_only"),
    "corpus_digest": SHA, "candidate_digest": SHA, "candidate": UNKNOWN,
    "request_schema_digest": SHA, "result_schema_digest": SHA,
    "permissions_authority": enum("document_claim_only"), "evidence": UNKNOWN},
 "propose": {"version": enum(1), "authority": enum("proposal_only"),
    "status": enum("not_executed"), "candidate_digest": SHA,
    "corpus_digest": SHA, "goal": TEXT, "normalized_parameters": UNKNOWN,
    "required_permissions": UNKNOWN, "verification_plan": UNKNOWN, "unknowns": UNKNOWN},
}
ARG_RULES = {
 "runtime.tool_search": {"query": TEXT, "limit": integer(1,100)},
 "memory.search": {"query": TEXT, "limit": integer(1,5)},
 "research.search": {"query": TEXT, "limit": integer(1,20),
    "cursor": ("withhold", "opaque_cursor_not_captured")},
 "research.read": {"artifact_id": ID, "corpus_digest": SHA,
    "start": integer(0,65536), "limit": integer(1,4096)},
 "research.describe": {"candidate_id": ID, "corpus_digest": SHA},
 "research.propose": {"candidate_id": ID, "corpus_digest": SHA,
    "goal": TEXT, "parameters": UNKNOWN},
}
RESULT_RULES = {
 "runtime.tool_search": {"authority": enum("metadata_only"),
    "loaded": array(("enum",NATIVE_NAMES),100), "catalog_digest": ("prefixed_sha",)},
 "memory.search": {"items": array(MEMORY_ITEM,5),
    "match_mode": enum("all_terms","any_terms","none"), "authority": enum("reference_only")},
 **{"research."+op: {**RESEARCH_BASE, "operation": enum(op), "data": data}
    for op,data in RESEARCH_DATA.items()},
}
# Exact reviewed Pydantic schemas; drift fails closed even when both bindings agree.
SCHEMA_PINS = {'runtime.tool_search': ('d95ef5564ccc67f83884c97765ac146ee4a1a0b7faf4a7f08e66af513b3b86e6', '178b2797956d5cd84637239b9ccccb53a883fa8c615f85aec5e52cd9cb789e0c'), 'memory.search': ('0aa302e63f58b49a1e6f5136c52718d357e4a59914dbd2e50738e4ffdb7995b0', 'cdb00b817a89c7603b2104b0e9bffa29132e7d2666077a82a8acfcd65cbee8c5'), 'research.search': ('342df1e22d1be98ddaed59d465c8a95a4a47654d65cfec38884e6c16ce40d45c', 'af4413b44c2b1968b923b71b0da287e46cf1a4238e67addbec279ac4b5a07c58'), 'research.read': ('41f22cf0b4f3f2ed46f0998cede81a6cc6317e44acc99b6b10dcd2387a599e49', 'af4413b44c2b1968b923b71b0da287e46cf1a4238e67addbec279ac4b5a07c58'), 'research.describe': ('ef059a398aa707e22923bba98568d2bfb694268dde3728c803319b68d1f41889', 'af4413b44c2b1968b923b71b0da287e46cf1a4238e67addbec279ac4b5a07c58'), 'research.propose': ('e9fd9b4a3ed0f2229e33efa7d2e42e68ef731465f7c0ac3719356cf013dd02d7', 'af4413b44c2b1968b923b71b0da287e46cf1a4238e67addbec279ac4b5a07c58')}
_BINDING_KEYS = frozenset({
 "organization_id","store_id","requester_id","run_id","model_step_id",
 "tool_step_id","receipt_id","native_call_id","tool_key","tool_version",
 "input_schema_sha256","output_schema_sha256","input_sha256","output_sha256",
})

class PublicTraceError(ValueError):
    """Fixed error codes only: never include rejected values."""
def _deny():
    raise PublicTraceError("public_trace_invalid") from None

def _json_shape(value, depth=0, budget=None):
    if budget is None: budget=[10000]
    budget[0]-=1
    if budget[0]<0 or depth>12: _deny()
    t=type(value)
    if value is None or t is bool: return
    if t is int:
        if abs(value)>2**53-1: _deny()
    elif t is float:
        if not math.isfinite(value): _deny()
    elif t is str:
        if len(value)>MAX_RAW_BYTES or any(0xD800<=ord(c)<=0xDFFF for c in value): _deny()
    elif t is list:
        for item in value: _json_shape(item,depth+1,budget)
    elif t is dict:
        for key,item in value.items():
            if type(key) is not str: _deny()
            _json_shape(key,depth+1,budget);_json_shape(item,depth+1,budget)
    else: _deny()

def canonical(value):
    _json_shape(value)
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    if len(raw)>MAX_RAW_BYTES: _deny()
    return raw

def digest(value):
    return sha256(canonical(value)).hexdigest()

def _uuid(value):
    if type(value) is not str: return False
    try: return str(UUID(value))==value
    except (ValueError,AttributeError): return False

def _binding(value):
    if type(value) is not dict or set(value)!=_BINDING_KEYS: _deny()
    for name in ("organization_id","store_id","requester_id"):
        if type(value[name]) is not str or not _OPAQUE.fullmatch(value[name]) or len(value[name])>36: _deny()
    for name in ("run_id","model_step_id","receipt_id"):
        if not _uuid(value[name]): _deny()
    if value["tool_step_id"] is not None and not _uuid(value["tool_step_id"]): _deny()
    if type(value["native_call_id"]) is not str or not _NATIVE.fullmatch(value["native_call_id"]): _deny()
    for name in ("input_schema_sha256","output_schema_sha256","input_sha256"):
        if type(value[name]) is not str or not _HEX.fullmatch(value[name]): _deny()
    if value["output_sha256"] is not None and (type(value["output_sha256"]) is not str or not _HEX.fullmatch(value["output_sha256"])): _deny()
    if type(value["tool_key"]) is not str or not 1<=len(value["tool_key"])<=120: _deny()
    if type(value["tool_version"]) is not str or not 1<=len(value["tool_version"])<=120: _deny()
    canonical(value)

def _scalar(rule,value):
    kind=rule[0]
    if kind=="nullable":
        return value is None or _scalar(rule[1],value)
    if kind=="int": return type(value) is int and rule[1]<=value<=rule[2]
    if kind=="bool": return type(value) is bool
    if kind=="enum": return any(type(value) is type(x) and value==x for x in rule[1])
    if kind=="uuid": return _uuid(value)
    if kind=="identifier":
        return (type(value) is str and _OPAQUE.fullmatch(value) is not None
            and not value.isdigit()
            and re.search(r"(?i)(?:sk-|bearer|password|secret|api[_-]?key|token)",value) is None)
    if kind=="sha": return type(value) is str and _HEX.fullmatch(value) is not None
    if kind=="prefixed_sha": return type(value) is str and value.startswith("sha256:") and _HEX.fullmatch(value[7:]) is not None
    _deny()

def _field(rule,value):
    if value is ABSENT: return {"state":"absent"}
    if type(rule) is dict:
        if type(value) is not dict: return {"state":"withheld","reason":"invalid_field"}
        return {"state":"recorded","fields":{key:_field(child,value.get(key,ABSENT))
            for key,child in rule.items()}, "unlisted_fields":"withheld"}
    if rule[0]=="withhold": return {"state":"withheld","reason":rule[1]}
    if rule[0]=="array":
        if type(value) is not list or len(value)>rule[2]:
            return {"state":"withheld","reason":"invalid_field"}
        return {"state":"recorded","items":[_field(rule[1],x) for x in value]}
    if not _scalar(rule,value): return {"state":"withheld","reason":"invalid_field"}
    return {"state":"recorded","value":value}

def project_trace(*, binding, expected_binding, phase, captured_at,
                  arguments=ABSENT, result=ABSENT):
    """Project original JSON already validated/bound by the Host.

    expected_binding is an independent trusted caller fact, not a model field.
    B0 compares facts, but DOES NOT validate DB ownership, permissions, or ACLs.
    """
    _binding(binding);_binding(expected_binding)
    if binding!=expected_binding: _deny()
    if type(phase) is not str or phase not in ("accepted","result"): _deny()
    if type(captured_at) is not str: _deny()
    try:
        stamp=datetime.fromisoformat(captured_at.replace("Z","+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds()!=0: _deny()
    except (ValueError,AttributeError,OverflowError): _deny()
    if arguments is not ABSENT:
        if type(arguments) is not dict or digest(arguments)!=binding["input_sha256"]: _deny()
    if phase=="accepted":
        if result is not ABSENT or binding["output_sha256"] is not None or binding["tool_step_id"] is not None: _deny()
    else:
        if result is ABSENT or binding["tool_step_id"] is None or type(result) is not dict: _deny()
        if digest(result)!=binding["output_sha256"]: _deny()
    supported=(binding["tool_version"]=="1" and binding["tool_key"] in ARG_RULES
        and SCHEMA_PINS.get(binding["tool_key"]) ==
            (binding["input_schema_sha256"],binding["output_schema_sha256"]))
    key=binding["tool_key"] if supported else None
    def body(value,rules):
        if value is ABSENT: return {"state":"not_recorded"}
        if not supported: return {"state":"withheld","reason":"unsupported_tool_schema"}
        return _field(rules,value)
    out={"version":1,"authority":"projection_only","authorization_checked":False,
        "binding_sha256":digest(binding),
        "run_id":binding["run_id"],"model_step_id":binding["model_step_id"],
        "tool_step_id":binding["tool_step_id"],"receipt_id":binding["receipt_id"],
        "native_call_sha256":sha256(binding["native_call_id"].encode()).hexdigest(),
        "tool_key":key,"tool_version":"1" if supported else None,
        "projection_schema":key+"/public-trace/1" if supported else None,
        "input_schema_sha256":binding["input_schema_sha256"],
        "output_schema_sha256":binding["output_schema_sha256"],
        "input_sha256":binding["input_sha256"],"output_sha256":binding["output_sha256"],
        "phase":phase,"captured_at":stamp.astimezone(timezone.utc).isoformat(),
        "time_semantics":"record_capture_not_execution_duration",
        "arguments":body(arguments,ARG_RULES.get(key,{})),
        "result":body(result,RESULT_RULES.get(key,{})),
        "public_plan":{"state":"not_recorded"},
        "source_authorization":"not_checked",
        "body_is_original_stream":False}
    if len(canonical(out))>MAX_PROJECTED_BYTES: _deny()
    return out
