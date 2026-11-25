"""Fresh human/source authorization for stored safe public-trace projections.

No raw event body, provider text, executable permission, database write or retry.
This interface is called only within the existing owned Run/Work receipt read.
"""
from copy import deepcopy
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import select
from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.auth.service import validate_actor_session
from backend.app.modules.identity.command_guard import refresh_authority
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.agents import repository, tool_registry
from backend.app.modules.agents.models import AgentRun, AgentExecution
from backend.app.modules.agents import public_trace_capture as capture
from backend.app.modules.memory import service as memory_service
from backend.app.modules.memory.models import PersonalMemory

def _permission(session, actor, permission):
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission=permission)

def _human(session, actor):
    if (type(actor) is not AuthenticatedSubject or actor.account_id is None
        or actor.session_id is None):
        raise HTTPException(403, "public_trace_human_required")
    refresh_authority(session)
    validate_actor_session(session, actor)
    _permission(session, actor, "agents.run")

def _value(fields, key):
    node = fields.get(key)
    if type(node) is not dict or set(node) != {"state", "value"} or node["state"] != "recorded":
        raise ValueError()
    return node["value"]

def _tool_permission(session, actor, agent, key, version):
    definition = tool_registry.resolve_tool(key, version)
    tool_registry._agent_scope(session, actor, agent, definition.agent_permission)
    _permission(session, actor, definition.requester_permission)

def _allowed(session, actor, run, control, native, records):
    frozen = control.frozen_context["pi_readonly_binding"]
    agent = AuthenticatedSubject(frozen["agent_id"], actor.organization_id,
                                 actor.store_id, actor.auth_expires_at)
    _tool_permission(session, actor, agent, native.tool_key, native.tool_version)
    if native.tool_key == "runtime.tool_search":
        names = {e["name"]: e for e in frozen["tools"]}
        for record in records:
            if record["phase"] != "result": continue
            node = record["result"]["fields"]["loaded"]
            if node.get("state") != "recorded": raise ValueError()
            for item in node["items"]:
                name = _value({"name": item}, "name")
                if name not in names: raise ValueError()
                entry = names[name]
                _tool_permission(session, actor, agent, entry["tool_key"], entry["version"])
        return
    if native.tool_key == "memory.search":
        now = datetime.now(timezone.utc)
        for record in records:
            if record["phase"] != "result": continue
            items = record["result"]["fields"]["items"]
            if items.get("state") != "recorded": raise ValueError()
            for item in items["items"]:
                fields = item["fields"]
                identity, version = _value(fields, "id"), _value(fields, "version")
                row = session.execute(select(PersonalMemory.id, PersonalMemory.version,
                    PersonalMemory.active, PersonalMemory.retain_until_withdrawn).where(
                    *memory_service._scope(actor), PersonalMemory.id == identity,
                    PersonalMemory.version == version, PersonalMemory.active.is_(True),
                    memory_service._unexpired(now), memory_service._visibility_clause(actor))).one_or_none()
                if (row is None or not row.active
                    or row.active != _value(fields, "active")
                    or row.retain_until_withdrawn != _value(fields, "retain_until_withdrawn")):
                    raise ValueError()
        return
    if native.tool_key in ("research.search", "research.read", "research.describe", "research.propose"):
        from backend.app.modules.agents import research_host
        pin = frozen.get("research_source")
        if pin is None: raise ValueError()
        research_host.current(session.get_bind(), actor, pin)
        return
    # Not a broad source authorization fallback, even for valid stored schemas.
    raise ValueError()

def read_for_run(session, actor, run):
    """Return one safe per-Run DTO; does not commit or mutate the caller."""
    if session.new or session.dirty or session.deleted:
        raise HTTPException(409, "public_trace_pending_changes")
    with session.no_autoflush:
        _human(session, actor)
        current = session.scalar(select(AgentRun).where(AgentRun.id == run.id,
            AgentRun.organization_id == actor.organization_id,
            AgentRun.store_id == actor.store_id,
            AgentRun.requester_id == actor.subject_id).execution_options(populate_existing=True))
        if current is None:
            raise HTTPException(404, "agent_not_found")
        control = repository.execution_for(session, current.organization_id,
                                            current.store_id, current.id)
        try:
            events = capture.verified_events(session, current, control)
            if events is None:
                return {"version": 1, "state": "not_recorded", "records": []}
        except (capture.TraceError, ValueError, TypeError, KeyError, AttributeError):
            return {"version": 1, "state": "withheld",
                    "reason": "record_binding_unavailable", "records": []}
        grouped = {}
        for event, native, _ in events:
            group = grouped.setdefault(native.id, {"native": native, "projections": []})
            group["projections"].append(event["projection"])
        records = []
        for receipt_id, group in grouped.items():
            native = group["native"]
            state = native.state
            if state not in ("accepted", "dispatch_intent", "succeeded", "failed", "unknown"):
                state = "unknown"
            basic = dict(receipt_id=receipt_id, step_id=native.tool_step_id,
                         native_state=state)
            try:
                _human(session, actor)
                _allowed(session, actor, current, control, native, group["projections"])
                # This is same-transaction freshness, not a PostgreSQL race proof.
                _human(session, actor)
                _allowed(session, actor, current, control, native, group["projections"])
            except (HTTPException, tool_registry.ToolRegistryError, ValueError, TypeError,
                    KeyError, AttributeError, OSError):
                records.append({**basic, "state": "withheld",
                                "reason": "current_source_unavailable"})
                continue
            record = {**basic, "state": "available"}
            for body in group["projections"]:
                record[body["phase"]] = deepcopy(body)
            records.append(record)
        _human(session, actor)
        return dict(version=1, state="available", records=records)
