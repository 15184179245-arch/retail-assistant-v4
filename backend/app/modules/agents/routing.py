"""AGT-012: explicit, fail-closed role routing over the SINGLE existing tool gateway.

This is a trusted-host primitive, not an HTTP endpoint or model tool. A role
is an upper bound, never a permission or independent Agent identity. The
current Agent Runner is NOT silently switched to this module in AGT-012.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents import conversation_service, skill_service
from backend.app.modules.agents.models import AgentConversation
from backend.app.modules.agents.tool_registry import (
    TOOLS, ToolRegistryError, _person_scope, invoke_tool, model_tool_descriptions,
)
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope


class RoleRoutingError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# Absolute authority ceiling is in code, NOT configurable using a model,
# retrieved knowledge or third-party skill. A spec may only REMOVE entries.
_READ_TOOL_CEILINGS: dict[str, frozenset[tuple[str, str]]] = {
    "personal_partner": frozenset({
        ("tasks.list_mine", "1"), ("catalog.search", "1"), ("knowledge.search", "1"),
    }),
    "store_assistant": frozenset({
        ("catalog.search", "1"), ("knowledge.search", "1"),
    }),
    "specialist": frozenset({
        ("tasks.list_mine", "1"), ("catalog.search", "1"), ("knowledge.search", "1"),
    }),
}
_SPECIALTY_CEILINGS: dict[str, frozenset[tuple[str, str]]] = {
    "catalog": frozenset({("catalog.search", "1")}),
    "knowledge": frozenset({("knowledge.search", "1")}),
    "tasks": frozenset({("tasks.list_mine", "1")}),
}
_TASK_CEILINGS: dict[str, frozenset[tuple[str, str]]] = {
    "catalog_lookup": _SPECIALTY_CEILINGS["catalog"],
    "knowledge_lookup": _SPECIALTY_CEILINGS["knowledge"],
    "own_tasks": _SPECIALTY_CEILINGS["tasks"],
}
_SKILL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
_SPEC_PATH = Path(__file__).resolve().parents[4] / "agents/specs/agent-roles.json"


@dataclass(frozen=True)
class RolePlan:
    """A snapshot for presentation; NOT a durable authorization grant."""

    role_key: str
    specialty: str | None
    task_kind: str | None
    organization_id: str
    store_id: str
    requester_id: str
    agent_subject_id: str
    config_digest: str
    allowed_tools: tuple[tuple[str, str], ...]
    tool_descriptions: tuple[dict, ...]
    personal_context: dict | None
    skill_binding: dict | None


def _fail(code: str):
    raise RoleRoutingError(code)


def _refs(raw: object, *, ceiling: frozenset[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    if not isinstance(raw, list):
        _fail("role_spec_invalid")
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"key", "version"}:
            _fail("role_spec_invalid")
        key, version = item["key"], item["version"]
        if not isinstance(key, str) or not isinstance(version, str):
            _fail("role_spec_invalid")
        ref = (key, version)
        registered = TOOLS.get(ref)
        if ref in seen or ref not in ceiling or registered is None or registered.mode != "read":
            _fail("role_spec_permission_escalation")
        seen.add(ref)
    return frozenset(seen)


def _load_spec() -> tuple[dict, str]:
    """Read only a pinned repository path; never accept requester-supplied paths."""
    try:
        raw = _SPEC_PATH.read_bytes()
        if not raw or len(raw) > 32_768:
            _fail("role_spec_invalid")
        spec = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise RoleRoutingError("role_spec_invalid") from exc
    if (not isinstance(spec, dict)
            or type(spec.get("schema_version")) is not int
            or spec["schema_version"] != 1
            or spec.get("agent_key") != "shop-assistant"
            or spec.get("policy") != "role-config-is-an-upper-bound-not-a-grant"
            or not isinstance(spec.get("roles"), dict)
            or set(spec["roles"]) != set(_READ_TOOL_CEILINGS)
            or not isinstance(spec.get("task_kinds"), dict)
            or set(spec["task_kinds"]) != set(_TASK_CEILINGS)):
        _fail("role_spec_invalid")
    for key, ceiling in _READ_TOOL_CEILINGS.items():
        entry = spec["roles"][key]
        if not isinstance(entry, dict) or entry.get("context") != (
                "requester_active_conversation_only" if key == "personal_partner"
                else "no_personal_or_private_memory"):
            _fail("role_spec_invalid")
        _refs(entry.get("allowed_tools"), ceiling=ceiling)
        skills = entry.get("allowed_skill_keys")
        if (not isinstance(skills, list) or len(skills) > 16
                or any(not isinstance(x, str) or _SKILL.fullmatch(x) is None for x in skills)
                or len(set(skills)) != len(skills)):
            _fail("role_spec_invalid")
        if key == "specialist":
            specialties = entry.get("specialties")
            if not isinstance(specialties, dict) or set(specialties) != set(_SPECIALTY_CEILINGS):
                _fail("role_spec_invalid")
            for name, limit in _SPECIALTY_CEILINGS.items():
                _refs(specialties[name], ceiling=limit)
    for key, ceiling in _TASK_CEILINGS.items():
        _refs(spec["task_kinds"][key], ceiling=ceiling)
    return spec, sha256(raw).hexdigest()


def _validate_identities(
    session: Session, requester: AuthenticatedSubject, agent: AuthenticatedSubject,
) -> None:
    """Delegate ALL business permission policy to the platform's existing helpers."""
    try:
        _person_scope(session, requester)  # real human login/session, active org/store
        authorize_scope(session, requester, organization_id=requester.organization_id,
                        store_id=requester.store_id, permission="agents.run")
        if ((agent.organization_id, agent.store_id) !=
                (requester.organization_id, requester.store_id)):
            _fail("role_scope_denied")
        if agent.account_id is not None or agent.session_id is not None:
            _fail("role_agent_invalid")
        authorize_scope(session, agent, organization_id=requester.organization_id,
                        store_id=requester.store_id)
    except ToolRegistryError as exc:
        raise RoleRoutingError(exc.code) from exc
    except HTTPException as exc:
        raise RoleRoutingError("role_scope_or_permission_denied") from exc
    kind = session.scalar(select(Subject.subject_type).where(
        Subject.organization_id == agent.organization_id,
        Subject.id == agent.subject_id,
    ))
    if kind != "agent":
        _fail("role_agent_invalid")


