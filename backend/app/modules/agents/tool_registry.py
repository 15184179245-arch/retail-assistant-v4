"""Fixed, scoped task tools for the first Agent workflow.

The caller owns the transaction. A rejected call leaves a staged audit event;
its caller must commit that event even though the tool raises ToolRegistryError.
No model supplied field selects a scope, principal, or arbitrary handler.

AGT-027: ``TOOLS``/``JARVIS_TOOLS``/evidence dictionaries remain import-compatible
views, but ``resolve_tool`` is the only lookup entry used by the synchronous
service, the worker, confirmations and routing.  ``CAPABILITY_REGISTRY`` is the
validated union of those views.  Every model-visible definition has exactly one
fixed handler, explicit input/output schemas and frozen metadata.  Dynamic
legacy code definitions remain metadata-only. Neither direct registration nor
pre-existing dynamic handlers may execute; AGT-033/034 own the safe replacement.
"""

from dataclasses import dataclass
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import re
from typing import Any, Callable, Literal
from types import MappingProxyType
from backend.app.modules.agents import capability_discovery as discovery
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents.tools import guanjia as guanjia_tool
from backend.app.modules.agents.tools import catalog as catalog_tool
from backend.app.modules.agents.tools import memory as memory_tool
from backend.app.modules.agents.tools import stocktake as stocktake_tool
from backend.app.modules.agents.tools import purchasing as purchasing_tool
from backend.app.modules.agents.tools import sandbox as sandbox_tool
from backend.app.modules.agents.tools import tool_maker as tool_maker_tool
from backend.app.modules.knowledge import p1c as knowledge_p1c
from backend.app.modules.memory import recall as memory_recall, service as memory_service
from backend.app.modules.memory.schemas import MemorySearchPage
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.tasks.repository import list_tasks_visible
from backend.app.modules.tasks.service import create_task
from backend.app.application import commit_guard
from backend.app.modules.workspace import service as workspace_service
from backend.app.modules.workspace.schemas import ArtifactCreateRequest, ArtifactPatchRequest, ArtifactRecord, ArtifactOpRecord
from backend.app.application.dispatch import execute_agent_suggestion, read_agent_tasks_mine
from backend.app.modules.identity.command_guard import lock_command_authority, check_command_authority, refresh_authority


@dataclass(frozen=True)
class NoEffectToolResult:
    """Constructed solely by the authorized, before-handler preflight branch."""
    payload: dict


class ToolRegistryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _schema_sha256(model: type[BaseModel]) -> str:
    return sha256(_canonical(model.model_json_schema()).encode("utf-8")).hexdigest()


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListMineInput(_Input):
    limit: int = Field(default=20, ge=1, le=20)


class ArtifactReadInput(_Input):
    artifact_id: str = Field(min_length=1, max_length=64)


class ArtifactCreateInput(ArtifactCreateRequest):
    pass


class ArtifactReadOutput(BaseModel):
    """Single-artifact read: the record page plus its operation history."""

    model_config = ConfigDict(extra="forbid")
    artifacts: list[ArtifactRecord]
    ops: list[ArtifactOpRecord] = Field(default_factory=list)


class ArtifactMutationOutput(BaseModel):
    """Mutation receipt for model callers: record + lock-ceiling outcome."""

    model_config = ConfigDict(extra="forbid")
    artifact: ArtifactRecord
    skipped_fields: list[str] = Field(default_factory=list)
    pending_suggestions_count: int = 0
    idempotent_replay: bool = False


class ArtifactPatchInput(ArtifactPatchRequest):
    artifact_id: str = Field(min_length=1, max_length=64)


class SuggestReviewInput(_Input):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    reason: str = Field(min_length=1, max_length=4000)
    reviewer_id: str = Field(min_length=1, max_length=36)
    proposal_ref: str | None = Field(default=None, max_length=200)


class TaskSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    state: str
    due_at: datetime | None


class ListMineOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[TaskSummary]


class CatalogSearchInput(catalog_tool.CatalogSearchInput):
    pass


class CatalogSearchOutput(catalog_tool.CatalogSearchOutput):
    pass


class KnowledgeSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=5, ge=1, le=20)


class KnowledgeSearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    citations: list[dict]
    insufficient: bool
    reason: str | None
    answer: None = None


class MemorySearchInput(_Input):
    query: str = Field(min_length=1, max_length=256)
    limit: int = Field(default=5, ge=1, le=5)


class SuggestReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    state: Literal["suggested"]
    reviewer_id: str


class DynamicToolInput(BaseModel):
    """Opaque argument bag for a registered dynamic tool.

    The dynamic tool's own ``run(arguments)`` contract decides the fields; the
    registry still validates that the value is a JSON object and never lets a
    model select a principal, scope or handler.
    """

    model_config = ConfigDict(extra="allow")


class DynamicToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    stdout: str = ""
    result: Any | None = None
    error: str | None = None


@dataclass(frozen=True)
class ToolExecution:
    """Handler return value; the registry validates it against output_model."""

    result: BaseModel
    audit_key: str | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class ToolDefinition:
    key: str
    description: str
    version: str
    requester_permission: str
    agent_permission: str | None
    mode: Literal["read", "write"]
    timeout_seconds: int
    idempotency: Literal["none", "required"]
    confirmation: Literal["none", "reviewer_decides_later"]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler_id: str = ""
    side_effect: Literal["read_only", "business_write", "capability_write"] = "read_only"
    organization_scope: str = "authenticated_organization"
    store_scope: str = "requester_store"
    audit_action: str = "agent_tool.invoked"
    model_visible: bool = True
    host_only: bool = False
    idempotency_scope: str = "requester_agent_tool"
    extra_requester_permissions: tuple[str, ...] = ()
    input_sha256: str = ""
    output_sha256: str = ""
    # Write tools default to a registry savepoint so output-contract validation
    # and the tool audit stay atomic with the business write.  Tools whose
    # service enforces its own physical-root create contract (workspace
    # persistence.begin_create refuses SAVEPOINT callers) set savepoint=False;
    # their mutation atomicity is owned by the service's nested savepoint,
    # matching the workspace router's _run_write semantics.
    savepoint: bool = True

    def __post_init__(self) -> None:
        if self.host_only and self.model_visible:
            raise RuntimeError(f"tool_registry_invalid:host_only:{self.key}")
        if not self.handler_id:
            raise RuntimeError(f"tool_registry_invalid:handler_missing:{self.key}")
        object.__setattr__(self, "input_sha256", _schema_sha256(self.input_model))
        object.__setattr__(self, "output_sha256", _schema_sha256(self.output_model))


_SAFE_AUDIT_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")
_TASK_ERROR_CODES = {
    "task_reviewer_invalid": "tool_reviewer_invalid",
    "idempotency_conflict": "tool_idempotency_conflict",
    "task_conflict": "tool_task_conflict",
}


def _audit_label(value: object) -> str:
    if isinstance(value, str) and _SAFE_AUDIT_LABEL.fullmatch(value):
        return value
    return "invalid:" + sha256(repr(value).encode("utf-8", errors="replace")).hexdigest()[:16]


def scoped_task_tool_key(requester_id: str, agent_id: str, tool_key: str,
                         version: str, idempotency_key: str) -> str:
    """Stable key shared by task execution and crash reconciliation."""
    return "agent_tool:" + sha256(json.dumps([
        requester_id, agent_id, tool_key, version, idempotency_key.strip(),
    ], separators=(",", ":")).encode("utf-8")).hexdigest()


def _audit(session: Session, requester: AuthenticatedSubject, *, action: str,
           tool_key: object, version: object, code: str, key: str | None = None,
           task_id: str | None = None) -> None:
    digest = sha256(key.encode("utf-8")).hexdigest() if key else str(uuid4())
    tool_label, version_label = _audit_label(tool_key), _audit_label(version)
    record_event(session, actor_id=requester.subject_id,
        organization_id=requester.organization_id, store_id=requester.store_id,
        action=action, target_type="agent_tool", target_id=tool_label,
        idempotency_key=f"agent_tool:{action}:{digest}",
        details={"tool_key": tool_label, "version": version_label,
                 "code": code, "task_id": task_id})


def _person_scope(session: Session, requester: AuthenticatedSubject, *, now=None) -> None:
    if requester.account_id is None or requester.session_id is None:
        raise ToolRegistryError("tool_login_required")
    with session.no_autoflush:
        try:
            refresh_authority(session)
            authorize_scope(session, requester, organization_id=requester.organization_id,
                            store_id=requester.store_id, now=now)
        except HTTPException as exc:
            raise ToolRegistryError("tool_login_invalid") from exc
        kind = session.scalar(select(Subject.subject_type).where(
            Subject.organization_id == requester.organization_id,
            Subject.id == requester.subject_id))
    if kind != "person":
        raise ToolRegistryError("tool_person_required")


def _agent_scope(session: Session, requester: AuthenticatedSubject,
                 agent: AuthenticatedSubject, permission: str, *, now=None) -> None:
    if (agent.organization_id, agent.store_id) != (requester.organization_id, requester.store_id):
        raise ToolRegistryError("tool_scope_denied")
    if agent.account_id is not None or agent.session_id is not None:
        raise ToolRegistryError("tool_agent_identity_invalid")
    kind = session.scalar(select(Subject.subject_type).where(
        Subject.organization_id == agent.organization_id, Subject.id == agent.subject_id))
    if kind != "agent":
        raise ToolRegistryError("tool_agent_identity_invalid")
    try:
        authorize_scope(session, agent, organization_id=agent.organization_id,
                        store_id=agent.store_id, permission=permission, now=now)
    except HTTPException as exc:
        raise ToolRegistryError("tool_agent_permission_denied") from exc


# ---------------------------------------------------------------------------
# Fixed handlers.  A handler is selected only by the frozen definition's
# handler_id; the model never supplies one.
# ---------------------------------------------------------------------------


def _invoke_artifact_read(session: Session, requester: AuthenticatedSubject,
                          agent: AuthenticatedSubject, payload: BaseModel,
                          idempotency_key: str | None) -> ToolExecution:
    detail = workspace_service.get_artifact_detail(session, requester, payload.artifact_id)
    result = ArtifactReadOutput(artifacts=[detail.artifact], ops=detail.ops)
    return ToolExecution(result=result)


def _invoke_artifact_create(session: Session, requester: AuthenticatedSubject,
                            agent: AuthenticatedSubject, payload: BaseModel,
                            idempotency_key: str | None) -> ToolExecution:
    key = scoped_task_tool_key(requester.subject_id, agent.subject_id,
                               "artifact.create", "1", idempotency_key or str(uuid4()))
    # Model callers act as agents: never inherit or claim human authority.
    req = payload.model_copy(update={"actor_type": "agent"})
    mutation = workspace_service.create_artifact(session, requester, req, header_idempotency_key=key)
    result = ArtifactMutationOutput(
        artifact=mutation.artifact,
        skipped_fields=mutation.skipped_fields,
        pending_suggestions_count=len(mutation.artifact.pending_suggestions),
        idempotent_replay=mutation.idempotent_replay,
    )
    return ToolExecution(result=result, audit_key=key)


