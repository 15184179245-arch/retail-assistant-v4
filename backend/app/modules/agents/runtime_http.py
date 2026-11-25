"""Opt-in, Host-configured fresh SDK submit. No public engine/config/attach API.

This synchronous isolated profile does not enable the existing POST, resume an
old execution, schedule background work, or select a production Provider.
"""
import http.client
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4
from typing import Callable
from pydantic import BaseModel, ConfigDict, Field, field_validator
from fastapi import Request
from sqlalchemy.orm import Session
from backend.app.core.model_context import MAX_MESSAGE_CHARS
from backend.app.modules.auth.service import validate_actor_session
from backend.app.modules.agents import service, repository
from backend.app.modules.agents.models import AgentExecution
from backend.app.modules.agents.runtime_bridge import FreshRunBridge
from backend.app.modules.agents.runtime_launch import HostConfig, prepare_release, run_prepared
from backend.app.workers.agent_executor import _origin_text

@dataclass(frozen=True)
class SdkHttpConfig:
    launch: HostConfig
    session_factory: Callable
    machine_token: str
    organization_id: str
    store_id: str
    private_parent: Path

class SdkRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    run_id: str

    @field_validator('text')
    @classmethod
    def exact_text(cls, value):
        if not value or value != value.strip():
            raise ValueError('outer_whitespace_unsupported')
        return value

    @field_validator('run_id')
    @classmethod
    def canonical_id(cls, value):
        if str(UUID(value)) != value:
            raise ValueError('canonical_uuid_required')
        return value

def _fail(code, status=503):
    raise service.AgentRuntimeError(code, status)

def _config(request, actor):
    config = getattr(request.app.state, 'sdk_readonly', None)
    if config is None:
        _fail('sdk_submission_disabled')
    if (type(config) is not SdkHttpConfig
            or set(vars(config)) != {'launch','session_factory','machine_token','organization_id','store_id','private_parent'}
            or type(config.launch) is not HostConfig or not callable(config.session_factory)
            or any(type(x) is not str or not x for x in (config.machine_token,config.organization_id,config.store_id))):
        _fail('sdk_host_configuration_invalid')
    private = config.private_parent
    if (type(private) is not type(Path()) or not private.is_absolute()
            or '..' in private.parts or Path(config.launch.release_root)/'runtime' not in private.parents):
        _fail('sdk_host_configuration_invalid')
    if (actor.organization_id, actor.store_id) != (config.organization_id, config.store_id):
        _fail('sdk_scope_unavailable', 403)
    return config

def _existing(session, actor, payload):
    # Read-only authorization and exact old-row comparison; never claim here.
    validate_actor_session(session, actor)
    service._scope(session, actor)
    run = service.get_run(session, actor.organization_id, actor.store_id, payload.run_id)
    if run is None:
        return None
    if run.requester_id != actor.subject_id:
        _fail('agent_not_found', 404)
    try:
        repository.require_run_runtime(session, run, repository.SDK_RUNTIME)
    except repository.AgentPersistenceError as exc:
        raise service.AgentRuntimeError('agent_idempotency_runtime_conflict',409) from exc
    execution = session.get(AgentExecution, run.id)
    if execution is None:
        _fail('sdk_existing_binding_unavailable',409)
    # Both adapters use SDK_RUNTIME, but that tuple is not a tool/launch
    # profile discriminator. An existing formal Run cannot be adopted by this
    # experimental fast path, even when its requester and text are identical.
    frozen = execution.frozen_context
    if (type(frozen) is not dict or 'pi_readonly_binding' in frozen
            or 'pi_runtime_record' in frozen):
        _fail('agent_idempotency_runtime_conflict',409)
    if _origin_text(session, execution, run) != payload.text:
        _fail('agent_idempotency_conflict',409)
    return service.read_run_receipt(session, actor, run.id)

