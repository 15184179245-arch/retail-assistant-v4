"""Fail-closed offline SSE normalization and private public-schema observations.

Digests are lowercase SHA-256 hex. Message canonical bytes are UTF-8 JSON,
ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False.
Missing optional cache counters normalize like the SDK for content comparison,
but usage_presence preserves their absence and billing_complete remains False.
Consumers must not settle billing using incomplete observations. This module
does not establish provenance, authorization, or real Go support. Public-schema
billing observations are private, exact decimal strings and never enable billing
completion in this offline-only branch.
"""
import hashlib
import json
import re
from decimal import Decimal, DecimalException

MODEL = 'deepseek-v4.1-flash'
MAX_BYTES = 128 * 1024


class PiOnlineResponseError(ValueError):
    """Stable error: never includes provider content or validation details."""


class PiOnlineResponseTruncated(PiOnlineResponseError):
    """Complete response and usage, but no complete model decision."""


def _fail():
    raise PiOnlineResponseError('pi_online_response_invalid')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _json(text):
    return json.loads(text, object_pairs_hook=_object, parse_constant=lambda _: _fail())


def _shape(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        _fail()


def _integer(value):
    if type(value) is not int or not 0 <= value <= 2**31-1:
        _fail()
    return value


def _text(value):
    if type(value) is not str:
        _fail()
    value.encode('utf-8')  # Reject JSON surrogate escapes, never silently repair.
    return value


def canonical_message_bytes(message):
    """Shared digest definition; caller must separately validate the message."""
    return json.dumps(message, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _amount(value):
    # Decimal values come from a second parse of the private usage subtree.
    # Strings preserve the JSON decimal exactly without leaking into SDK messages.
    if type(value) not in (int, Decimal) or not Decimal(value).is_finite() or value < 0:
        _fail()
    return str(value)


def _billing(value, usage, presence):
    if 'opencode' not in value:
        if 'cost' in value:
            _fail()
        return None
    if 'cost' not in value:
        _fail()
    source = value['opencode']
    _shape(source, {'billing_source', 'list_cost', 'tokens'}, {'allowance', 'estimated'})
    kind = source['billing_source']
    if type(kind) is not str or kind not in ('free', 'balance', 'go', 'go-plus', 'byok'):
        _fail()
    cost, listed = _amount(value['cost']), _amount(source['list_cost'])
    if 'estimated' in source and (kind != 'byok' or source['estimated'] is not True):
        _fail()
    if 'allowance' in source and kind not in ('go', 'go-plus'):
        _fail()
    allowance = _amount(source['allowance']) if 'allowance' in source else None
    if kind == 'free' and Decimal(cost) != 0:
        _fail()
    tokens = source['tokens']
    _shape(tokens, {'input', 'output', 'reasoning', 'cache_read', 'cache_write_5m', 'cache_write_1h'})
    tokens = {key: _integer(number) for key, number in tokens.items()}
    if ('completion_tokens_details' in value
            and value['completion_tokens_details']['reasoning_tokens'] != tokens['reasoning']):
        _fail()
    if tokens['reasoning'] > tokens['output'] or tokens['output'] != usage['output']:
        _fail()
    # Do not infer SDK cache counters from billing counters. The public example
    # omits those SDK fields, and its prompt counter is not a cache-inclusive sum.
    if presence['cached_tokens'] and tokens['cache_read'] != usage['cacheRead']:
        _fail()
    if presence['cache_write_tokens'] and tokens['cache_write_5m'] + tokens['cache_write_1h'] != usage['cacheWrite']:
        _fail()
    if tokens['input'] != usage['input']:
        _fail()
    return {'billing_source': kind, 'cost': cost, 'list_cost': listed,
            'allowance': allowance, 'estimated': source.get('estimated', False),
            'cost_kind': 'provider_report_or_estimate' if kind == 'byok' else 'reported_balance_charge',
            'tokens': tokens}


def _usage(value):
    _shape(value, {'prompt_tokens', 'completion_tokens'}, {'prompt_tokens_details', 'completion_tokens_details', 'total_tokens', 'cost', 'opencode'})
    prompt, output = _integer(value['prompt_tokens']), _integer(value['completion_tokens'])
    if 'completion_tokens_details' in value:
        if 'opencode' not in value:
            _fail()
        completion = value['completion_tokens_details']
        zeros = {'audio_tokens', 'accepted_prediction_tokens', 'rejected_prediction_tokens'}
        _shape(completion, {'reasoning_tokens'}, zeros)
        for key in zeros & completion.keys():
            if _integer(completion[key]) != 0:
                _fail()
        if _integer(value['completion_tokens_details']['reasoning_tokens']) > output:
            _fail()
    # Absence of optional cache counters follows the SDK's documented zero
    # defaults; prompt/completion observations themselves are never defaulted.
    details = value.get('prompt_tokens_details', {})
    zeros = {'audio_tokens', 'cache_creation_input_tokens'} if 'opencode' in value else set()
    _shape(details, set(), {'cached_tokens', 'cache_write_tokens'} | zeros)
    for key in zeros & details.keys():
        if _integer(details[key]) != 0:
            _fail()
    read = _integer(details.get('cached_tokens', 0))
    raw_write = details.get('cache_write_tokens', 0)
    # A nullable standard field is absent evidence, not an observed zero.
    # Only the complete, separately checked Go basis can release accounting.
    if raw_write is None and 'opencode' in value:
        raw_write = 0
    write = _integer(raw_write)
    if write or read > prompt or ('total_tokens' in value and _integer(value['total_tokens']) != prompt + output):
        _fail()
    presence = {
        'prompt_tokens': True, 'completion_tokens': True,
        'cached_tokens': 'cached_tokens' in details,
        'cache_write_tokens': 'cache_write_tokens' in details and details['cache_write_tokens'] is not None,
    }
    return ({'input': prompt-read, 'output': output, 'cacheRead': read, 'cacheWrite': write}, presence)



def validate_go_usage_basis(value):
    """Strict private persistence shape; no text, keys, URLs or inferred counters."""
    _shape(value, {'basis_digest', 'version', 'kind', 'model', 'response_digest', 'prompt_tokens',
        'completion_tokens', 'total_tokens', 'reasoning_tokens', 'cache_read',
        'cache_write_5m', 'cache_write_1h', 'billing_source', 'cost', 'list_cost', 'allowance'})
    if (type(value['version']) is not int
            or (value['version'], value['kind']) not in ((1, 'go-zero-cache-v1'), (2, 'go-cache-read-v2'))
            or value['model'] != MODEL
            or value['billing_source'] != 'go'
            or type(value['response_digest']) is not str
            or not re.fullmatch(r'[a-f0-9]{64}', value['response_digest'])):
        _fail()
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'reasoning_tokens',
                'cache_read', 'cache_write_5m', 'cache_write_1h'):
        _integer(value[key])
    if (value['total_tokens'] != value['prompt_tokens'] + value['completion_tokens']
            or value['reasoning_tokens'] > value['completion_tokens']
            or value['cache_read'] > value['prompt_tokens']
            or (value['version'] == 1 and value['cache_read'] != 0)
            or (value['version'] == 2 and value['cache_read'] == 0)
            or any(value[key] != 0 for key in ('cache_write_5m', 'cache_write_1h'))):
        _fail()
    for key in ('cost', 'list_cost', 'allowance'):
        amount = value[key]
        if (type(amount) is not str or len(amount) > 128
                or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', amount)
                or not Decimal(amount).is_finite() or Decimal(amount) < 0):
            _fail()
    expected = hashlib.sha256(canonical_message_bytes({key:item for key,item in value.items() if key != 'basis_digest'})).hexdigest()
    if value['basis_digest'] != expected:
        _fail()
    return value


def _usage_observation(value, response_digest):
    usage, presence = _usage(value)
    billing = _billing(value, usage, presence)
    basis = None
    # Explicit Go write counters establish the missing standard write dimension.
    # Cache read must agree with the standard field; it is already subtracted
    # from prompt by _usage and cross-checked against Go input by _billing.
    if billing is not None and billing['billing_source'] == 'go':
        details = value.get('completion_tokens_details')
        tokens = billing['tokens']
        if (details is not None and presence['cached_tokens']
                and all(tokens[key] == 0 for key in ('cache_write_5m', 'cache_write_1h'))
                and usage['cacheWrite'] == 0
                and billing['allowance'] is not None and 'total_tokens' in value):
            if details['reasoning_tokens'] != tokens['reasoning']:
                _fail()
            cached = usage['cacheRead'] > 0
            basis = {'version':2 if cached else 1, 'kind':'go-cache-read-v2' if cached else 'go-zero-cache-v1', 'model':MODEL,
                'response_digest':response_digest, 'prompt_tokens':value['prompt_tokens'],
                'completion_tokens':value['completion_tokens'], 'total_tokens':value['total_tokens'],
                'reasoning_tokens':details['reasoning_tokens'], 'cache_read':tokens['cache_read'],
                'cache_write_5m':tokens['cache_write_5m'], 'cache_write_1h':tokens['cache_write_1h'],
                'billing_source':'go', 'cost':billing['cost'], 'list_cost':billing['list_cost'],
                'allowance':billing['allowance']}
            basis['basis_digest'] = hashlib.sha256(canonical_message_bytes(basis)).hexdigest()
            validate_go_usage_basis(basis)
    return {'usage':usage, 'usage_presence':presence, 'usage_basis':basis,
            'accounting_complete':basis is not None or (all(presence.values()) and billing is None),
            'billing_complete':all(presence.values()) and billing is None,
            **({'billing_observation':billing} if billing is not None else {})}



def _standard_accounting_observation(value):
    # Preserve the pre-H4h independent accounting boundary: unrelated metadata
    # may make content unsupported without erasing explicit standard counters.
    if type(value) is not dict or not {'prompt_tokens','completion_tokens'} <= value.keys(): _fail()
    prompt, output = _integer(value['prompt_tokens']), _integer(value['completion_tokens'])
    details = value.get('prompt_tokens_details', {})
    if type(details) is not dict: _fail()
    hits = [details['cached_tokens']] if 'cached_tokens' in details else []
    hits += [value[key] for key in ('prompt_cache_hit_tokens','cached_tokens') if key in value]
    for hit in hits: _integer(hit)
    if not hits or len(set(hits)) != 1 or 'cache_write_tokens' not in details: _fail()
    read, write = hits[0], _integer(details['cache_write_tokens'])
    if read > prompt or write != 0: _fail()
    if 'total_tokens' in value and _integer(value['total_tokens']) != prompt+output: _fail()
    if 'prompt_cache_miss_tokens' in value and _integer(value['prompt_cache_miss_tokens']) != prompt-read: _fail()
    presence = {'prompt_tokens':True, 'completion_tokens':True,
                'cached_tokens':'cached_tokens' in details, 'cache_write_tokens':True}
    return {'usage':{'input':prompt-read,'output':output,'cacheRead':read,'cacheWrite':write},
            'usage_presence':presence,'usage_basis':None,'accounting_complete':True,
            'billing_complete':False}

def observe_response_usage(raw):
    """Accounting independently survives invalid decisive content; never authorizes it."""
    try:
        data = raw.encode('utf-8') if type(raw) is str else raw
        if type(data) is not bytes or not 1 <= len(data) <= MAX_BYTES:
            _fail()
        digest = hashlib.sha256(data).hexdigest()
        found = None
        done = False
        for line in data.decode('utf-8').splitlines():
            if not line.startswith('data:'):
                if line and not line.startswith(':'):
                    _fail()
                continue
            text = line[5:].strip()
            if text == '[DONE]':
                if done: _fail()
                done = True
                continue
            if done or not text: _fail()
            chunk = json.loads(text, object_pairs_hook=_object, parse_float=Decimal,
                               parse_constant=lambda _: _fail())
            if type(chunk) is not dict or ('model' in chunk and chunk['model'] != MODEL): _fail()
            if chunk.get('usage') is not None:
                if found is not None: _fail()
                value = chunk['usage']
                if type(value) is dict and 'opencode' in value:
                    found = _usage_observation(value, digest)
                    if not found['accounting_complete']:
                        # Complete original standard dimensions remain a known
                        # local estimate, even with unsupported Go metadata.
                        # This never creates a Go basis or authorizes content.
                        found = _standard_accounting_observation(value)
                else:
                    found = _standard_accounting_observation(value)
        if not done or found is None or not found['accounting_complete']: _fail()
        return found
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError, KeyError, DecimalException):
        raise PiOnlineResponseError('pi_provider_usage_unknown') from None