def _context_self_only(session: Session, requester: AuthenticatedSubject) -> dict | None:
    # Do not create an empty conversation as a side effect of planning a route.
    found = session.scalar(select(AgentConversation.id).where(
        AgentConversation.organization_id == requester.organization_id,
        AgentConversation.store_id == requester.store_id,
        AgentConversation.subject_id == requester.subject_id,
        AgentConversation.status == "active",
    ).limit(1))
    if found is None:
        return None
    return conversation_service.context_for_agent(session, requester)


def select_role(
    session: Session,
    *,
    requester: AuthenticatedSubject,
    agent: AuthenticatedSubject,
    role_key: str,
    specialty: str | None = None,
    task_kind: str | None = None,
    skill_key: str | None = None,
) -> RolePlan:
    """Select a role using trusted host metadata, NOT model output or prompt text.

    Model-visible tools = role ceiling ∩ static role spec ∩ current requester/
    Agent platform grants. All tool execution must call invoke_role_read_tool
    rather than treating RolePlan as a permission or handing it to the model.
    """
    if not isinstance(role_key, str) or role_key not in _READ_TOOL_CEILINGS:
        _fail("role_unknown")
    if (role_key == "specialist" and specialty not in _SPECIALTY_CEILINGS) or (
            role_key != "specialist" and specialty is not None):
        _fail("role_specialty_invalid")
    if task_kind is not None and (
            not isinstance(task_kind, str) or task_kind not in _TASK_CEILINGS):
        _fail("role_task_kind_invalid")
    if role_key == "specialist" and task_kind is not None:
        if not _SPECIALTY_CEILINGS[specialty].intersection(_TASK_CEILINGS[task_kind]):
            _fail("role_task_kind_conflict")
    if skill_key is not None and (
            not isinstance(skill_key, str) or _SKILL.fullmatch(skill_key) is None):
        _fail("role_skill_invalid")
    if not isinstance(requester, AuthenticatedSubject) or not isinstance(agent, AuthenticatedSubject):
        _fail("role_identity_invalid")
    # A long-lived transaction may hold ORM objects loaded before a grant was
    # revoked in another transaction. Flush pending local writes, then reload
    # account/session/permission rows from the DB before calculating exposure.
    session.flush()
    session.expire_all()
    _validate_identities(session, requester, agent)
    spec, digest = _load_spec()
    role = spec["roles"][role_key]
    enabled = _refs(role["allowed_tools"], ceiling=_READ_TOOL_CEILINGS[role_key])
    if role_key == "specialist":
        enabled &= _refs(role["specialties"][specialty],
                        ceiling=_SPECIALTY_CEILINGS[specialty])
    if task_kind is not None:
        enabled &= _refs(spec["task_kinds"][task_kind], ceiling=_TASK_CEILINGS[task_kind])
    # The existing tool gateway owns ALL permission logic, including the
    # permission on the real agent Subject. Do not duplicate it here.
    descriptions = model_tool_descriptions(session, requester, agent)
    current = {
        (item["tool_key"], item["version"]): item
        for item in descriptions if (item["tool_key"], item["version"]) in enabled
        and TOOLS[(item["tool_key"], item["version"])].mode == "read"
    }
    allowed = tuple(sorted(current))
    # Personal context is NEVER promoted to store/specialist role.
    context = _context_self_only(session, requester) if role_key == "personal_partner" else None
    binding = None
    if skill_key is not None:
        # Default config allows NO skill; additional skill runtime automation
        # is expressly reserved for SKL-003. Even a configured key must be a
        # currently active approved release for this exact employee/store.
        if skill_key not in role["allowed_skill_keys"]:
            _fail("role_skill_not_allowed")
        try:
            binding = skill_service.current_binding(session, requester, skill_key=skill_key)
        except (skill_service.SkillReleaseError, HTTPException) as exc:
            raise RoleRoutingError("role_skill_unavailable") from exc
    return RolePlan(
        role_key=role_key, specialty=specialty, task_kind=task_kind,
        organization_id=requester.organization_id, store_id=requester.store_id,
        requester_id=requester.subject_id, agent_subject_id=agent.subject_id,
        config_digest=digest, allowed_tools=allowed,
        tool_descriptions=tuple(current[ref] for ref in allowed),
        personal_context=context, skill_binding=binding,
    )