def submit(request: Request, payload: SdkRequest, actor, auth_session: Session):
    # This is the same FastAPI cached dependency used by real authentication.
    # Release it even on pure admission errors; no request transaction spans I/O.
    try:
        keys = [v for k,v in request.scope['headers'] if k.lower() == b'idempotency-key']
        if keys != [payload.run_id.encode('ascii')]:
            _fail('sdk_idempotency_exact_required',400)
        config = _config(request,actor)
        validate_actor_session(auth_session,actor)
        service._scope(auth_session,actor)
    finally:
        auth_session.rollback()
        auth_session.close()
    with config.session_factory() as s:
        receipt = _existing(s,actor,payload)
    if receipt is not None:
        return receipt
    # Hash all approved inputs before any new durable submission.
    try:
        prepared = prepare_release(config.launch)
        # Complete fallible private-name preparation before acquiring any Run.
        # Existing receipts already returned above and never need a new name.
        private = config.private_parent/('sdk-http-'+str(uuid4()))
    except Exception as exc:
        raise service.AgentRuntimeError('sdk_release_unavailable',503) from exc
    try:
        receipt,bridge = FreshRunBridge.submit_for_http(
            session_factory=config.session_factory,requester=actor,
            machine_token=config.machine_token,text=payload.text,key=payload.run_id,
            requested_run_id=payload.run_id)
    except service.AgentRuntimeError:
        raise
    except Exception as exc:
        # Includes uncertain submit/claim replies. Never query to invent owner.
        raise service.AgentRuntimeError('sdk_submission_unresolved',503) from exc
    if bridge is None:
        # The original fenced transaction serialized a competing first submit.
        # Its session has exited/rolled back. Recheck exact text/auth in a fresh
        # read transaction; no second submission, claim or private tree creation.
        with config.session_factory() as s:
            receipt = _existing(s,actor,payload)
        if receipt is None:
            _fail('sdk_submission_unresolved')
        return receipt
    # All operational preparation is complete before ownership. A normally
    # returned new claimant goes directly to the factory/supervisor; this HTTP
    # layer never calls abandon, including after their first unresolved result.
    try:
        run_prepared(prepared=prepared,private_tree=private,bridge=bridge,prompt=payload.text)
    except Exception as exc:
        raise service.AgentRuntimeError('sdk_runtime_failed',503) from exc
    with config.session_factory() as s:
        result = service.read_run_receipt(s,actor,payload.run_id)
    result['replayed'] = False
    return result



# Formal route is separate from the experimental adapter above. The caller
# cannot select a tool profile, module, Provider, release root or credential.
from backend.app.modules.agents.runtime_bridge import PiRunBridge
from backend.app.modules.agents.runtime_launch import PiHostConfig, prepare_pi_release, run_pi_prepared


@dataclass(frozen=True)
class PiHttpConfig:
    launch: PiHostConfig
    session_factory: Callable
    machine_token: str
    organization_id: str
    store_id: str
    private_parent: Path
    offline_transport: object = None
    campaign: object = None
    category_source: object = None
    research_source: object = None
    sales_source: object = None


class PiRequest(SdkRequest):
    """Canonical Run UUID + text; explicit engine choice is this fixed route."""


def _pi_config(request, actor, *, memory=False):
    config = getattr(request.app.state, "pi_memory" if memory else "pi_readonly", None)
    return validate_pi_config(config, actor)


def validate_pi_config(config, actor):
    """Host-only assembly validation shared by HTTP and the durable worker."""
    if config is None:
        _fail("pi_submission_disabled")
    if (type(config) is not PiHttpConfig
            or set(vars(config)) != {"launch", "session_factory", "machine_token", "organization_id", "store_id", "private_parent", "offline_transport", "campaign", "category_source", "research_source", "sales_source"}
            or type(config.launch) is not PiHostConfig or not callable(config.session_factory)
            or any(type(value) is not str or not value for value in
                   (config.machine_token, config.organization_id, config.store_id))):
        _fail("pi_host_configuration_invalid")
    if (type(config.private_parent) is not type(Path()) or not config.private_parent.is_absolute()
            or ".." in config.private_parent.parts
            or Path(config.launch.release_root)/"runtime" not in config.private_parent.parents):
        _fail("pi_host_configuration_invalid")
    if (actor.organization_id, actor.store_id) != (config.organization_id, config.store_id):
        _fail("pi_scope_unavailable", 403)
    if config.sales_source is not None:
        from backend.app.modules.integrations.guanjia import sales_ranking_source
        try:
            with config.session_factory() as db:
                if sales_ranking_source.current(db.get_bind(), actor) is not config.sales_source:
                    _fail('pi_host_configuration_invalid')
        except (ValueError, TypeError, OSError):
            _fail('pi_host_configuration_invalid')
    if config.research_source is not None:
        from . import research_host
        try:
            with config.session_factory() as db:
                if research_host.current(db.get_bind(),actor) is not config.research_source:_fail('pi_host_configuration_invalid')
        except (ValueError,TypeError,OSError):_fail('pi_host_configuration_invalid')
    if config.category_source is not None:
        from backend.app.modules.integrations.guanjia import category_host
        try:
            with config.session_factory() as db:
                if category_host.current(db.get_bind(),actor) is not config.category_source:
                    _fail('pi_host_configuration_invalid')
        except (ValueError,TypeError,OSError):
            _fail('pi_host_configuration_invalid')
    return config