def _invoke_artifact_patch(session: Session, requester: AuthenticatedSubject,
                           agent: AuthenticatedSubject, payload: BaseModel,
                           idempotency_key: str | None) -> ToolExecution:
    key = scoped_task_tool_key(requester.subject_id, agent.subject_id,
                               "artifact.patch", "1", idempotency_key or str(uuid4()))
    # Model callers act as agents: never inherit or claim human authority,
    # so human-locked fields are skipped and routed to suggestions.
    req = payload.model_copy(update={"actor_type": "agent"})
    mutation = workspace_service.patch_artifact(session, requester, payload.artifact_id, req, header_idempotency_key=key)
    result = ArtifactMutationOutput(
        artifact=mutation.artifact,
        skipped_fields=mutation.skipped_fields,
        pending_suggestions_count=len(mutation.artifact.pending_suggestions),
        idempotent_replay=mutation.idempotent_replay,
    )
    return ToolExecution(result=result, audit_key=key)


def _invoke_list_mine(session: Session, requester: AuthenticatedSubject,
                      agent: AuthenticatedSubject, payload: BaseModel,
                      idempotency_key: str | None) -> ToolExecution:
    tasks = read_agent_tasks_mine(session, requester=requester, limit=payload.limit)
    result = ListMineOutput(tasks=[TaskSummary(id=task.id, title=task.title,
        state=task.state, due_at=task.due_at) for task in tasks])
    return ToolExecution(result=result)


def _invoke_knowledge_search(session: Session, requester: AuthenticatedSubject,
                             agent: AuthenticatedSubject, payload: BaseModel,
                             idempotency_key: str | None) -> ToolExecution:
    # Explicit Host/profile routing.  The Pi Host sets this marker only after
    # an explicit runtime profile selects the staged projection path; the
    # historical non-Pi knowledge.search.v1 callers never carry it and keep
    # their original materialised contract.  There is no implicit "projection
    # exists -> new path" switch and no legacy fallback for the Pi path.
    profile = session.info.get('knowledge_projection_profile')
    prepared = session.info.get('p1c_prepared')
    if profile in PI_ALL_PROFILES and prepared is not None:
        from backend.app.modules.knowledge import p1c as knowledge_p1c
        from backend.app.modules.knowledge import projection as knowledge_projection
        try:
            result = knowledge_p1c.finalize_prepared(
                None, prepared, requester, result=session.info.get('p1c_worker'),
                limit=payload.limit,
                deadline=session.info.get('p1c_deadline_monotonic'),
                cancel_event=session.info.get('p1c_stop_event'),
                root_session=session)
        except knowledge_projection.KnowledgeProjectionUnavailable as error:
            raise ToolRegistryError('knowledge_projection_unavailable') from error
        if result.get('state') != 'ready':
            # Stale/insufficient-evidence identity is an explicit error state,
            # never an empty "no match" and never a silent legacy fallback.
            raise ToolRegistryError('knowledge_projection_unavailable')
        citations = list(result.get('citations') or [])
        return ToolExecution(result=KnowledgeSearchOutput(
            query=payload.query, citations=citations,
            insufficient=not citations,
            reason=None if citations else 'insufficient_evidence'))
    if profile is not None:
        # A Host selected the projection path but the staged inputs are
        # absent: fail closed rather than falling back to legacy materialise.
        raise ToolRegistryError('knowledge_projection_unavailable')
    from backend.app.modules.knowledge import retrieval as legacy_retrieval
    legacy = legacy_retrieval.search_knowledge(
        session, requester, query=payload.query, limit=payload.limit)
    citations = list(legacy.get('citations') or [])
    return ToolExecution(result=KnowledgeSearchOutput(
        query=payload.query, citations=citations,
        insufficient=not citations,
        reason=legacy.get('reason') if citations else 'insufficient_evidence'))


def _invoke_catalog_search(session: Session, requester: AuthenticatedSubject,
                           agent: AuthenticatedSubject, payload: BaseModel,
                           idempotency_key: str | None) -> ToolExecution:
    return ToolExecution(result=catalog_tool.search(session, requester, payload))


def _invoke_memory_search(session: Session, requester: AuthenticatedSubject,
                          agent: AuthenticatedSubject, payload: BaseModel,
                          idempotency_key: str | None) -> ToolExecution:
    try:
        result = memory_recall.search_memories(session, requester, query=payload.query, limit=payload.limit)
    except memory_service.MemoryError as error:
        raise ToolRegistryError(error.code) from error
    return ToolExecution(result=MemorySearchPage(**result))


def _memory_writer(operation):
    def invoke(session, requester, agent, payload, idempotency_key):
        key = scoped_task_tool_key(requester.subject_id, agent.subject_id,
                                   'memory.' + operation, '1', idempotency_key)
        try:
            result = memory_tool.execute(session, requester, agent, operation=operation,
                                         payload=payload, command_key=key)
        except memory_service.MemoryError as error:
            raise ToolRegistryError(error.code) from error
        return ToolExecution(result=result, audit_key=key)
    return invoke


def _invoke_suggest_review(session: Session, requester: AuthenticatedSubject,
                           agent: AuthenticatedSubject, payload: BaseModel,
                           idempotency_key: str | None) -> ToolExecution:
    # The Agent identity is real and independently authorized; the requester is
    # revalidated by the registry and recorded in the tool audit.
    scoped_key = scoped_task_tool_key(requester.subject_id, agent.subject_id,
        "tasks.suggest_review", "1", idempotency_key)
    task = execute_agent_suggestion(session, requester=requester, agent=agent,
        payload=payload.model_dump(), idempotency_key=scoped_key).data
    # This is the creation receipt. A replay can return the same task after its
    # reviewer has already changed the current task state.
    result = SuggestReviewOutput(task_id=task.id, state="suggested",
                                  reviewer_id=task.reviewer_id)
    return ToolExecution(result=result, audit_key=scoped_key, task_id=task.id)


def _invoke_sandbox(session: Session, requester: AuthenticatedSubject,
                    agent: AuthenticatedSubject, payload: BaseModel,
                    idempotency_key: str | None) -> ToolExecution:
    return ToolExecution(result=sandbox_tool.execute_python_sandbox(session, requester, payload))


def _invoke_create_tool(session: Session, requester: AuthenticatedSubject,
                        agent: AuthenticatedSubject, payload: BaseModel,
                        idempotency_key: str | None) -> ToolExecution:
    return ToolExecution(result=tool_maker_tool.create_dynamic_tool(session, requester, payload))


def _invoke_stocktake_evidence(session: Session, requester: AuthenticatedSubject,
                               agent: AuthenticatedSubject, payload: BaseModel,
                               idempotency_key: str | None) -> ToolExecution:
    return ToolExecution(result=stocktake_tool.read_evidence(session, requester, payload))


def _invoke_purchasing_extract(session: Session, requester: AuthenticatedSubject,
                               agent: AuthenticatedSubject, payload: BaseModel,
                               idempotency_key: str | None) -> ToolExecution:
    return ToolExecution(result=purchasing_tool.extract_candidates(session, requester, payload))


def _invoke_purchasing_explain(session: Session, requester: AuthenticatedSubject,
                               agent: AuthenticatedSubject, payload: BaseModel,
                               idempotency_key: str | None) -> ToolExecution:
    return ToolExecution(result=purchasing_tool.explain_discrepancies(session, requester, payload))


def _dynamic_handler(store_id: str, tool_key: str, version: str) -> Callable[..., ToolExecution]:
    def invoke(session: Session, requester: AuthenticatedSubject,
               agent: AuthenticatedSubject, payload: BaseModel,
               idempotency_key: str | None) -> ToolExecution:
        raise ToolRegistryError(sandbox_tool.LEGACY_CODE_DISABLED)
    return invoke


def _invoke_discovery_unbound(*_args, **_kwargs):
    raise ToolRegistryError("agent_pi_discovery_host_required")


def _invoke_source_unbound(*_args, **_kwargs):
    raise ToolRegistryError("guanjia_source_process_required")


def _invoke_guanjia_unbound(*_args, **_kwargs):
    raise ToolRegistryError('guanjia_host_binding_required')


_HANDLERS: dict[str, Callable[..., ToolExecution]] = {
    "runtime.tool_search.v1": _invoke_discovery_unbound,
    "guanjia.read_prices.v1": _invoke_source_unbound,
    "guanjia.read_stock.v1": _invoke_source_unbound,
    "guanjia.category_discovery.v1": _invoke_source_unbound,
    "guanjia.category_products.v1": _invoke_source_unbound,
    "guanjia.category_products_status.v1": _invoke_source_unbound,
    "guanjia.observe_prices.v1": _invoke_guanjia_unbound,
    "guanjia.observe_stock.v1": _invoke_guanjia_unbound,
    "tasks.list_mine.v1": _invoke_list_mine,
    "artifact.read.v1": _invoke_artifact_read,
    "artifact.create.v1": _invoke_artifact_create,
    "artifact.patch.v1": _invoke_artifact_patch,
    "knowledge.search.v1": _invoke_knowledge_search,
    "memory.search.v1": _invoke_memory_search,
    "memory.save.v1": _memory_writer('save'),
    "memory.correct.v1": _memory_writer('correct'),
    "memory.withdraw.v1": _memory_writer('withdraw'),
    "catalog.search.v1": _invoke_catalog_search,
    "tasks.suggest_review.v1": _invoke_suggest_review,
    "sandbox.python_exec.v1": _invoke_sandbox,
    "tools.create_tool.v1": _invoke_create_tool,
    "stocktake.read_evidence.v1": _invoke_stocktake_evidence,
    "purchasing.extract_candidates.v1": _invoke_purchasing_extract,
    "purchasing.explain_discrepancies.v1": _invoke_purchasing_explain,
}