def invoke_role_read_tool(
    session: Session, *,
    requester: AuthenticatedSubject, agent: AuthenticatedSubject,
    plan: RolePlan, tool_key: str, version: str, arguments: object,
) -> dict:
    """Always revalidate employee, agent, config, and tool before each read.

    Caller owns the transaction/audit commit. No model-controlled role choice,
    write task, approval, arbitrary SQL/HTTP or external ERP endpoint exists.
    """
    if not isinstance(plan, RolePlan):
        _fail("role_plan_invalid")
    if ((plan.organization_id, plan.store_id, plan.requester_id, plan.agent_subject_id) !=
            (requester.organization_id, requester.store_id, requester.subject_id,
             agent.subject_id)):
        _fail("role_plan_scope_changed")
    if not isinstance(tool_key, str) or not isinstance(version, str):
        _fail("role_tool_not_allowed")
    # A published skill binding is not a runtime grant. This module must not
    # execute skill-selected workflows before SKL-003 freezes the binding.
    if plan.skill_binding is not None:
        _fail("role_skill_execution_deferred")
    fresh = select_role(session, requester=requester, agent=agent,
                        role_key=plan.role_key, specialty=plan.specialty,
                        task_kind=plan.task_kind)
    ref = (tool_key, version)
    if (fresh.config_digest != plan.config_digest
            or ref not in plan.allowed_tools or ref not in fresh.allowed_tools
            or TOOLS.get(ref) is None or TOOLS[ref].mode != "read"):
        _fail("role_tool_not_allowed")
    # invoke_tool repeats scope and both capabilities. Its audit is staged in
    # the exact same caller transaction as this invocation.
    return invoke_tool(session, requester=requester, agent=agent, tool_key=tool_key,
                       version=version, arguments=arguments)
