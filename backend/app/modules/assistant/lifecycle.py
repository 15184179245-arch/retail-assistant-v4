"""Explicit local assistant service; browser lifetime does not own the worker."""
import asyncio
from contextlib import asynccontextmanager
from threading import Event, Thread

from backend.app.modules.agents import pi_online_campaign as campaign_contract
from backend.app.modules.assistant.core_adapter import DshCoreAdapter
from backend.app.modules.assistant.dispatch import AssistantWorkerConfig, run_until_stopped, _config


class ManagedAssistant:
    def __init__(self, core, config):
        if type(core) is not DshCoreAdapter or type(config) is not AssistantWorkerConfig or config.online_policy is None:
            raise ValueError('assistant_host_configuration_invalid')
        _config(config)
        self.core, self.config = core, config
        self.stop = Event()
        self.thread = None
        self.failed = False

    def accepts(self, actor, *, require_dispatch_window=True):
        policy = self.config.online_policy
        return (self.thread is not None and self.thread.is_alive()
                and not self.stop.is_set() and not self.failed
                and (actor.organization_id, actor.store_id, actor.subject_id) ==
                    (policy.organization_id, policy.store_id, policy.requester_id)
                and (policy.dispatch_window_open() if require_dispatch_window
                     else campaign_contract.utc_now() < policy.expires_at))

    def _run(self):
        try:
            run_until_stopped(self.config, self.stop)
        except Exception:
            # Keep error text/credentials out of API and console. Work/run
            # receipts remain the diagnostic source; never retry this loop.
            self.failed = True
        finally:
            self.stop.set()

    @asynccontextmanager
    async def lifespan(self, app):
        if self.thread is not None or hasattr(app.state, 'assistant_core') or hasattr(app.state, 'assistant_host'):
            raise ValueError('assistant_host_already_started')
        app.state.assistant_host = self
        app.state.assistant_core = self.core
        self.thread = Thread(target=self._run, name='v4-pi-assistant', daemon=True)
        try:
            self.thread.start()
            yield
        finally:
            self.stop.set()
            if self.thread.ident is not None:
                await asyncio.to_thread(self.thread.join, 150)
            if getattr(app.state, 'assistant_core', None) is self.core:
                delattr(app.state, 'assistant_core')
            if getattr(app.state, 'assistant_host', None) is self:
                delattr(app.state, 'assistant_host')
            if self.thread.is_alive():
                raise RuntimeError('assistant_shutdown_unresolved')


def available_core(app, actor, *, require_dispatch_window=True):
    core = getattr(app.state, 'assistant_core', None)
    host = getattr(app.state, 'assistant_host', None)
    if host is not None and (type(host) is not ManagedAssistant or host.core is not core
                             or not host.accepts(actor, require_dispatch_window=require_dispatch_window)):
        return None
    return core if type(core) is DshCoreAdapter else None


def intake_work_policy(app, session, actor):
    """Host-selected optional authority, narrowed to current Pi capabilities."""
    from backend.app.modules.assistant.authority import WorkAuthorityPolicy
    from backend.app.modules.assistant.principal import DEFAULT_CAPABILITIES
    from backend.app.modules.identity.service import resolve_effective_capabilities
    host = getattr(app.state, 'assistant_host', None)
    if type(host) is not ManagedAssistant or not host.config.work_authority_enabled:
        return None
    if not host.accepts(actor, require_dispatch_window=False):
        raise ValueError('assistant_host_unavailable')
    permissions, _ = resolve_effective_capabilities(session, actor)
    allowed=DEFAULT_CAPABILITIES
    sales = getattr(host.config.pi, 'sales_source', None)
    if sales is not None:
        from backend.app.modules.integrations.guanjia import sales_ranking_source
        if sales_ranking_source.current(session.get_bind(), actor) is not sales:
            raise ValueError('assistant_host_unavailable')
        allowed = allowed | frozenset({'guanjia.sales.read'})
    research=getattr(host.config.pi,'research_source',None)
    if research is not None and 'read' in research.pin():
        from backend.app.modules.agents.research_broker_host import required_permissions
        allowed=allowed|frozenset(required_permissions(research))
    if research is not None and 'trial_name' in research.pin():
        allowed=frozenset({'agents.run','tasks.manage','tasks.read_self','knowledge.read'})
    return WorkAuthorityPolicy(host.config.online_policy.expires_at,
        tuple(sorted(allowed.intersection(permissions))))


def available_turn_core(app,actor,*,require_dispatch_window=True):
    """Unlike legacy intake, followups require an actually running managed Host."""
    host=getattr(app.state,'assistant_host',None)
    if (type(host) is not ManagedAssistant or not host.config.work_authority_enabled
            or available_core(app,actor,require_dispatch_window=require_dispatch_window) is None):return None
    return host.config.turns


def inbox_enabled(app, actor):
    host = getattr(app.state, 'assistant_host', None)
    return (type(host) is ManagedAssistant and host.config.inbox_policy is not None
            and (actor.organization_id,actor.store_id,actor.subject_id) ==
                (host.config.online_policy.organization_id,host.config.online_policy.store_id,
                 host.config.online_policy.requester_id))


def available_inbox_policy(app, session, actor):
    from backend.app.modules.assistant.authority import WorkAuthorityPolicy
    from backend.app.modules.assistant.inbox import REQUIRED
    # Inbox receipts are distinct from direct dispatch intake.
    if (not inbox_enabled(app, actor)
            or available_turn_core(app, actor, require_dispatch_window=False) is None):return None
    host = app.state.assistant_host
    current = intake_work_policy(app, session, actor)
    permissions = tuple(sorted(set(current.permissions).intersection(host.config.inbox_policy.permissions)))
    expiry = min(current.expires_at, host.config.inbox_policy.expires_at)
    if not REQUIRED <= set(permissions) or campaign_contract.utc_now() >= expiry: return None
    return WorkAuthorityPolicy(expiry, permissions)


def unavailable_reason(app, actor):
    host = getattr(app.state, 'assistant_host', None)
    if type(host) is ManagedAssistant:
        policy = host.config.online_policy
        if (actor.organization_id,actor.store_id,actor.subject_id) == (policy.organization_id,policy.store_id,policy.requester_id):
            if campaign_contract.utc_now() >= policy.expires_at: return 'trial_expired'
    return 'unavailable'


def workspace_context_available(app, actor):
    """Host readiness only, never a grant for a specific thread/kind/operation."""
    host = getattr(app.state, 'assistant_host', None)
    if (type(host) is not ManagedAssistant or not host.accepts(actor)
            or not host.config.work_authority_enabled
            or getattr(host.config, 'tool_profile', None) != 'pi-workbench-v1'):
        return False
    from backend.app.modules.agents.tool_registry import resolve_tool, ToolRegistryError
    try:
        for operation in ('read', 'create', 'patch'):
            resolve_tool('artifact.' + operation, '1')
    except ToolRegistryError:
        return False
    return True