TOOLS = {
    ("memory.save", "1"): ToolDefinition(
        key="memory.save", version="1", description="将稳定偏好或已核经验保存到当前用户共享记忆，保留至撤回；不保存凭据，不授业务权限。",
        requester_permission="memory.write", agent_permission="agents.memory.save",
        mode="write", timeout_seconds=5, idempotency="required", confirmation="none",
        input_model=memory_tool.SaveInput, output_model=memory_tool.WriteReceipt,
        handler_id="memory.save.v1", side_effect="business_write", extra_requester_permissions=("memory.read",)),
    ("memory.correct", "1"): ToolDefinition(
        key="memory.correct", version="1", description="按已检索的记忆ID和版本纠正当前用户共享记忆；版本冲突须重新检索，不猜ID或覆盖新版。",
        requester_permission="memory.write", agent_permission="agents.memory.correct",
        mode="write", timeout_seconds=5, idempotency="required", confirmation="none",
        input_model=memory_tool.CorrectInput, output_model=memory_tool.WriteReceipt,
        handler_id="memory.correct.v1", side_effect="business_write", extra_requester_permissions=("memory.read",)),
    ("memory.withdraw", "1"): ToolDefinition(
        key="memory.withdraw", version="1", description="按明确记忆ID和版本撤回当前用户共享记忆，停止后续召回；不承诺擦除已下载副本或全部历史记录。",
        requester_permission="memory.write", agent_permission="agents.memory.withdraw",
        mode="write", timeout_seconds=5, idempotency="required", confirmation="none",
        input_model=memory_tool.WithdrawInput, output_model=memory_tool.WriteReceipt,
        handler_id="memory.withdraw.v1", side_effect="business_write", extra_requester_permissions=("memory.read",)),
    ("memory.search", "1"): ToolDefinition(
        key="memory.search", version="1",
        description="检索当前员工自己的有效记忆；偏好、先前约定或经验应先按关键词召回。返回内容仅供参考，不能作为授权、任务完成或当前库存价格的证明；无命中时不得编造记忆。",
        requester_permission="memory.read", agent_permission="agents.memory.search",
        mode="read", timeout_seconds=3, idempotency="none", confirmation="none",
        input_model=MemorySearchInput, output_model=MemorySearchPage, handler_id="memory.search.v1",
    ),
    ("tasks.list_mine", "1"): ToolDefinition(
        key="tasks.list_mine", description="读取当前员工本人任务摘要。", version="1", requester_permission="tasks.read_self",
        agent_permission="agents.tasks.list_mine", mode="read", timeout_seconds=3,
        idempotency="none", confirmation="none", input_model=ListMineInput,
        output_model=ListMineOutput, handler_id="tasks.list_mine.v1",
    ),
    ("knowledge.search", "1"): ToolDefinition(
        key="knowledge.search", description="检索已审核且未失效的知识并返回引用；缺少证据时不编造答案。", version="1", requester_permission="knowledge.read",
        agent_permission="agents.knowledge.search", mode="read", timeout_seconds=3,
        idempotency="none", confirmation="none", input_model=KnowledgeSearchInput,
        output_model=KnowledgeSearchOutput, handler_id="knowledge.search.v1",
    ),
    ("catalog.search", "1"): ToolDefinition(
        key="catalog.search", description="按原始编号/条码精确找货；口语或候选过多时传plan：keyword_groups组间AND组内OR同义词，spec_terms仅用户明确规格，不猜尺寸/材质等级。按当前门店来源过滤并可融合语义候选；结果不局限旧1000条，商品身份与规格须员工确认。", version="1", requester_permission="catalog.read",
        agent_permission="agents.catalog.search", mode="read", timeout_seconds=12,
        idempotency="none", confirmation="none", input_model=CatalogSearchInput,
        output_model=CatalogSearchOutput, handler_id="catalog.search.v1",
    ),
    ("tasks.suggest_review", "1"): ToolDefinition(
        key="tasks.suggest_review", description="提出待指定负责人审阅的任务建议；经发起人确认后仅创建suggested，不接受或分派。", version="1", requester_permission="tasks.suggest",
        agent_permission="agents.tasks.suggest_review", mode="write", timeout_seconds=5,
        idempotency="required", confirmation="reviewer_decides_later",
        input_model=SuggestReviewInput, output_model=SuggestReviewOutput,
        handler_id="tasks.suggest_review.v1", side_effect="business_write",
    ),
}

# Workspace artifact tools (AGENT-02): the definitions are fixed by this same
# single registry (no parallel registry exists), but they are NOT part of the
# ordinary business catalog.  resolve_tool() never resolves them; without an
# authenticated Work context they do not enter a Run's granted tools.  Only
# workbench-profile paths with an explicitly selected artifact pair resolve
# through the registry-resident group below.  Frozen authority contract: the
# machine agent needs identity only (agent_permission None, same as the
# structural runtime.tool_search); artifact authority flows through the
# requester's explicit Work delegation plus the workspace kind permissions,
# never through a standing machine-agent grant.
WORKSPACE_TOOLS = {
    ("artifact.read", "1"): ToolDefinition(
        key="artifact.read", description="按 Artifact ID 读取当前工作区工件内容及操作历史。", version="1", requester_permission="agents.run",
        agent_permission=None, mode="read", timeout_seconds=5,
        idempotency="none", confirmation="none", input_model=ArtifactReadInput,
        output_model=ArtifactReadOutput, handler_id="artifact.read.v1",
    ),
    ("artifact.create", "1"): ToolDefinition(
        key="artifact.create", description="在指定线程内创建新工件，自动设置初始版本。", version="1", requester_permission="agents.run",
        agent_permission=None, mode="write", timeout_seconds=5,
        idempotency="required", confirmation="none", input_model=ArtifactCreateInput,
        output_model=ArtifactMutationOutput, handler_id="artifact.create.v1", side_effect="business_write",
        savepoint=False,
    ),
    ("artifact.patch", "1"): ToolDefinition(
        key="artifact.patch", description="应用增量补丁至现有工件，需提供基准版本。", version="1", requester_permission="agents.run",
        agent_permission=None, mode="write", timeout_seconds=5,
        idempotency="required", confirmation="none", input_model=ArtifactPatchInput,
        output_model=ArtifactMutationOutput, handler_id="artifact.patch.v1", side_effect="business_write",
        savepoint=False,
    ),
}

JARVIS_TOOLS = {
    ("sandbox.python_exec", "1"): ToolDefinition(
        key="sandbox.python_exec",
        description="贾维斯代码沙箱：当没有现成工具或需复杂多维分析、统计、过滤、数学计算、格式提取与整理时，直接编写并执行Python脚本得出精确结论。支持内置函数、math/json/re/datetime/statistics及search_catalog、query_db只读查询。",
        version="1",
        requester_permission="agents.run",
        agent_permission="agents.run",
        mode="read",
        timeout_seconds=10,
        idempotency="none",
        confirmation="none",
        input_model=sandbox_tool.PythonExecInput,
        output_model=sandbox_tool.PythonExecOutput,
        handler_id="sandbox.python_exec.v1",
        idempotency_scope="none",
    ),
    ("tools.create_tool", "1"): ToolDefinition(
        key="tools.create_tool",
        description="贾维斯工具创造器：没有工具就创造工具！当发现高频、可复用的业务处理或分析流程时，编写一段Python函数自创并沉淀为一个新工具，经沙箱校验后永久注册进系统工具库，后续所有交互均可直接调用该工具。",
        version="1",
        requester_permission="agents.run",
        agent_permission="agents.run",
        mode="write",
        timeout_seconds=10,
        idempotency="none",
        confirmation="none",
        input_model=tool_maker_tool.CreateToolInput,
        output_model=tool_maker_tool.CreateToolOutput,
        handler_id="tools.create_tool.v1",
        side_effect="capability_write",
        idempotency_scope="none",
    ),
}

# AGT-014 internal gateway extension: host-only, intentionally NOT added to
# TOOLS, registered-tools.json or model-visible descriptions.  They still live
# in the same validated registry so their key/version/handler cannot drift.
STOCKTAKE_EVIDENCE_TOOLS = {
    ("stocktake.read_evidence", "1"): ToolDefinition(
        key="stocktake.read_evidence",
        description="读取同店局部盘点观察、照片哈希、人工包装换算和库位存在断言；绝不报告账面库存。",
        version="1", requester_permission="stocktake.read",
        agent_permission="agents.stocktake.read_evidence",
        mode="read", timeout_seconds=8, idempotency="none",
        confirmation="none", input_model=stocktake_tool.EvidenceInput,
        output_model=stocktake_tool.EvidenceOutput,
        handler_id="stocktake.read_evidence.v1",
        model_visible=False, host_only=True, idempotency_scope="none",
        # Placement assertions are location data with an independent read
        # capability; stocktake.read alone must not disclose locations.
        extra_requester_permissions=("locations.read",),
    ),
}

# AGT-015 host-only purchasing evidence tools. NEVER add them to TOOLS,
# model_tool_descriptions, shop-assistant.json or registered-tools.json:
# executable model ToolCalls need their own approved contract card.
PURCHASING_EVIDENCE_TOOLS = {
    ("purchasing.extract_candidates", "1"): ToolDefinition(
        key="purchasing.extract_candidates", version="1",
        description="仅解析员工已授权上传的采购意向原文位置和候选数量，商品/供应商须人工确认。",
        requester_permission="purchasing.read",
        agent_permission="agents.purchasing.read_evidence",
        mode="read", timeout_seconds=8, idempotency="none",
        confirmation="none", input_model=purchasing_tool.PurchaseEvidenceInput,
        output_model=purchasing_tool.PurchaseEvidenceOutput,
        handler_id="purchasing.extract_candidates.v1",
        model_visible=False, host_only=True, idempotency_scope="none",
    ),
    ("purchasing.explain_discrepancies", "1"): ToolDefinition(
        key="purchasing.explain_discrepancies", version="1",
        description="解释五类已记载的到货差异及逐批人工裁决，绝不入库、改价或下单。",
        requester_permission="purchasing.read",
        agent_permission="agents.purchasing.read_evidence",
        mode="read", timeout_seconds=8, idempotency="none",
        confirmation="none", input_model=purchasing_tool.ReceivingExplanationInput,
        output_model=purchasing_tool.ReceivingExplanationOutput,
        handler_id="purchasing.explain_discrepancies.v1",
        model_visible=False, host_only=True, idempotency_scope="none",
        extra_requester_permissions=("receiving.read",),
    ),
}


GUANJIA_HOST_TOOLS = {
    ("guanjia.observe_prices", "1"): ToolDefinition(
        key="guanjia.observe_prices", version="1", description="同店已确认商品价格的受控Host观察；真实字段未核则HOLD。",
        requester_permission="guanjia.pricing.read", agent_permission="agents.guanjia.prices.read",
        mode="read", timeout_seconds=30, idempotency="none", confirmation="none",
        input_model=guanjia_tool.PriceInput, output_model=guanjia_tool.ObservationOutput,
        handler_id="guanjia.observe_prices.v1", model_visible=False, host_only=True,
        idempotency_scope="none", extra_requester_permissions=("guanjia.company.read",)),
    ("guanjia.observe_stock", "1"): ToolDefinition(
        key="guanjia.observe_stock", version="1", description="同店已确认商品库存的受控Host观察；未知库存不当零。",
        requester_permission="guanjia.inventory.read", agent_permission="agents.guanjia.stock.read",
        mode="read", timeout_seconds=30, idempotency="none", confirmation="none",
        input_model=guanjia_tool.StockInput, output_model=guanjia_tool.ObservationOutput,
        handler_id="guanjia.observe_stock.v1", model_visible=False, host_only=True,
        idempotency_scope="none"),
}

class SourceProductInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    product_id: str = Field(min_length=1, max_length=120,
        description='Exact local catalog product ID returned by catalog.search; never a name or guessed vendor ID.')


class SourceObservationOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    observation_id: str
    observation: dict