def _pi_route_profile(memory, discovery, source=False):
    if type(memory) is not bool or type(discovery) is not bool or type(source) is not bool or discovery and not memory or source and not discovery:
        _fail('pi_profile_route_mismatch', 409)
    return 'pi-source-v1' if source else 'pi-discovery-v1' if discovery else 'pi-memory-v1' if memory else 'pi-readonly-v1'


def _pi_existing(session, actor, payload, *, memory=False, discovery=False, source=False):
    from backend.app.modules.assistant import principal
    if type(actor) is principal.WorkPrincipal:
        principal.check(session, actor)
        if actor.run_id != payload.run_id:
            _fail('agent_idempotency_runtime_conflict', 409)
    else:
        validate_actor_session(session, actor)
    service._scope(session, actor)
    run = service.get_run(session, actor.organization_id, actor.store_id, payload.run_id)
    if run is None:
        return None
    if run.requester_id != actor.subject_id:
        _fail("agent_not_found", 404)
    try:
        repository.require_run_runtime(session, run, repository.SDK_RUNTIME)
    except repository.AgentPersistenceError as error:
        raise service.AgentRuntimeError("agent_idempotency_runtime_conflict", 409) from error
    execution = repository.execution_for(session, actor.organization_id, actor.store_id, run.id)
    if execution is not None:
        bound = principal.from_execution(session, execution, run)
        if type(actor) is principal.WorkPrincipal and bound != actor:
            _fail('agent_idempotency_runtime_conflict', 409)
    frozen = execution.frozen_context if execution is not None else None
    if (type(frozen) is not dict or type(frozen.get("pi_readonly_binding")) is not dict
            or frozen["pi_readonly_binding"].get("profile") != _pi_route_profile(memory, discovery, source)):
        _fail("agent_idempotency_runtime_conflict", 409)
    try:
        record = repository.validate_pi_runtime_record(frozen.get("pi_runtime_record"))
        if repository.pi_record_tool_profile(record) != frozen["pi_readonly_binding"]["profile"]:
            _fail("agent_idempotency_runtime_conflict",409)
    except repository.AgentPersistenceError as error:
        raise service.AgentRuntimeError("agent_idempotency_runtime_conflict", 409) from error
    if _origin_text(session, execution, run) != payload.text:
        _fail("agent_idempotency_conflict", 409)
    # Existing rows need no current release preparation and cannot re-spawn,
    # reclaim, re-emit any result or silently adopt a different launch digest.
    return service.read_run_receipt(session, actor, run.id)


def submit_pi(request: Request, payload: PiRequest, actor, auth_session: Session, *, memory=False):
    try:
        keys = [value for key, value in request.scope["headers"] if key.lower() == b"idempotency-key"]
        if keys != [payload.run_id.encode("ascii")]:
            _fail("pi_idempotency_exact_required", 400)
        config = _pi_config(request, actor, memory=memory)
        validate_actor_session(auth_session, actor)
        service._scope(auth_session, actor)
    finally:
        # Never retain the auth transaction through source hashing or Node I/O.
        # A failed rollback/close stops the request, not a new Session fallback.
        auth_session.rollback()
        auth_session.close()
    with config.session_factory() as session:
        receipt = _pi_existing(session, actor, payload, memory=memory)
    if receipt is not None:
        return receipt
    prepared, private = prepare_pi_submission(config, actor, payload, memory=memory)
    return execute_pi_submission(config, actor, payload, prepared, private, memory=memory)