def _recoverable_argument_schema_error(error, schema):
    """Only structural public-schema failures; no custom validator messages."""
    kinds = {'missing', 'too_long', 'too_short', 'string_too_long', 'string_too_short',
             'less_than', 'less_than_equal', 'greater_than', 'greater_than_equal',
             'int_type', 'float_type', 'bool_type', 'string_type', 'list_type',
             'dict_type', 'literal_error', 'enum', 'finite_number', 'multiple_of'}
    fields = set()
    def visit(value):
        if isinstance(value, dict):
            properties = value.get('properties')
            if isinstance(properties, dict):
                fields.update(properties)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(schema.model_json_schema())
    forbidden = {'owner_id', 'requester_id', 'organization_id', 'store_id', 'subject_id',
                 'agent_subject_id', 'machine_id', 'permissions', 'permission', 'grants'}
    errors = error.errors(include_input=False, include_context=False, include_url=False)
    return bool(errors) and len(errors) <= 16 and all(
        item['type'] in kinds and 1 <= len(item['loc']) <= 8
        and all((type(part) is str and part in fields and part not in forbidden)
                or (type(part) is int and 0 <= part <= 100) for part in item['loc'])
        for item in errors)

def _normalize(raw, model, memory, discovery, source, categories, category_products_contract, admitted_schemas=None, recover_invalid_arguments=False):
    if type(model) is not str or model != MODEL or type(raw) not in (str, bytes):
        _fail()
    data = raw.encode('utf-8') if type(raw) is str else raw
    if not 1 <= len(data) <= MAX_BYTES:
        _fail()
    text = data.decode('utf-8')
    # Deliberately reject multiline SSE data, comments, event/id/retry fields,
    # partial final frames and mixed newline conventions.
    if '\r' in text:
        if '\n' in text.replace('\r\n', '') or '\r' in text.replace('\r\n', ''):
            _fail()
        text = text.replace('\r\n', '\n')
    if not text.endswith('\n\n'):
        _fail()
    frames = text.rstrip('\n').split('\n\n')
    if not frames or frames[-1] != 'data: [DONE]':
        _fail()
    completion_id = None
    usage = None
    finish = None
    content = ''
    tools = {}
    order = []
    mode = None
    role_seen = False
    reasoning_seen = False
    accounting = None
    raw_digest = hashlib.sha256(data).hexdigest()
    for frame in frames[:-1]:
        if not frame.startswith('data: ') or '\n' in frame:
            _fail()
        chunk = _json(frame[6:])
        _shape(chunk, {'id', 'object', 'model', 'choices'}, {'usage', 'created', 'system_fingerprint'})
        cid = _text(chunk['id'])
        if not 1 <= len(cid) <= 200 or chunk['model'] != model or chunk['object'] != 'chat.completion.chunk':
            _fail()
        if completion_id is None:
            completion_id = cid
        if cid != completion_id or usage is not None:
            _fail()
        if 'created' in chunk:
            _integer(chunk['created'])
        if 'system_fingerprint' in chunk and chunk['system_fingerprint'] is not None:
            _text(chunk['system_fingerprint'])
        choices = chunk['choices']
        if type(choices) is not list:
            _fail()
        has_usage = 'usage' in chunk and chunk['usage'] is not None
        if not choices:
            if not has_usage or finish is None:
                _fail()
            exact = json.loads(frame[6:], object_pairs_hook=_object,
                               parse_float=Decimal, parse_constant=lambda _: _fail())
            accounting = _usage_observation(exact['usage'], raw_digest)
            usage, usage_presence = accounting['usage'], accounting['usage_presence']
            continue
        if len(choices) != 1 or finish is not None:
            _fail()
        choice = choices[0]
        _shape(choice, {'index', 'delta'}, {'finish_reason', 'logprobs'})
        # OpenAI-compatible streams may explicitly report no token scores.
        # Preserve the original bytes/digest; unsupported score payloads fail.
        if choice.get('logprobs') is not None:
            _fail()
        if type(choice['index']) is not int or choice['index'] != 0:
            _fail()
        delta = choice['delta']
        _shape(delta, set(), {'role', 'content', 'tool_calls', 'reasoning_content'})
        if delta.get('role') is not None:
            if delta['role'] != 'assistant' or (not role_seen and (reasoning_seen or mode is not None)):
                _fail()
            role_seen = True
        if delta.get('reasoning_content') is not None:
            reasoning = _text(delta['reasoning_content'])
            # Go may repeat an empty optional field on every content chunk.
            # Only actual reasoning participates in the ordering constraint.
            if reasoning:
                if mode is not None or not role_seen:
                    _fail()
                reasoning_seen = True
        if 'content' in delta and delta['content'] is not None:
            fragment = _text(delta['content'])
            if fragment:
                if mode == 'tool' and not memory:
                    _fail()
                if not content:
                    order.append('text')
                mode = 'text' if not tools else 'tool'
                content += fragment
                if len(content) > (MAX_BYTES if discovery else 8000):
                    _fail()
        if delta.get('tool_calls') is not None:
            if not memory and (mode == 'text' or delta.get('content') not in (None, '')):
                _fail()
            calls = delta['tool_calls']
            if type(calls) is not list or not 1 <= len(calls) <= (4 if memory else 1):
                _fail()
            mode = 'tool'
            frame_indexes = set()
            for call in calls:
                _shape(call, {'index', 'function'}, {'id', 'type'})
                index = call['index']
                if (type(index) is not int or not 0 <= index < (4 if memory else 1)
                        or index in frame_indexes or call.get('type', 'function') != 'function'):
                    _fail()
                frame_indexes.add(index)
                if index not in tools:
                    # SDK preserves first appearance, not a reordered projection.
                    if index != len(tools):
                        _fail()
                    tools[index] = {'id':'', 'name':'', 'arguments':''}
                    order.append(index)
                tool = tools[index]
                if 'id' in call:
                    new_id = _text(call['id'])
                    if (not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', new_id)
                            or (tool['id'] and tool['id'] != new_id)
                            or any(i != index and t['id'] == new_id for i,t in tools.items())):
                        _fail()
                    tool['id'] = new_id
                function = call['function']
                _shape(function, set(), {'name', 'arguments'})
                if 'name' in function:
                    name = _text(function['name'])
                    if not name or (tool['name'] and tool['name'] != name):
                        _fail()
                    tool['name'] = name
                if 'arguments' in function:
                    tool['arguments'] += _text(function['arguments'])
        # Omitted intermediate finish_reason is unfinished, never success.
        # The explicit final reason, usage and [DONE] remain mandatory below.
        reason = choice.get('finish_reason')
        if reason is not None:
            if reason not in ('stop', 'tool_calls', 'length'):
                _fail()
            finish = reason
        if has_usage:
            if reason is None:
                _fail()
            exact = json.loads(frame[6:], object_pairs_hook=_object,
                               parse_float=Decimal, parse_constant=lambda _: _fail())
            accounting = _usage_observation(exact['usage'], raw_digest)
            usage, usage_presence = accounting['usage'], accounting['usage_presence']
    if reasoning_seen and (accounting is None or accounting['usage_basis'] is None):
        _fail()
    if completion_id is None or usage is None or finish is None:
        _fail()
    if finish == 'length':
        if accounting is None or not accounting['accounting_complete']:
            _fail()
        raise PiOnlineResponseTruncated('pi_provider_output_truncated')
    if mode == 'text' and finish == 'stop' and 1 <= len(content) <= (MAX_BYTES if discovery else 8000):
        items = [{'type': 'text', 'text': content}]
        stop = 'stop'
    elif mode == 'tool' and finish == 'tool_calls' and tools:
        from .tool_registry import ListMineInput, KnowledgeSearchInput, CatalogSearchInput, MemorySearchInput
        schemas = {'v4_tasks_list_mine_v1': ListMineInput,
                   'v4_knowledge_search_v1': KnowledgeSearchInput,
                   'v4_catalog_search_v1': CatalogSearchInput, 'v4_memory_search_v1': MemorySearchInput}
        if discovery:
            from .capability_discovery import SearchInput
            schemas['tool_search'] = SearchInput
        if memory:
            from .tools.memory import SaveInput, CorrectInput, WithdrawInput
            schemas.update({'v4_memory_save_v1':SaveInput,'v4_memory_correct_v1':CorrectInput,'v4_memory_withdraw_v1':WithdrawInput})
        if source:
            from .tool_registry import SourceProductInput
            schemas.update({'v4_guanjia_read_prices_v1':SourceProductInput,'v4_guanjia_read_stock_v1':SourceProductInput})
        if categories:
            from .tool_registry import CategoryDiscoveryInput, CategoryProductsInput
            product_name = ('v4_guanjia_category_products_status_v1' if category_products_contract is not None
                            else 'v4_guanjia_category_products_v1')
            schemas.update({'v4_guanjia_category_discovery_v1':CategoryDiscoveryInput,
                            product_name:CategoryProductsInput})
        if admitted_schemas is not None:
            schemas = admitted_schemas
        normalized = {}
        invalid_arguments = []
        for index, tool in tools.items():
            if not tool['id'] or tool['name'] not in schemas:
                _fail()
            args = _json(tool['arguments'])
            if type(args) is not dict:
                _fail()
            # No defaults, coercion, discarded calls or repaired arguments.
            from pydantic import ValidationError
            try:
                schemas[tool['name']].model_validate(args, strict=True)
            except ValidationError as error:
                if not recover_invalid_arguments or not _recoverable_argument_schema_error(error, schemas[tool['name']]):
                    raise
                invalid_arguments.append({'call_id':tool['id'], 'name':tool['name'],
                                          'code':'tool_arguments_schema_invalid'})
            canonical_message_bytes(args)
            normalized[index] = {'type':'toolCall', 'id':tool['id'],
                                 'name':tool['name'], 'arguments':args}
        if invalid_arguments and any(t['name'] == 'tool_search' for t in tools.values()):
            _fail()
        items = [{'type':'text', 'text':content} if key == 'text' else normalized[key] for key in order]
        stop = 'toolUse'
    else:
        _fail()
    message = {'role': 'assistant', 'content': items, 'usage': usage, 'stopReason': stop}
    return {**({'invalid_arguments':invalid_arguments} if mode == 'tool' and invalid_arguments else {}),
            'raw_sha256': raw_digest,
            'completion_id': 'sha256:'+hashlib.sha256(completion_id.encode('utf-8')).hexdigest(),
            'message': message, 'message_sha256': hashlib.sha256(canonical_message_bytes(message)).hexdigest(),
            'usage': dict(usage), 'usage_presence': usage_presence,
            **{key:value for key,value in accounting.items() if key not in ('usage', 'usage_presence')}}


def normalize_response(raw: bytes | str, *, model: str, memory: bool = False, discovery: bool = False,
                       source: bool = False, categories: bool = False, category_products_contract=None,
                       verified_binding=None, active_names=None, recover_invalid_arguments=False) -> dict:
    """Parse one complete response. No I/O, persistence, repair or tool execution."""
    try:
        if type(memory) is not bool or type(discovery) is not bool or type(source) is not bool or type(categories) is not bool or (discovery and not memory) or (source and not discovery) or (categories and not source):
            _fail()
        if category_products_contract is not None and (
                type(category_products_contract) is not str
                or category_products_contract != 'v4.guanjia-category/2' or not categories):
            _fail()
        if type(recover_invalid_arguments) is not bool:
            _fail()
        admitted=None
        if verified_binding is not None or active_names is not None:
            if not source:_fail()
            from .sales_ranking_runtime import response_schemas
            admitted=response_schemas(verified_binding,active_names)
        return _normalize(raw, model, memory, discovery, source, categories, category_products_contract,admitted,recover_invalid_arguments)
    except PiOnlineResponseTruncated:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError, KeyError, DecimalException):
        raise PiOnlineResponseError('pi_online_response_invalid') from None