GUANJIA_SOURCE_TOOLS = {
    ('guanjia.read_' + operation, '1'): ToolDefinition(
        key='guanjia.read_' + operation, version='1', description=description,
        requester_permission=permission, agent_permission='agents.guanjia.'+operation+'.read',
        mode='read', timeout_seconds=45, idempotency='none', confirmation='none',
        input_model=SourceProductInput, output_model=SourceObservationOutput,
        handler_id='guanjia.read_'+operation+'.v1', model_visible=False, host_only=True,
        idempotency_scope='none', extra_requester_permissions=extra)
    for operation, description, permission, extra in (
        ('prices', '读取管家婆当前商品零售价格字段。先catalog.search确定product_id；保留规格和原值，未知币税不推断，不作正式报价。',
         'guanjia.pricing.read', ('guanjia.company.read',)),
        ('stock', '读取管家婆当前商品库存字段。先catalog.search确定product_id；绑定门店仓库，空响应不当零，不作可售承诺。',
         'guanjia.inventory.read', ()))
}


class CategoryDiscoveryInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str = Field(min_length=1,max_length=200)
    limit: int = Field(ge=1,le=100)


class CategoryProductsInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    category_business_id: str = Field(min_length=1,max_length=200)
    limit: int = Field(ge=1,le=100)
    offset: int = Field(ge=0,le=1000000)


class CategoryResultOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    category_receipt_id: str
    category_result: dict


CATEGORY_STATUS_CONTRACT = 'v4.guanjia-category/2'


class CategoryStatusItemOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    product_id: str
    external_code: str
    category_business_id: str
    snapshot_id: str
    snapshot_sha256: str
    product_version: int
    upstream_status: Literal['enabled', 'disabled', 'unknown']
    upstream_status_reason: Literal['missing', 'null', 'invalid_type'] | None


class CategoryStatusCountsOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: int = Field(ge=0)
    disabled: int = Field(ge=0)
    unknown: int = Field(ge=0)


class CategoryStatusPageOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    category_business_id: str
    descendant_business_ids: list[str]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0, le=1000000)
    items: list[CategoryStatusItemOutput]
    candidate_contract: Literal['v4.guanjia-category-status/1']
    authority: Literal['local_source_snapshot_only']
    total_semantics: Literal['local_active_catalog_records']
    upstream_status_counts: CategoryStatusCountsOutput


class CategoryStatusValueOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    contract: Literal['v4.guanjia-category/2']
    authority: Literal['local_source_snapshot_only']
    operation: Literal['category_products']
    arguments: CategoryProductsInput
    directory_artifact_sha256: str
    directory_content_sha256: str
    directory_observed_at: str
    data: CategoryStatusPageOutput


class CategoryStatusResultOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    category_receipt_id: str
    category_result: CategoryStatusValueOutput


GUANJIA_CATEGORY_TOOLS = {
    ('guanjia.'+operation,'1'):ToolDefinition(key='guanjia.'+operation,version='1',
        description=description,requester_permission='catalog.read',agent_permission='agents.catalog.search',
        mode='read',timeout_seconds=30,idempotency='none',confirmation='none',
        input_model=model,output_model=CategoryResultOutput,handler_id='guanjia.'+operation+'.v1',
        model_visible=False,host_only=True,idempotency_scope='none')
    for operation,model,description in (
        ('category_discovery',CategoryDiscoveryInput,'查找管家婆分类业务ID、父子关系和歧义候选；本地来源快照，不暗选首项。'),
        ('category_products',CategoryProductsInput,'按精确分类业务ID及子类分页查询本地商品快照，返回完整总量及来源引用；非实时上游。'))
}
# A new identity, not a silent revision of the persisted v1 definition.
GUANJIA_CATEGORY_TOOLS[('guanjia.category_products_status', '1')] = ToolDefinition(
    key='guanjia.category_products_status', version='1',
    description='按分类业务ID查询本地来源快照，区分本地有效目录总数与原始启用/停用/未知三态；全量计数跨页保持，非实时上游。',
    requester_permission='catalog.read', agent_permission='agents.catalog.search',
    mode='read', timeout_seconds=30, idempotency='none', confirmation='none',
    input_model=CategoryProductsInput, output_model=CategoryStatusResultOutput,
    handler_id='guanjia.category_products_status.v1', model_visible=False,
    host_only=True, idempotency_scope='none')



RUNTIME_METADATA_TOOLS = {
    (discovery.KEY, "1"): ToolDefinition(
        key=discovery.KEY, version="1", description=discovery.DESCRIPTION,
        requester_permission="agents.run", agent_permission=None,
        mode="read", timeout_seconds=5, idempotency="none", confirmation="none",
        input_model=discovery.SearchInput, output_model=discovery.SearchOutput,
        handler_id="runtime.tool_search.v1", host_only=True, model_visible=False),
}

from . import research_runtime, sales_ranking_runtime, sales_product_runtime, local_sales_runtime
RESEARCH_TOOLS = {
    ('research.'+op,'1'): ToolDefinition(key='research.'+op,version='1',
        description=research_runtime.DESCRIPTIONS[op],requester_permission='knowledge.read',
        agent_permission='agents.guanjia.research.read' if op=='probe' else 'agents.knowledge.search',mode='read',timeout_seconds=45 if op=='probe' else 30,
        idempotency='none',confirmation='none',input_model=model,output_model=research_runtime.ProbeOutput if op=='probe' else research_runtime.Output,
        handler_id='research.'+op+'.v1',model_visible=False,host_only=True,idempotency_scope='none',
        extra_requester_permissions=('guanjia.company.read',) if op=='probe' else ())
    for op,model in research_runtime.INPUTS.items()}
for _key,_version in RESEARCH_TOOLS:
    _HANDLERS[_key+'.v'+_version]=_invoke_source_unbound

GUANJIA_SALES_TOOLS = {
    (sales_ranking_runtime.KEY, '1'): ToolDefinition(
        key=sales_ranking_runtime.KEY, version='1', description=sales_ranking_runtime.DESCRIPTION,
        requester_permission='guanjia.sales.read', agent_permission='agents.guanjia.sales.read',
        mode='read', timeout_seconds=120, idempotency='none', confirmation='none',
        input_model=sales_ranking_runtime.Input, output_model=sales_ranking_runtime.Output,
        handler_id='guanjia.sales_ranking.read.v1', model_visible=False, host_only=True,
        idempotency_scope='none', extra_requester_permissions=('guanjia.company.read',))}
_HANDLERS['guanjia.sales_ranking.read.v1'] = _invoke_source_unbound
GUANJIA_SALES_TOOLS[(sales_product_runtime.KEY, '2')] = ToolDefinition(
    key=sales_product_runtime.KEY, version='2', description=sales_product_runtime.DESCRIPTION,
    requester_permission='guanjia.sales.read', agent_permission='agents.guanjia.sales.read',
    mode='read', timeout_seconds=120, idempotency='none', confirmation='none',
    input_model=sales_product_runtime.Input, output_model=sales_product_runtime.Output,
    handler_id='guanjia.sales_ranking.read.v2', model_visible=False, host_only=True,
    idempotency_scope='none', extra_requester_permissions=('guanjia.company.read',))
_HANDLERS['guanjia.sales_ranking.read.v2'] = _invoke_source_unbound
GUANJIA_SALES_TOOLS[(local_sales_runtime.KEY, '3')] = ToolDefinition(
    key=local_sales_runtime.KEY,version='3',description=local_sales_runtime.DESCRIPTION,
    requester_permission='guanjia.sales.read',agent_permission='agents.guanjia.sales.read',
    mode='read',timeout_seconds=120,idempotency='none',confirmation='none',
    input_model=local_sales_runtime.Input,output_model=local_sales_runtime.Output,
    handler_id='guanjia.sales_ranking.read.v3',model_visible=False,host_only=True,
    idempotency_scope='none',extra_requester_permissions=('guanjia.company.read',))
_HANDLERS['guanjia.sales_ranking.read.v3'] = _invoke_source_unbound


_STATIC_GROUPS = (TOOLS, JARVIS_TOOLS, STOCKTAKE_EVIDENCE_TOOLS,
                  PURCHASING_EVIDENCE_TOOLS, GUANJIA_HOST_TOOLS, RUNTIME_METADATA_TOOLS, GUANJIA_SOURCE_TOOLS, GUANJIA_CATEGORY_TOOLS, RESEARCH_TOOLS, GUANJIA_SALES_TOOLS)


def _build_registry() -> dict[tuple[str, str], ToolDefinition]:
    registry: dict[tuple[str, str], ToolDefinition] = {}
    for group in _STATIC_GROUPS:
        for identity, definition in group.items():
            if identity != (definition.key, definition.version):
                raise RuntimeError(f"tool_registry_invalid:identity:{identity[0]}:{identity[1]}")
            if identity in registry:
                raise RuntimeError(
                    f"tool_registry_duplicate:{identity[0]}:{identity[1]}")
            if definition.handler_id not in _HANDLERS:
                raise RuntimeError(
                    f"tool_registry_handler_missing:{identity[0]}:{identity[1]}")
            registry[identity] = definition
    return registry


CAPABILITY_REGISTRY = _build_registry()


def _validate_workspace_group() -> None:
    """Same structural invariants as _build_registry, for the workspace group.

    The workspace artifact tools stay outside CAPABILITY_REGISTRY (the ordinary
    business catalog) but must obey the identical identity/handler checks so a
    typo can never ship silently behind the workbench profile.
    """
    for identity, definition in WORKSPACE_TOOLS.items():
        if identity != (definition.key, definition.version):
            raise RuntimeError(f"tool_registry_invalid:identity:{identity[0]}:{identity[1]}")
        if definition.handler_id not in _HANDLERS:
            raise RuntimeError(
                f"tool_registry_handler_missing:{identity[0]}:{identity[1]}")


_validate_workspace_group()


def workspace_definition(tool_key: object, version: object) -> ToolDefinition:
    """Resolve a fixed workspace artifact definition by exact identity.

    This is definition lookup, never invocation authority: every caller must
    have already authenticated a workbench Run binding whose explicit
    tool_selection contains this pair.  resolve_tool() keeps raising
    tool_not_registered for these keys on every other path.
    """
    if type(tool_key) is not str or type(version) is not str:
        raise ToolRegistryError("tool_not_registered")
    definition = WORKSPACE_TOOLS.get((tool_key, version))
    if definition is None:
        raise ToolRegistryError("tool_not_registered")
    return definition

# Runtime-only, per-store registrations created by tools.create_tool.  Keyed by
# (store_id, tool_key, version) so the same key can exist in two stores without
# sharing an implementation or a scope.
_DYNAMIC_CAPABILITIES: dict[tuple[str, str, str], ToolDefinition] = {}
_DYNAMIC_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,79}\Z")


def resolve_tool(tool_key: object, version: object, *,
                 store_id: str | None = None) -> ToolDefinition:
    """The single key/version lookup: exact match, no prefix or latest fallback."""
    if not isinstance(tool_key, str) or not isinstance(version, str):
        raise ToolRegistryError("tool_not_registered")
    definition = None
    for group in _STATIC_GROUPS:
        definition = group.get((tool_key, version))
        if definition is not None:
            break
    if definition is None and isinstance(store_id, str):
        definition = _DYNAMIC_CAPABILITIES.get((store_id, tool_key, version))
    if definition is None:
        raise ToolRegistryError("tool_not_registered")
    return definition


def register_dynamic_tool(session: Session, requester: AuthenticatedSubject, *,
                          tool_key: str, description: str,
                          python_code: str) -> ToolDefinition:
    """Legacy registration is retired before any mutable registry operation."""
    raise ToolRegistryError(sandbox_tool.LEGACY_CODE_DISABLED)


