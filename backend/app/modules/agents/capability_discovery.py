"""Host receipts for Pi's official metadata-only tool discovery.

Ranking stays in the pinned public Pi extension. Selected names are untrusted
until checked here; neither relevance nor business truth is certified here.
"""
import json
from hashlib import sha256
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

PROFILE = 'pi-discovery-v1'
SOURCE_PROFILE = 'pi-source-v1'
NAME = 'tool_search'
KEY = 'runtime.tool_search'
EVENTS = 'pi_discovery_events'
DESCRIPTION = ('Search available authorized tools by purpose, then load their exact schemas. '
               '先搜索可用能力，再调用已装载工具。搜索只返回工具信息，不证明业务事实或授予新权限。')


class SearchInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str = Field(min_length=1, max_length=2048, description='Search query for deferred tools.')
    limit: int = Field(default=8, ge=1, le=100, description='Maximum number of tools to load.')


class SearchOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    authority: Literal['metadata_only']
    loaded: list[str]
    catalog_digest: str


def _digest(value):
    return 'sha256:' + sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def enabled(ctx):
    return ctx.execution.frozen_context['pi_readonly_binding']['profile'] in (PROFILE, SOURCE_PROFILE)


def _deny():
    from backend.app.modules.agents.tool_registry import ToolRegistryError
    raise ToolRegistryError('agent_pi_discovery_invalid')


def active_names(session, ctx):
    """Reconstruct loaded names from same-Run successful bound receipts."""
    from backend.app.modules.agents.models import AgentPiToolReceipt, AgentStep, AgentToolCall
    from sqlalchemy import select
    binding = ctx.execution.frozen_context['pi_readonly_binding']
    if not enabled(ctx):
        return [entry['name'] for entry in binding['tools']]
    allowed = {entry['name'] for entry in binding['tools']}
    if NAME not in allowed:
        _deny()
    active, calls = [NAME], set()
    events = ctx.execution.frozen_context.get(EVENTS, [])
    if type(events) is not list or len(events) > ctx.run.max_tool_calls:
        _deny()
    for event in events:
        if type(event) is not dict or set(event) != {'call_id','arguments','result','receipt_id','step_id'}:
            _deny()
        result = event['result']
        if (type(result) is not dict or set(result) != {'authority','loaded','catalog_digest'}
                or result['authority'] != 'metadata_only' or result['catalog_digest'] != ctx.run.toolset_version
                or type(result['loaded']) is not list or any(type(name) is not str for name in result['loaded'])):
            _deny()
        try:
            args = SearchInput.model_validate(event['arguments'])
        except ValueError:
            _deny()
        names = result['loaded']
        if (len(names) > args.limit or len(set(names)) != len(names)
                or any(name not in allowed or name in active for name in names)
                or type(event['call_id']) is not str or event['call_id'] in calls):
            _deny()
        receipt = session.scalar(select(AgentPiToolReceipt).where(
            AgentPiToolReceipt.organization_id == ctx.run.organization_id,
            AgentPiToolReceipt.store_id == ctx.run.store_id, AgentPiToolReceipt.run_id == ctx.run.id,
            AgentPiToolReceipt.id == event['receipt_id']))
        step = session.get(AgentStep, event['step_id'])
        call = session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == ctx.run.id,
            AgentToolCall.step_id == event['step_id']))
        # A successful tool result must still come from an earlier successful
        # model Step. Final-close validation is too late: loaded names can be
        # sent to the next model before close. Never repair or replay bad facts.
        model = session.get(AgentStep, receipt.model_step_id) if receipt is not None else None
        if (receipt is None or step is None or call is None or model is None
                or (receipt.contract_version, receipt.model_kind, receipt.tool_kind, receipt.error_code)
                    != (1, 'model', 'tool', None)
                or (model.organization_id, model.store_id, model.run_id, model.kind, model.state)
                    != (ctx.run.organization_id, ctx.run.store_id, ctx.run.id, 'model', 'succeeded')
                or model.sequence_no >= step.sequence_no
                or (call.organization_id, call.store_id, call.run_id, call.step_id, call.step_kind)
                    != (ctx.run.organization_id, ctx.run.store_id, ctx.run.id, step.id, 'tool')
                or receipt.native_call_id != event['call_id'] or receipt.pi_name != NAME
                or (receipt.tool_key, receipt.tool_version, receipt.toolset_version, receipt.state)
                    != (KEY, '1', ctx.run.toolset_version, 'succeeded')
                or receipt.input_digest != _digest(event['arguments'])[7:]
                or receipt.tool_step_id != step.id or receipt.output_ref != _digest(result)
                or (step.organization_id, step.store_id, step.run_id, step.kind, step.state, step.output_ref)
                    != (ctx.run.organization_id,ctx.run.store_id,ctx.run.id,'tool','succeeded',receipt.output_ref)
                or (call.tool_key,call.tool_version,call.input_digest) != (KEY,'1',receipt.input_digest)
                or call.idempotency_key != 'pi:' + _digest([ctx.run.id,event['call_id']])[7:]):
            _deny()
        active.extend(names); calls.add(event['call_id'])
    return active


def selection_result(session, ctx, arguments, selection):
    if not enabled(ctx):
        _deny()
    args = SearchInput.model_validate(arguments)
    active = active_names(session, ctx)
    allowed = {entry['name'] for entry in ctx.execution.frozen_context['pi_readonly_binding']['tools']}
    if (type(selection) is not list or any(type(name) is not str for name in selection)
            or len(selection) > args.limit or len(set(selection)) != len(selection)
            or any(name not in allowed or name in active for name in selection)):
        _deny()
    return {'authority':'metadata_only','loaded':list(selection),'catalog_digest':ctx.run.toolset_version}


def append_event(ctx, *, decision, receipt, step, result):
    frozen = dict(ctx.execution.frozen_context)
    events = list(frozen.get(EVENTS, []))
    events.append({'call_id':decision['native_call_id'],'arguments':decision['arguments'],
        'result':result,'receipt_id':receipt.id,'step_id':step.id})
    frozen[EVENTS] = json.loads(json.dumps(events, allow_nan=False))
    ctx.execution.frozen_context = frozen