def prepare_pi_submission(config, actor, payload, *, memory=False, discovery=False, source=False):
    """No Run creation, claim, dispatch or network. Caller must recheck authority."""
    validate_pi_config(config, actor)
    try:
        prepared = prepare_pi_release(config.launch)
        if repository.pi_record_tool_profile(prepared.runtime_record()) != _pi_route_profile(memory, discovery, source):
            _fail("pi_profile_route_mismatch",409)
        if prepared.runtime_record()['contract_version'] == 2:
            from backend.app.modules.agents.pi_online_transport import PiOnlineTransport
            # HTTPS is available only to the explicit trusted one-response probe.
            # Ordinary v2 and the existing synthetic campaign stay offline.
            if (type(config.offline_transport) is not PiOnlineTransport
                    or config.offline_transport._run_scope is not None):
                raise ValueError('pi_online_transport_held')
            if config.campaign is not None:
                from backend.app.modules.agents.pi_online_campaign import PiOnlineCampaign
                if type(config.campaign) is not PiOnlineCampaign:
                    raise ValueError('pi_campaign_held')
                config.campaign.check_submission(actor, payload.text, payload.run_id,
                    prepared.runtime_record(), config.offline_transport)
            elif config.offline_transport._connection_factory is http.client.HTTPSConnection:
                raise ValueError('pi_online_transport_held')
        elif config.offline_transport is not None:
            raise ValueError('pi_online_transport_binding')
        elif config.campaign is not None:
            raise ValueError('pi_campaign_held')
        private = config.private_parent/("pi-http-"+str(uuid4()))
        if (not private.parent.is_dir() or private.parent.resolve() != private.parent
                or private.exists() or private.is_symlink()):
            raise ValueError('pi_private_parent_unavailable')
    except Exception as error:
        raise service.AgentRuntimeError("pi_release_unavailable", 503) from error
    return prepared, private


def execute_pi_submission(config, actor, payload, prepared, private, *, memory=False, discovery=False, source=False):
    """Execute only a new, normally returned Pi claim; never adopt an old Run."""
    if repository.pi_record_tool_profile(prepared.runtime_record()) != _pi_route_profile(memory, discovery, source):
        _fail('pi_profile_route_mismatch', 409)
    try:
        receipt, bridge = PiRunBridge.submit_for_http(session_factory=config.session_factory,
            requester=actor, machine_token=config.machine_token, text=payload.text,
            key=payload.run_id, requested_run_id=payload.run_id, runtime_record=prepared.runtime_record(),
            offline_transport=config.offline_transport, campaign=config.campaign, category_source=config.category_source, research_source=config.research_source, sales_source=config.sales_source)
    except service.AgentRuntimeError:
        raise
    except Exception as error:
        # In particular, no ownership reconstruction after commit/claim reply loss.
        raise service.AgentRuntimeError("pi_submission_unresolved", 503) from error
    if bridge is None:
        with config.session_factory() as session:
            receipt = _pi_existing(session, actor, payload, memory=memory, discovery=discovery, source=source)
        if receipt is None:
            _fail("pi_submission_unresolved")
        return receipt
    try:
        def publish(report):
            if config.campaign is not None and config.campaign.mode == 'real-assistant-trial':
                from backend.app.modules.agents.pi_probe_artifacts import record_runtime_report
                record_runtime_report(config.campaign.artifact_directory, report)
        run_pi_prepared(prepared=prepared, private_tree=private, bridge=bridge, prompt=payload.text, publish=publish)
    except Exception as error:
        # Supervisor already owns its single A disposition. This layer only
        # returns a generic failure; it never retries or calls abandon again.
        raise service.AgentRuntimeError("pi_runtime_failed", 503) from error
    with config.session_factory() as session:
        result = service.read_run_receipt(session, actor, payload.run_id)
    result["replayed"] = False
    return result