def toolset_manifest(descriptions: object, *, store_id: str | None = None) -> list[dict]:
    """Freeze the full executable contract of a Run's model-visible tools."""
    if not isinstance(descriptions, list):
        raise ToolRegistryError("tool_registry_inconsistent")
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in descriptions:
        if not isinstance(item, dict):
            raise ToolRegistryError("tool_registry_inconsistent")
        tool_key, version = item.get("tool_key"), item.get("version")
        if not isinstance(tool_key, str) or not isinstance(version, str):
            raise ToolRegistryError("tool_registry_inconsistent")
        if (tool_key, version) in seen:
            raise ToolRegistryError("tool_registry_duplicate")
        seen.add((tool_key, version))
        if type(tool_key) is str and type(version) is str and (tool_key, version) in WORKSPACE_TOOLS:
            # Fixed registry-resident workspace contract: manifest rows are
            # descriptions of already-authorized pairs (workbench bindings
            # only); resolve_tool keeps rejecting these keys elsewhere.
            definition = workspace_definition(tool_key, version)
        else:
            definition = resolve_tool(tool_key, version, store_id=store_id)
        entries.append({
            "tool_key": tool_key,
            "version": version,
            "mode": definition.mode,
            "side_effect": definition.side_effect,
            "confirmation": definition.confirmation,
            "idempotency": definition.idempotency,
            "idempotency_scope": definition.idempotency_scope,
            "timeout_seconds": definition.timeout_seconds,
            "organization_scope": definition.organization_scope,
            "store_scope": definition.store_scope,
            "requester_permission": definition.requester_permission,
            "agent_permission": definition.agent_permission,
            "extra_requester_permissions": list(definition.extra_requester_permissions),
            "audit_action": definition.audit_action,
            "model_visible": definition.model_visible,
            "host_only": definition.host_only,
            "handler_id": definition.handler_id,
            "description_sha256": sha256(
                definition.description.encode("utf-8")).hexdigest(),
            "input_sha256": definition.input_sha256,
            "output_sha256": definition.output_sha256,
        })
    entries.sort(key=lambda entry: (entry["tool_key"], entry["version"]))
    return entries


def toolset_digest(descriptions: object, *, store_id: str | None = None) -> str:
    manifest = toolset_manifest(descriptions, store_id=store_id)
    return "sha256:" + sha256(_canonical(manifest).encode("utf-8")).hexdigest()


def verify_toolset_digest(descriptions: object, expected: object, *,
                          store_id: str | None = None) -> None:
    if not isinstance(expected, str) or not expected.startswith("sha256:"):
        raise ToolRegistryError("agent_worker_toolset_changed")
    if not hmac.compare_digest(toolset_digest(descriptions, store_id=store_id),
                               expected):
        raise ToolRegistryError("agent_worker_toolset_changed")


def _tool_description(definition: ToolDefinition) -> dict:
    return {
        "tool_key": definition.key,
        "version": definition.version,
        "description": definition.description,
        "mode": definition.mode,
        "side_effect": definition.side_effect,
        "confirmation": definition.confirmation,
        "input_schema": definition.input_model.model_json_schema(),
    }


def legacy_code_tool(tool_key: object, version: object, *,
                     store_id: str | None = None) -> bool:
    """Execution policy separate from historical key/version metadata lookup.

    Old names are denied at every version. Any pre-existing dynamic definition
    or dynamic handler remains denied even when its code or schema was replaced.
    """
    if tool_key in ("sandbox.python_exec", "tools.create_tool"):
        return True
    if not isinstance(tool_key, str) or not isinstance(version, str):
        return False
    if (store_id, tool_key, version) in _DYNAMIC_CAPABILITIES:
        return True
    try:
        definition = resolve_tool(tool_key, version, store_id=store_id)
    except ToolRegistryError:
        return False
    return definition.handler_id.startswith("dynamic.")


def invoke_tool(session: Session, *, requester: AuthenticatedSubject,
                agent: AuthenticatedSubject, tool_key: str, version: str,
                arguments: object, idempotency_key: str | None = None, result_abi: str | None = None) -> dict | NoEffectToolResult:
    """Invoke one registered capability; caller commits success or audited rejection.

    Host-only evidence tools are not reachable through this model gateway; they
    have dedicated host entry points.  A direct DYNAMIC_STORE_TOOLS write without
    ``register_dynamic_tool`` is invisible and not resolvable here.
    """
    if isinstance(tool_key, str) and tool_key in {key for key, _ in GUANJIA_HOST_TOOLS}:
        raise ToolRegistryError("guanjia_host_binding_required")
    _person_scope(session, requester)
    definition: ToolDefinition | None = None
    try:
        if legacy_code_tool(tool_key, version, store_id=requester.store_id):
            raise ToolRegistryError(sandbox_tool.LEGACY_CODE_DISABLED)
        if type(tool_key) is str and type(version) is str and (tool_key, version) in WORKSPACE_TOOLS:
            # Delegated seam (AGENT-02): the fixed workspace contract is
            # callable only through this gateway's requester/agent ACL
            # ceiling; ordinary registry resolution keeps rejecting it.
            definition = workspace_definition(tool_key, version)
        else:
            definition = resolve_tool(tool_key, version, store_id=requester.store_id)
        if definition.host_only:
            raise ToolRegistryError("tool_not_registered")
        epoch = None
        if definition.mode == "write":
            epoch = lock_command_authority(session, requester)
            _person_scope(session, requester, now=commit_guard._db_clock(session))
        _agent_scope(session, requester, agent, definition.agent_permission,
                     now=commit_guard._db_clock(session) if definition.mode == "write" else None)
        try:
            authorize_scope(session, requester, organization_id=requester.organization_id,
                            store_id=requester.store_id,
                            permission=definition.requester_permission)
            for permission in definition.extra_requester_permissions:
                authorize_scope(session, requester, organization_id=requester.organization_id,
                                store_id=requester.store_id, permission=permission)
        except HTTPException as exc:
            raise ToolRegistryError("tool_requester_permission_denied") from exc
        try:
            payload = definition.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolRegistryError("tool_input_invalid") from exc
        if (definition.input_sha256 != _schema_sha256(definition.input_model)
                or definition.output_sha256 != _schema_sha256(definition.output_model)):
            raise ToolRegistryError("tool_registry_inconsistent")
        if definition.idempotency == "required" and (
            not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key.strip()) <= 200
        ):
            raise ToolRegistryError("tool_idempotency_required")
        if definition.idempotency == "none" and idempotency_key is not None:
            raise ToolRegistryError("tool_input_invalid")
        handler = _HANDLERS.get(definition.handler_id)
        if handler is None:
            raise ToolRegistryError("tool_not_registered")
        if result_abi is not None:
            from . import canonical_result
            if result_abi != canonical_result.RESULT_ABI:
                raise ToolRegistryError('tool_result_abi_invalid')
            if definition.key == 'memory.search':
                # Preserve memory's additional authority check before even a
                # parameter hint. Catch ONLY the shared pure query validation.
                memory_service._require(session, requester, memory_service.READ, memory_service._now(None))
                try:
                    memory_recall.validate_search_query(payload.query, payload.limit)
                except memory_service.MemoryError as error:
                    if error.code != 'memory_query_invalid': raise
                    return NoEffectToolResult(dict(canonical_result.ERROR_PAYLOAD))
        # Include the output contract and tool audit in the write savepoint.
        # A Worker may catch this error and commit its failure control later.
        # savepoint=False write tools manage their own create atomicity at the
        # physical root (workspace persistence contract); they run bare like
        # the workspace router's _run_write path.
        with session.begin_nested() if definition.mode == "write" and definition.savepoint else nullcontext():
            execution = handler(session, requester, agent, payload, idempotency_key)
            if not isinstance(execution, ToolExecution):
                raise ToolRegistryError("tool_registry_inconsistent")
            try:
                result = definition.output_model.model_validate(
                    execution.result.model_dump(mode="json"))
            except ValidationError as exc:
                raise ToolRegistryError("tool_registry_inconsistent") from exc
            _audit(session, requester, action=definition.audit_action,
                   tool_key=definition.key, version=definition.version, code="ok",
                   key=execution.audit_key, task_id=execution.task_id)
            if definition.mode == "write":
                def final(db, instant):
                    try:
                        check_command_authority(db, requester, permission=definition.requester_permission,
                                                epoch=epoch, now=instant)
                        for permission in definition.extra_requester_permissions:
                            check_command_authority(db, requester, permission=permission,
                                                    epoch=epoch, now=instant)
                        _person_scope(db, requester, now=instant)
                        _agent_scope(db, requester, agent, definition.agent_permission, now=instant)
                    except (HTTPException, ToolRegistryError):
                        # CommitGuard checks abort the transaction via CommitGuardError
                        raise commit_guard.CommitGuardError("tool_requester_permission_denied")
                commit_guard.enlist(session, action="agent_tool:" + definition.key, check=final)
        return result.model_dump(mode="json")
    except workspace_service.WorkspaceError as exc:
        # Workspace service enforces the requester's per-kind business ceiling
        # (require_kind_permission) below the registry bar; surface its denials
        # as canonical tool codes so model callers see the uniform contract.
        code = ("tool_requester_permission_denied" if exc.status_code == 403 else
                "tool_not_found" if exc.status_code == 404 else
                "tool_state_conflict" if exc.status_code == 409 else
                "tool_input_invalid" if exc.status_code == 422 else
                "tool_execution_denied")
        raise ToolRegistryError(code) from exc
    except (ToolRegistryError, HTTPException) as exc:
        code = (exc.code if isinstance(exc, ToolRegistryError) else
                _TASK_ERROR_CODES.get(exc.detail, "tool_execution_denied")
                if isinstance(exc.detail, str) else "tool_execution_denied")
        _audit(session, requester, action="agent_tool.denied",
               tool_key=definition.key if definition is not None else tool_key,
               version=definition.version if definition is not None else version,
               code=code)
        raise ToolRegistryError(code) from exc


def _invoke_host_tool(session: Session, *, requester: AuthenticatedSubject,
                      agent: AuthenticatedSubject, tool_key: str, version: str,
                      arguments: object,
                      idempotency_key: str | None = None) -> dict:
    """Host-only evidence path: same registry, never model-discoverable."""
    if isinstance(tool_key, str) and tool_key in {key for key, _ in GUANJIA_HOST_TOOLS}:
        raise ToolRegistryError("guanjia_host_binding_required")
    _person_scope(session, requester)
    definition: ToolDefinition | None = None
    try:
        if legacy_code_tool(tool_key, version, store_id=requester.store_id):
            raise ToolRegistryError(sandbox_tool.LEGACY_CODE_DISABLED)
        definition = resolve_tool(tool_key, version, store_id=requester.store_id)
        if not definition.host_only:
            raise ToolRegistryError("tool_not_registered")
        session.flush()
        session.expire_all()
        _person_scope(session, requester)
        _agent_scope(session, requester, agent, definition.agent_permission)
        try:
            authorize_scope(session, requester,
                            organization_id=requester.organization_id,
                            store_id=requester.store_id,
                            permission=definition.requester_permission)
            for permission in definition.extra_requester_permissions:
                authorize_scope(session, requester,
                                organization_id=requester.organization_id,
                                store_id=requester.store_id,
                                permission=permission)
        except HTTPException as exc:
            raise ToolRegistryError("tool_requester_permission_denied") from exc
        if idempotency_key is not None:
            raise ToolRegistryError("tool_input_invalid")
        try:
            payload = definition.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolRegistryError("tool_input_invalid") from exc
        handler = _HANDLERS.get(definition.handler_id)
        if handler is None:
            raise ToolRegistryError("tool_not_registered")
        execution = handler(session, requester, agent, payload, None)
        if not isinstance(execution, ToolExecution):
            raise ToolRegistryError("tool_registry_inconsistent")
        result = definition.output_model.model_validate(
            execution.result.model_dump(mode="json"))
        _audit(session, requester, action=definition.audit_action,
               tool_key=definition.key, version=definition.version, code="ok")
        return result.model_dump(mode="json")
    except (ToolRegistryError, HTTPException, ValueError) as exc:
        code = (exc.code if isinstance(exc, ToolRegistryError) else
                exc.detail if isinstance(exc, HTTPException)
                and isinstance(exc.detail, str) else "tool_execution_denied")
        _audit(session, requester, action="agent_tool.denied",
               tool_key=definition.key if definition is not None else tool_key,
               version=definition.version if definition is not None else version,
               code=str(code))
        raise ToolRegistryError(str(code)) from exc


def invoke_stocktake_evidence(
    session: Session, *, requester: AuthenticatedSubject,
    agent: AuthenticatedSubject, tool_key: str, version: str,
    arguments: object, idempotency_key: str | None = None,
) -> dict:
    """Host-only AGT-014 evidence read; caller commits audit in same transaction."""
    return _invoke_host_tool(session, requester=requester, agent=agent,
                             tool_key=tool_key, version=version,
                             arguments=arguments,
                             idempotency_key=idempotency_key)


def invoke_purchasing_evidence(
    session: Session, *, requester: AuthenticatedSubject, agent: AuthenticatedSubject,
    tool_key: str, version: str, arguments: object,
    idempotency_key: str | None = None,
) -> dict:
    """Existing platform session, dual-subject, read grants + shared audit."""
    return _invoke_host_tool(session, requester=requester, agent=agent,
                             tool_key=tool_key, version=version,
                             arguments=arguments,
                             idempotency_key=idempotency_key)


def model_tool_descriptions(session: Session, requester: AuthenticatedSubject,
                            agent: AuthenticatedSubject, *, now=None) -> list[dict]:
    """Generate the model-visible contract from the same executable registry."""
    from backend.app.modules.assistant.principal import WorkPrincipal, projection_permissions
    work_permissions = (projection_permissions(session, requester, now=now)
                        if type(requester) is WorkPrincipal else None)
    descriptions = []
    all_defs = list(TOOLS.values()) + list(JARVIS_TOOLS.values())
    for definition in sorted(all_defs, key=lambda item: (item.key, item.version)):
        if (not definition.model_visible or definition.host_only
                or legacy_code_tool(definition.key, definition.version,
                                    store_id=requester.store_id)):
            continue
        try:
            if work_permissions is None:
                authorize_scope(session, requester, organization_id=requester.organization_id,
                                store_id=requester.store_id, permission=definition.requester_permission, now=now)
            elif definition.requester_permission and definition.requester_permission not in work_permissions:
                continue
            _agent_scope(session, requester, agent, definition.agent_permission, now=now)
        except ToolRegistryError:
            continue
        except HTTPException as exc:
            if exc.status_code == 403 and exc.detail == "permission_denied":
                continue
            raise
        descriptions.append(_tool_description(definition))

    return descriptions


# AGT-027's opt-in Host contract. This table is not a general registry export
# and does not enable an SDK, Provider, runtime route or default SDK tool.
PI_READONLY_PROFILE = "pi-readonly-v1"
PI_MEMORY_PROFILE = "pi-memory-v1"
PI_DISCOVERY_PROFILE = discovery.PROFILE
PI_SOURCE_PROFILE = discovery.SOURCE_PROFILE
PI_PROFILES = frozenset((PI_READONLY_PROFILE, PI_MEMORY_PROFILE, PI_DISCOVERY_PROFILE, PI_SOURCE_PROFILE))
# The workbench profile is a recognizable upper bound over the source policy
# plus the workspace artifact tools; it never grants tools by default and is
# always cropped by an explicit, persisted tool_selection (see
# WORKBENCH-TOOLS-AUTHORITY-CONTRACT section 3). Old profiles keep their
# exact frozen shape and gain no new binding field.
PI_WORKBENCH_PROFILE = "pi-workbench-v1"
PI_ALL_PROFILES = frozenset((*PI_PROFILES, PI_WORKBENCH_PROFILE))
_PI_ARTIFACT_NAMES = MappingProxyType({
    ("artifact.read", "1"): "v4_artifact_read_v1",
    ("artifact.create", "1"): "v4_artifact_create_v1",
    ("artifact.patch", "1"): "v4_artifact_patch_v1",
})
_PI_READONLY_NAMES = MappingProxyType({
    ("memory.search", "1"): "v4_memory_search_v1",
    ("tasks.list_mine", "1"): "v4_tasks_list_mine_v1",
    ("knowledge.search", "1"): "v4_knowledge_search_v1",
    ("catalog.search", "1"): "v4_catalog_search_v1",
})
_PI_READONLY_HANDLERS = MappingProxyType({
    pair: _HANDLERS[f"{pair[0]}.v{pair[1]}"] for pair in _PI_READONLY_NAMES
})
_PI_MEMORY_NAMES = MappingProxyType({**_PI_READONLY_NAMES,
    **{('memory.' + op, '1'): 'v4_memory_' + op + '_v1'
       for op in ('save', 'correct', 'withdraw')}})
_PI_MEMORY_HANDLERS = MappingProxyType({
    pair: _HANDLERS[f"{pair[0]}.v{pair[1]}"] for pair in _PI_MEMORY_NAMES
})

_PI_DISCOVERY_NAMES = MappingProxyType({**_PI_MEMORY_NAMES, (discovery.KEY, "1"): discovery.NAME})
_PI_DISCOVERY_HANDLERS = MappingProxyType({pair: _HANDLERS[f"{pair[0]}.v{pair[1]}"] for pair in _PI_DISCOVERY_NAMES})
_PI_SOURCE_NAMES = MappingProxyType({**_PI_DISCOVERY_NAMES,
    ('guanjia.read_prices', '1'): 'v4_guanjia_read_prices_v1',
    ('guanjia.read_stock', '1'): 'v4_guanjia_read_stock_v1'})
_PI_CATEGORY_NAMES = MappingProxyType({('guanjia.category_discovery','1'):'v4_guanjia_category_discovery_v1',
    ('guanjia.category_products','1'):'v4_guanjia_category_products_v1'})
_PI_CATEGORY_STATUS_NAMES = MappingProxyType({
    ('guanjia.category_discovery', '1'): 'v4_guanjia_category_discovery_v1',
    ('guanjia.category_products_status', '1'): 'v4_guanjia_category_products_status_v1'})
_PI_SOURCE_HANDLERS = MappingProxyType({pair: _HANDLERS[f"{pair[0]}.v{pair[1]}"]
    for pair in (*_PI_SOURCE_NAMES, *_PI_CATEGORY_NAMES, *_PI_CATEGORY_STATUS_NAMES, *research_runtime.NAMES, *sales_ranking_runtime.NAMES, *sales_product_runtime.NAMES, *local_sales_runtime.NAMES)})
_PI_ARTIFACT_HANDLERS = MappingProxyType({pair: _HANDLERS[f"{pair[0]}.v{pair[1]}"]
    for pair in _PI_ARTIFACT_NAMES})

def _pi_names(profile, category_source=None, category_products_contract=None, research_source=None, sales_source=None, sales_contract='1'):
    if sales_contract not in ('1', '2', '3') or sales_source is None and sales_contract != '1':
        raise ToolRegistryError('agent_pi_toolset_invalid')
    if sales_source is not None:
        try: (local_sales_runtime if sales_contract=='3' else sales_ranking_runtime).validate_source(sales_source)
        except (ValueError, TypeError, KeyError): raise ToolRegistryError('agent_pi_toolset_invalid') from None
        if profile not in (PI_SOURCE_PROFILE, PI_WORKBENCH_PROFILE): raise ToolRegistryError('agent_pi_toolset_invalid')
    if research_source is not None:
        from .research_host import validate_pin
        try: validate_pin(research_source)
        except (ValueError,TypeError,KeyError):raise ToolRegistryError('agent_pi_toolset_invalid') from None
        if profile not in (PI_SOURCE_PROFILE, PI_WORKBENCH_PROFILE):raise ToolRegistryError('agent_pi_toolset_invalid')
    if category_products_contract is not None and (
            type(category_products_contract) is not str
            or category_products_contract != CATEGORY_STATUS_CONTRACT
            or profile not in (PI_SOURCE_PROFILE, PI_WORKBENCH_PROFILE) or category_source is None):
        raise ToolRegistryError('agent_pi_toolset_invalid')
    if profile == PI_READONLY_PROFILE:
        return _PI_READONLY_NAMES
    if profile == PI_MEMORY_PROFILE:
        return _PI_MEMORY_NAMES
    if profile == PI_DISCOVERY_PROFILE:
        return _PI_DISCOVERY_NAMES
    if profile == PI_SOURCE_PROFILE:
        if sales_contract == '3':
            if category_source is not None or research_source is not None or category_products_contract is not None:
                raise ToolRegistryError('agent_pi_toolset_invalid')
            # Local-only source profile never gains ERP network tools through
            # an unrelated preexisting grant. Normal discovery/memory remain.
            return {**_PI_DISCOVERY_NAMES, **local_sales_runtime.NAMES}
        research_names={p:n for p,n in research_runtime.NAMES.items() if p[0]!='research.probe' or research_source is not None and 'read' in research_source}
        category_names = (_PI_CATEGORY_STATUS_NAMES if category_products_contract is not None else _PI_CATEGORY_NAMES)
        if research_source is not None and 'trial_name' in research_source:
            if category_source is not None or category_products_contract is not None or sales_source is not None:
                raise ToolRegistryError('agent_pi_toolset_invalid')
            # A research-only root never acquires ERP or write tools, even if
            # a mistaken grant is later added to its isolated identity.
            return {**{p:n for p,n in _PI_DISCOVERY_NAMES.items() if p[0] in ('tasks.list_mine','runtime.tool_search')},
                    **research_names}
        return {**_PI_SOURCE_NAMES, **(category_names if category_source is not None else {}),
                **(research_names if research_source is not None else {}),
                **((sales_product_runtime.NAMES if sales_contract == '2' else sales_ranking_runtime.NAMES) if sales_source is not None else {})}
    if profile == PI_WORKBENCH_PROFILE:
        if research_source is not None and 'trial_name' in research_source:
            # An isolated research trial never gains workspace artifact
            # tools, even when a workbench profile name is layered on top
            # of its pinned isolation.
            raise ToolRegistryError('agent_pi_toolset_invalid')
        # Base policy is the source catalog (workbench requires every
        # capability flag), first cropped by the original pins; the
        # artifact tools extend the recognizable bound only. Binding-time
        # tool_selection crops this again to the configured upper bound.
        return {**_pi_names(PI_SOURCE_PROFILE, category_source,
                            category_products_contract, research_source,
                            sales_source, sales_contract),
                **_PI_ARTIFACT_NAMES}
    raise ToolRegistryError('agent_pi_profile_invalid')


_PI_BINDING_FIELDS = frozenset({
    "profile", "run_id", "organization_id", "store_id", "requester_id",
    "agent_id", "ordinary_toolset_version", "tools",
})


def _pi_json(value: object) -> str:
    """No lossy default=str, non-finite values or mutable caller references."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        encoded.encode("utf-8")
        return encoded
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ToolRegistryError("agent_pi_toolset_invalid") from None


def _pi_definition(key: str, version: str, *, profile=PI_READONLY_PROFILE) -> ToolDefinition:
    # Resolve a fixed definition, not invocation authority. The executor first
    # checks exact membership in the already verified Run binding. Old bindings
    # cannot acquire source tools through this identity-only helper.
    if profile not in PI_ALL_PROFILES:
        raise ToolRegistryError('agent_pi_profile_invalid')
    memory_write = (profile in (PI_MEMORY_PROFILE, PI_DISCOVERY_PROFILE, PI_SOURCE_PROFILE, PI_WORKBENCH_PROFILE) and key in ("memory.save", "memory.correct", "memory.withdraw")) or (profile == PI_WORKBENCH_PROFILE and key in ("artifact.create", "artifact.patch"))
    handlers = (_PI_SOURCE_HANDLERS if profile == PI_SOURCE_PROFILE else
                {**_PI_SOURCE_HANDLERS, **_PI_ARTIFACT_HANDLERS} if profile == PI_WORKBENCH_PROFILE else
                _PI_DISCOVERY_HANDLERS if profile == PI_DISCOVERY_PROFILE else
                _PI_MEMORY_HANDLERS if profile == PI_MEMORY_PROFILE else _PI_READONLY_HANDLERS)
    metadata = profile in (PI_DISCOVERY_PROFILE, PI_SOURCE_PROFILE, PI_WORKBENCH_PROFILE) and key == discovery.KEY
    source = profile in (PI_SOURCE_PROFILE, PI_WORKBENCH_PROFILE) and ((key, version) in GUANJIA_SOURCE_TOOLS or (key, version) in GUANJIA_CATEGORY_TOOLS or (key, version) in RESEARCH_TOOLS or (key, version) in GUANJIA_SALES_TOOLS)
    if (key, version) not in handlers:
        raise ToolRegistryError("agent_pi_tool_not_allowed")
    if profile == PI_WORKBENCH_PROFILE and (key, version) in WORKSPACE_TOOLS:
        definition = workspace_definition(key, version)
    else:
        definition = resolve_tool(key, version)
    if (definition.mode != ("write" if memory_write else "read")
            or definition.side_effect != ("business_write" if memory_write else "read_only")
            or definition.idempotency != ("required" if memory_write else "none")
            or definition.model_visible is not (not (metadata or source)) or definition.host_only is not (metadata or source)
            or definition.confirmation != "none"
            or definition.handler_id != f"{key}.v{version}"
            or definition.organization_scope != "authenticated_organization"
            or definition.store_scope != "requester_store"
            or legacy_code_tool(key, version)
            or not callable(_HANDLERS.get(definition.handler_id))
            or _HANDLERS[definition.handler_id] is not handlers[(key, version)]
            or definition.input_sha256 != _schema_sha256(definition.input_model)
            or definition.output_sha256 != _schema_sha256(definition.output_model)):
        raise ToolRegistryError("agent_pi_toolset_invalid")
    return definition


def _pi_binding_shape(binding: object, *, profile=PI_READONLY_PROFILE) -> dict:
    """Structural validation is not authentication of an arbitrary Host."""
    category_source = binding.get('category_source') if type(binding) is dict else None
    category_products_contract = binding.get('category_products_contract') if type(binding) is dict else None
    research_source = binding.get('research_source') if type(binding) is dict else None
    sales_source = binding.get('sales_source') if type(binding) is dict else None
    if category_source is not None:
        from backend.app.modules.integrations.guanjia.category_host import validate_pin
        try:
            validate_pin(category_source)
        except (ValueError,TypeError,KeyError):
            raise ToolRegistryError('agent_pi_toolset_invalid') from None
        if profile not in (PI_SOURCE_PROFILE, PI_WORKBENCH_PROFILE):
            raise ToolRegistryError('agent_pi_toolset_invalid')
    sales_contract = binding.get('sales_contract', '1') if type(binding) is dict else '1'
    if type(binding) is dict and 'sales_contract' in binding and sales_contract not in ('2','3'):
        raise ToolRegistryError('agent_pi_toolset_invalid')
    names = _pi_names(profile, category_source, category_products_contract, research_source, sales_source, sales_contract)
    tool_selection = binding.get('tool_selection') if type(binding) is dict else None
    if profile == PI_WORKBENCH_PROFILE:
        if (type(tool_selection) is not list or not tool_selection
                or any(type(item) is not dict or set(item) != {"tool_key", "version"}
                       or type(item.get("tool_key")) is not str or type(item.get("version")) is not str
                       for item in tool_selection)):
            raise ToolRegistryError('agent_pi_toolset_invalid')
        selection_pairs = [(item["tool_key"], item["version"]) for item in tool_selection]
        if (sorted(set(selection_pairs)) != selection_pairs
                or not set(selection_pairs) <= set(names)):
            raise ToolRegistryError('agent_pi_toolset_invalid')
    elif tool_selection is not None:
        raise ToolRegistryError('agent_pi_toolset_invalid')
    fields = (_PI_BINDING_FIELDS | ({'category_source'} if category_source is not None else set())
              | ({'category_products_contract'} if category_products_contract is not None else set())
              | ({'research_source'} if research_source is not None else set())
              | ({'sales_source'} if sales_source is not None else set())
              | ({'sales_contract'} if sales_contract in ('2','3') else set())
              | ({'tool_selection'} if profile == PI_WORKBENCH_PROFILE else set()))
    if (type(binding) is not dict or set(binding) != fields
            or binding.get("profile") != profile
            or any(type(binding.get(k)) is not str or not binding[k]
                   or len(binding[k]) > 120
                   for k in ("run_id", "organization_id", "store_id",
                             "requester_id", "agent_id"))
            or not isinstance(binding.get("ordinary_toolset_version"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", binding["ordinary_toolset_version"])
            or type(binding.get("tools")) is not list
            or len(binding["tools"]) > len(names)):
        raise ToolRegistryError("agent_pi_toolset_invalid")
    if sales_source is not None:
        (local_sales_runtime if sales_contract=='3' else sales_ranking_runtime).validate_source(sales_source, binding)
    seen = set()
    for item in binding["tools"]:
        if (type(item) is not dict
                or set(item) != {"name", "tool_key", "version", "description",
                                 "input_schema", "contract"}
                or type(item["tool_key"]) is not str or type(item["version"]) is not str):
            raise ToolRegistryError("agent_pi_toolset_invalid")
        pair = item["tool_key"], item["version"]
        if (pair in seen or pair not in names
                or (profile == PI_WORKBENCH_PROFILE and pair not in selection_pairs
                    and pair != (discovery.KEY, "1"))
                or item["name"] != names[pair]
                or type(item["description"]) is not str
                or type(item["input_schema"]) is not dict
                or type(item["contract"]) is not dict):
            raise ToolRegistryError("agent_pi_toolset_invalid")
        seen.add(pair)
        definition = _pi_definition(*pair, profile=profile)
        expected = {"name": names[pair], "tool_key": pair[0],
            "version": pair[1], "description": definition.description,
            "input_schema": definition.input_model.model_json_schema(),
            "contract": toolset_manifest([{"tool_key": pair[0], "version": pair[1]}])[0]}
        if _pi_json(item) != _pi_json(expected):
            raise ToolRegistryError("agent_pi_toolset_invalid")
    if [(e["tool_key"], e["version"]) for e in binding["tools"]] != sorted(seen):
        raise ToolRegistryError("agent_pi_toolset_invalid")
    _pi_json(binding)
    return binding


def pi_binding_digest(binding: object) -> str:
    return "sha256:" + sha256(_pi_json(_pi_binding_shape(binding, profile=_binding_profile(binding))).encode("utf-8")).hexdigest()


def _workbench_artifact_pairs(session: Session, requester: AuthenticatedSubject, *, now=None) -> frozenset:
    """Artifact pairs admitted by the requester's current Work delegation.

    Frozen authority rule: a workbench artifact tool enters a Run's frozen
    binding only when the delegated requester's Work carries an explicit
    workspace context whose kinds/ops the current principal still holds the
    kind permissions for (create/patch additionally require the selected
    read delegation).  Standing personal or machine grants never substitute
    for the Work resource delegation; Works without a context admit none.
    """
    from backend.app.modules.assistant.authority import _models
    from backend.app.modules.assistant.principal import WorkPrincipal, projection_permissions
    from backend.app.modules.assistant.workspace_binding import root_context
    from backend.app.modules.workspace.service import _KIND_PERMISSION_MAP
    if type(requester) is not WorkPrincipal or requester.kind not in ('work', 'turn'):
        return frozenset()
    work = session.get(_models(requester.kind)[0], requester.work_id, populate_existing=True)
    if work is None:
        return frozenset()
    context = root_context(session, work, kind=requester.kind)
    if context is None:
        return frozenset()
    held = projection_permissions(session, requester, now=now)
    allowed = set()
    for artifact in context.get('artifacts') or ():
        if type(artifact) is not dict:
            continue
        permissions = _KIND_PERMISSION_MAP.get(artifact.get('kind'))
        operations = artifact.get('operations')
        if permissions is None or type(operations) is not list:
            continue
        read_ok = ('read' in operations
                   and any(permission in held for permission in permissions['read']))
        if not read_ok:
            continue
        allowed.add(("artifact.read", "1"))
        write_ok = any(permission in held for permission in permissions['write'])
        if write_ok and 'create' in operations:
            allowed.add(("artifact.create", "1"))
        if write_ok and 'patch' in operations:
            allowed.add(("artifact.patch", "1"))
    return frozenset(allowed)


def freeze_pi_binding(session: Session, requester: AuthenticatedSubject,
                               agent: AuthenticatedSubject, *, run_id: str,
                               now=None, profile=PI_READONLY_PROFILE, category_source=None,
                               category_products_contract=None, research_source=None, sales_source=None, sales_contract='2',
                               tool_selection=None) -> dict:
    """Build a new Run's finite allowlist under current double authorization.

    The caller owns initial Run persistence. This function never repins a Run,
    changes permissions, dispatches a tool or commits a transaction.
    Workbench runs additionally require an explicit tool_selection (the
    configured upper bound); omission must never default to all tools.
    """
    sales_contract = sales_contract if sales_source is not None else '1'
    names = _pi_names(profile, category_source, category_products_contract, research_source, sales_source, sales_contract)
    if profile == PI_WORKBENCH_PROFILE:
        if (type(tool_selection) is not list or not tool_selection
                or any(type(item) is not dict or set(item) != {"tool_key", "version"}
                       or type(item.get("tool_key")) is not str or type(item.get("version")) is not str
                       for item in tool_selection)):
            raise ToolRegistryError("agent_pi_toolset_invalid")
        selection_pairs = [(item["tool_key"], item["version"]) for item in tool_selection]
        if (sorted(set(selection_pairs)) != selection_pairs
                or not set(selection_pairs) <= set(names)):
            raise ToolRegistryError("agent_pi_toolset_invalid")
        tool_selection = [{"tool_key": key, "version": version}
                          for key, version in sorted(set(selection_pairs))]
    elif tool_selection is not None:
        raise ToolRegistryError("agent_pi_toolset_invalid")
    with session.no_autoflush:
        _person_scope(session, requester, now=now)
        if (requester.organization_id, requester.store_id) != (
                agent.organization_id, agent.store_id):
            raise ToolRegistryError("tool_scope_denied")
        if (agent.account_id is not None or agent.session_id is not None
                or session.scalar(select(Subject.subject_type).where(
                    Subject.organization_id == agent.organization_id,
                    Subject.id == agent.subject_id)) != "agent"):
            raise ToolRegistryError("tool_agent_identity_invalid")
        try:
            authorize_scope(session, agent, organization_id=agent.organization_id,
                            store_id=agent.store_id, now=now)
        except HTTPException as exc:
            raise ToolRegistryError("tool_agent_permission_denied") from exc
        try:
            _build_registry()
        except RuntimeError:
            raise ToolRegistryError("agent_pi_toolset_invalid") from None
        descriptions = model_tool_descriptions(session, requester, agent, now=now)
        ordinary_pin = toolset_digest(descriptions, store_id=requester.store_id)
        if profile in (PI_DISCOVERY_PROFILE, PI_SOURCE_PROFILE, PI_WORKBENCH_PROFILE):
            descriptions = descriptions + [{"tool_key": discovery.KEY, "version": "1"}]
        if profile in (PI_SOURCE_PROFILE, PI_WORKBENCH_PROFILE):
            descriptions = descriptions + [{"tool_key": key, "version": version} for key, version in GUANJIA_SOURCE_TOOLS]
            if research_source is not None:
                descriptions += [{'tool_key':key,'version':version} for key,version in RESEARCH_TOOLS]
            if category_source is not None:
                descriptions += [{"tool_key":key,"version":version} for key,version in GUANJIA_CATEGORY_TOOLS]
            if sales_source is not None:
                (local_sales_runtime if sales_contract=='3' else sales_ranking_runtime).validate_source(sales_source, {'organization_id':requester.organization_id,
                    'store_id':requester.store_id,'requester_id':requester.subject_id,'agent_id':agent.subject_id}, now=now)
                descriptions += [{'tool_key':sales_ranking_runtime.KEY,'version':sales_contract}]
        if profile == PI_WORKBENCH_PROFILE:
            # Artifact tools are ordinary registry tools and may already be
            # present in the model-visible descriptions when both ACLs
            # grant them; never append a duplicate entry.
            present = {(item["tool_key"], item["version"]) for item in descriptions}
            descriptions += [{"tool_key": key, "version": version}
                             for key, version in _PI_ARTIFACT_NAMES
                             if (key, version) not in present]
            # Artifact pairs additionally require the requester's explicit
            # Work delegation (thread binding + workspace kinds/ops with
            # currently held kind permissions), never a standing grant.
            artifact_pairs = _workbench_artifact_pairs(session, requester, now=now)
            selected_pairs = {(entry["tool_key"], entry["version"]) for entry in tool_selection}
        entries = []
        for item in descriptions:
            pair = item["tool_key"], item["version"]
            if pair not in names:
                continue
            if profile == PI_WORKBENCH_PROFILE:
                if pair == (discovery.KEY, "1"):
                    # Structural discovery machinery of the profile itself,
                    # not a business tool inside the selection upper bound;
                    # the child runtime requires it in the frozen manifest.
                    pass
                elif pair not in selected_pairs:
                    continue
                elif pair in WORKSPACE_TOOLS and pair not in artifact_pairs:
                    continue
            definition = _pi_definition(*pair, profile=profile)
            try:
                authorize_scope(session, requester,
                    organization_id=requester.organization_id, store_id=requester.store_id,
                    permission=definition.requester_permission, now=now)
                _agent_scope(session, requester, agent, definition.agent_permission, now=now)
                for permission in definition.extra_requester_permissions:
                    authorize_scope(session, requester,
                        organization_id=requester.organization_id, store_id=requester.store_id,
                        permission=permission, now=now)
            except ToolRegistryError:
                continue
            except HTTPException as exc:
                if exc.status_code == 403 and exc.detail == "permission_denied":
                    continue
                raise
            entries.append({"name": names[pair],
                "tool_key": pair[0], "version": pair[1],
                "description": definition.description,
                "input_schema": definition.input_model.model_json_schema(),
                "contract": toolset_manifest([item], store_id=requester.store_id)[0]})
        entries.sort(key=lambda item: (item["tool_key"], item["version"]))
        binding = {"profile": profile, "run_id": run_id,
            "organization_id": requester.organization_id, "store_id": requester.store_id,
            "requester_id": requester.subject_id, "agent_id": agent.subject_id,
            "ordinary_toolset_version": ordinary_pin, "tools": entries}
        if sales_source is not None:
            binding['sales_source'] = sales_source
            if sales_contract in ('2','3'): binding['sales_contract'] = sales_contract
        if research_source is not None:
            binding['research_source'] = research_source
        if category_source is not None:
            binding['category_source'] = category_source
        if category_products_contract is not None:
            binding['category_products_contract'] = category_products_contract
        if profile == PI_WORKBENCH_PROFILE:
            binding['tool_selection'] = tool_selection
        return json.loads(_pi_json(_pi_binding_shape(binding, profile=profile)))


def verify_pi_binding(session: Session, requester: AuthenticatedSubject,
                               agent: AuthenticatedSubject, *, run_id: str,
                               binding: object, expected_digest: object, now=None) -> list[dict]:
    """Recompute the original pin and readonly subset; never repair an old pin."""
    profile = _binding_profile(binding)
    frozen = _pi_binding_shape(binding, profile=profile)
    if (type(expected_digest) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest) is None
            or not hmac.compare_digest(pi_binding_digest(frozen), expected_digest)):
        raise ToolRegistryError("agent_worker_toolset_changed")
    current = freeze_pi_binding(session, requester, agent, run_id=run_id, now=now, profile=profile,
        category_source=frozen.get('category_source'), category_products_contract=frozen.get('category_products_contract'), research_source=frozen.get('research_source'), sales_source=frozen.get('sales_source'), sales_contract=frozen.get('sales_contract', '1'),
        tool_selection=frozen.get('tool_selection'))
    if not hmac.compare_digest(_pi_json(current).encode(), _pi_json(frozen).encode()):
        raise ToolRegistryError("agent_worker_toolset_changed")
    return [{"tool_key": e["tool_key"], "version": e["version"],
             "description": e["description"], "mode": e["contract"]["mode"], "side_effect": e["contract"]["side_effect"],
             "confirmation": "none", "input_schema": deepcopy(e["input_schema"])}
            for e in frozen["tools"]]


def pi_model_tools(binding: object) -> list[dict]:
    """Projection only; callers must have verified the trusted Run binding."""
    return [{"name": e["name"], "description": e["description"],
             "parameters": deepcopy(e["input_schema"])}
            for e in _pi_binding_shape(binding, profile=_binding_profile(binding))["tools"]]


def pi_pair(binding: object, name: object, version: object) -> tuple[str, str]:
    """Exact reverse lookup restricted to this Run, never the global table."""
    profile = _binding_profile(binding)
    frozen = _pi_binding_shape(binding, profile=profile)
    if type(name) is not str or type(version) is not str:
        raise ToolRegistryError("agent_pi_tool_not_allowed")
    for entry in frozen["tools"]:
        if entry["name"] == name and entry["version"] == version:
            return entry["tool_key"], entry["version"]
    raise ToolRegistryError("agent_pi_tool_not_allowed")


def _binding_profile(binding):
    # A persisted binding is untrusted run data: any structural malformation
    # is a corrupted toolset pin and must fail closed as toolset-invalid,
    # never re-classified as a profile selection or fall back to another set.
    if type(binding) is not dict or type(binding.get('profile')) is not str or binding['profile'] not in PI_ALL_PROFILES:
        raise ToolRegistryError('agent_pi_toolset_invalid')
    return binding['profile']


# Compatibility entry points remain strictly read-only, including malformed/new profiles.
def pi_readonly_binding_digest(binding):
    _pi_binding_shape(binding)
    return pi_binding_digest(binding)


def freeze_pi_readonly_binding(session, requester, agent, *, run_id, now=None):
    return freeze_pi_binding(session, requester, agent, run_id=run_id, now=now)


def verify_pi_readonly_binding(session, requester, agent, *, run_id, binding, expected_digest, now=None):
    _pi_binding_shape(binding)
    return verify_pi_binding(session, requester, agent, run_id=run_id, binding=binding,
                             expected_digest=expected_digest, now=now)


def pi_readonly_model_tools(binding):
    _pi_binding_shape(binding)
    return pi_model_tools(binding)


def pi_readonly_pair(binding, name, version):
    _pi_binding_shape(binding)
    return pi_pair(binding, name, version)


def invoke_guanjia_observation(session: Session, *, requester: AuthenticatedSubject,
                                agent: AuthenticatedSubject, tool_key: str, version: str,
                                arguments: object, host: guanjia_tool.ObservationHost) -> dict:
    """Dedicated Host path; never uses the old flush/expire evidence helper."""
    from backend.app.modules.integrations.guanjia.read_service import _observation_clean_session
    _observation_clean_session(session)
    definition = resolve_tool(tool_key, version, store_id=requester.store_id)
    if (tool_key, version) not in GUANJIA_HOST_TOOLS:
        raise ToolRegistryError("tool_not_registered")
    if (definition.input_sha256 != _schema_sha256(definition.input_model)
            or definition.output_sha256 != _schema_sha256(definition.output_model)):
        raise ToolRegistryError("tool_registry_inconsistent")
    try:
        payload = definition.input_model.model_validate(arguments)
    except ValidationError:
        raise ToolRegistryError("tool_input_invalid") from None
    try:
        result = guanjia_tool.invoke(session, requester, agent, payload, host=host)
        return definition.output_model.model_validate(result.model_dump()).model_dump(mode="json")
    except (guanjia_tool.ObservationDenied, guanjia_tool.ObservationAuditUnavailable) as exc:
        raise ToolRegistryError(str(exc)) from None
