"""SKL-002: Repeated employee action pattern discovery and skill candidate proposal.

Requirements: R-014, R-033, SKL-002.
Enforces bounded sampling, de-identification, deduplication of retries,
counterexample identification, conflict detection, and fail-closed draft proposals.
No automatic release or runner bypass: all proposed skills enter SKL-001 as unapproved drafts.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents.models import (
    AgentConfirmation,
    AgentExecution,
    AgentRun,
    AgentStep,
    AgentToolCall,
    SkillVersion,
)
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.memory.models import PersonalMemory
from backend.app.modules.memory.policy import classify_content, filter_content_for_reader

PATTERN_DISCOVER_PERMISSION = "skills.patterns.discover"
PATTERN_PROPOSE_PERMISSION = "skills.patterns.propose"
SKILL_SUBMIT_PERMISSION = "skills.submit"

# Regex for de-identification
PHONE_PATTERN = re.compile(r"\b1[3-9]\d{9}\b")
TOKEN_PATTERN = re.compile(r"\b(?:sx1_|sx2_)[A-Za-z0-9_-]{30,}\b")


class PatternDiscoveryError(ValueError):
    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail or code
        super().__init__(self.detail)


def _now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PatternDiscoveryError("pattern_datetime_invalid")
    return value.astimezone(timezone.utc)


def _require_human_actor(session: Session, actor: AuthenticatedSubject, permission: str, now: datetime) -> None:
    if (not isinstance(actor, AuthenticatedSubject)
            or actor.account_id is None or actor.session_id is None
            or actor.account_version is None):
        raise HTTPException(401, "pattern_employee_session_required")
    
    subject_kind = session.scalar(select(Subject.subject_type).where(
        Subject.id == actor.subject_id, Subject.organization_id == actor.organization_id
    ))
    if subject_kind != "person":
        raise HTTPException(403, "pattern_human_actor_required")

    # Check either specific pattern permission or broad skill submit permission
    has_perm = False
    for perm in [permission, SKILL_SUBMIT_PERMISSION]:
        try:
            authorize_scope(session, actor, organization_id=actor.organization_id,
                            store_id=actor.store_id, permission=perm, now=now)
            has_perm = True
            break
        except HTTPException:
            continue
    if not has_perm:
        raise HTTPException(403, "pattern_permission_denied")


def deidentify_text(text: str, can_read_restricted: bool = False) -> str:
    """Removes customer phones, auth tokens, and commercial restricted secrets."""
    if not text:
        return ""
    scrubbed = PHONE_PATTERN.sub("[脱敏手机号]", text)
    scrubbed = TOKEN_PATTERN.sub("[脱敏凭证]", scrubbed)
    scrubbed = filter_content_for_reader(scrubbed, can_read_restricted=can_read_restricted)
    return scrubbed


@dataclass
class ToolInvocation:
    tool_key: str
    tool_version: str
    input_digest: str
    sequence_no: int
    step_state: str = "unknown"


@dataclass
class RunTraceSummary:
    run_id: str
    state: str
    requester_id: str
    tool_sequence: list[str]
    invocations: list[ToolInvocation]
    created_at: datetime
    has_rejection: bool = False
    is_invalidated: bool = False
    invalidation_reason: str | None = None
    business_outcome: str = "unknown"
    execution_failure: bool = False
    execution_unknown: bool = False


def _require_clean_session(session: Session) -> None:
    # Must run before ANY authorization query/refresh. Even no_autoflush cannot
    # protect dirty attributes from populate_existing replacing their values.
    if session.new or session.dirty or session.deleted:
        raise PatternDiscoveryError("pattern_pending_changes")


def extract_run_traces(
    session: Session,
    actor: AuthenticatedSubject,
    *,
    limit: int = 50,
    since_days: int = 30,
    now: datetime | None = None,
) -> list[RunTraceSummary]:
    """Bounded, store-scoped sampling of completed and failed task runs."""
    _require_clean_session(session)
    with session.no_autoflush:
        current = _now(now)
        _require_human_actor(session, actor, PATTERN_DISCOVER_PERMISSION, current)

        since_time = current - timedelta(days=max(1, min(since_days, 90)))
        bounded_limit = max(1, min(limit, 200))

        runs = session.scalars(
            select(AgentRun)
            .where(
                AgentRun.organization_id == actor.organization_id,
                AgentRun.store_id == actor.store_id,
                AgentRun.created_at >= since_time,
            )
            .order_by(AgentRun.created_at.desc())
            .limit(bounded_limit)
            .execution_options(populate_existing=True)
        ).all()

        summaries = []
        for run in runs:
            # Check if run steps have rejections from confirmations
            rejections_count = session.scalar(
                select(AgentConfirmation.run_id)
                .where(
                    AgentConfirmation.organization_id == actor.organization_id,
                    AgentConfirmation.store_id == actor.store_id,
                    AgentConfirmation.run_id == run.id,
                    AgentConfirmation.state.in_(["rejected", "cancelled", "expired"]),
                )
                .limit(1)
            )
            has_rejection = rejections_count is not None

            # Fetch tool calls for this run ordered by step sequence
            tool_call_rows = session.execute(
                select(AgentToolCall, AgentStep.sequence_no, AgentStep.state)
                .join(AgentStep, (AgentToolCall.step_id == AgentStep.id) & (AgentToolCall.run_id == AgentStep.run_id)
                      & (AgentToolCall.organization_id == AgentStep.organization_id)
                      & (AgentToolCall.store_id == AgentStep.store_id))
                .where(
                    AgentToolCall.organization_id == actor.organization_id,
                    AgentToolCall.store_id == actor.store_id,
                    AgentToolCall.run_id == run.id,
                )
                .order_by(AgentStep.sequence_no.asc())
                .execution_options(populate_existing=True)
            ).all()

            invocations: list[ToolInvocation] = []
            raw_tool_sequence: list[str] = []
            for tc, seq_no, step_state in tool_call_rows:
                invocations.append(ToolInvocation(
                    tool_key=tc.tool_key,
                    tool_version=tc.tool_version,
                    input_digest=tc.input_digest,
                    sequence_no=seq_no,
                    step_state=step_state,
                ))
                # Deduplicate immediate retries within the same sequence
                if not raw_tool_sequence or raw_tool_sequence[-1] != tc.tool_key:
                    raw_tool_sequence.append(tc.tool_key)

            # These are execution observations, never proof the business goal was met.
            step_states = list(session.scalars(select(AgentStep.state).where(
                AgentStep.organization_id == run.organization_id,
                AgentStep.store_id == run.store_id, AgentStep.run_id == run.id)))
            execution = session.scalar(select(AgentExecution).where(
                AgentExecution.organization_id == run.organization_id,
                AgentExecution.store_id == run.store_id, AgentExecution.run_id == run.id
            ).execution_options(populate_existing=True))
            execution_failure = has_rejection or run.state in {"failed", "cancelled"} or any(
                state in {"failed", "cancelled"} for state in step_states)
            execution_unknown = (not step_states or any(state not in {"succeeded", "failed", "cancelled"}
                for state in step_states) or (execution is not None and
                (execution.phase == "unknown" or execution.effect_status == "unknown"
                 or execution.dispatch_state == "unknown")))

            # Check if source memory / evidence was invalidated or withdrawn
            is_invalidated = False
            invalidation_reason = None
            if run.input_ref and run.input_ref.startswith("memory:"):
                mem_id = run.input_ref.split(":", 1)[1]
                mem = session.get(PersonalMemory, mem_id, populate_existing=True)
                from backend.app.modules.memory.service import _is_unexpired
                if (mem is None or (mem.organization_id, mem.store_id, mem.owner_id) !=
                        (run.organization_id, run.store_id, run.requester_id)
                        or not mem.active or not _is_unexpired(mem, current)):
                    is_invalidated = True
                    invalidation_reason = "原个人记忆缺失、范围不匹配、已失效撤回或过期"

            summaries.append(RunTraceSummary(
                run_id=run.id,
                state=run.state,
                requester_id=run.requester_id,
                tool_sequence=raw_tool_sequence,
                invocations=invocations,
                created_at=run.created_at,
                has_rejection=has_rejection,
                is_invalidated=is_invalidated,
                invalidation_reason=invalidation_reason,
                business_outcome="unknown",
                execution_failure=execution_failure,
                execution_unknown=execution_unknown,
            ))

        return summaries


@dataclass
class PatternSummary:
    pattern_id: str
    pattern_name: str
    tool_sequence: list[str]
    frequency: int  # count of verified business outcomes, NOT completed executions
    sample_run_ids: list[str]
    counterexample_run_ids: list[str]
    conflicts: list[str]
    is_viable: bool
    unviability_reason: str | None
    suggested_skill_key: str
    suggested_toolset_version: str
    unverified_run_ids: list[str] = field(default_factory=list)
    invalidated_run_ids: list[str] = field(default_factory=list)
    business_outcome: str = "unknown"


def discover_skill_patterns(
    session: Session,
    actor: AuthenticatedSubject,
    *,
    min_frequency: int = 2,
    limit: int = 50,
    since_days: int = 30,
    now: datetime | None = None,
) -> list[PatternSummary]:
    """Return bounded observations without fabricating business acceptance.

    A completed Run (even with only succeeded Steps) is execution evidence only.
    No authoritative, criterion-bound business-outcome producer is wired yet.
    This phase therefore cannot produce verified successes or viable drafts.
    The historical key-only grouping is diagnostic, not a versioned method.
    """
    _require_clean_session(session)
    current = _now(now)
    with session.no_autoflush:
        traces = extract_run_traces(session, actor, limit=limit, since_days=since_days, now=current)
    groups: dict[tuple[str, ...], dict[str, set[str]]] = {}
    for trace in traces:
        seq = tuple(trace.tool_sequence)
        sub_seqs = {seq[i:j] for i in range(len(seq)) for j in range(i + 2, len(seq) + 1)}
        for sub_seq in sub_seqs:
            group = groups.setdefault(sub_seq, {"unverified": set(), "failed": set(), "invalidated": set()})
            bucket = ("invalidated" if trace.is_invalidated else
                      "failed" if trace.execution_failure else "unverified")
            group[bucket].add(trace.run_id)
    patterns = []
    for seq, group in sorted(groups.items(), key=lambda item: (-len(item[0]), item[0])):
        unknown, failed, invalid = (sorted(group[k]) for k in ("unverified", "failed", "invalidated"))
        skill_key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"flow_{'_'.join(seq)[:40]}").strip("_")
        reason = ("原支撑记忆或证据已失效撤回，不可作为技能依据" if invalid and not unknown and not failed
                  else "业务验收未验证：执行记录仅供观察，不能计为成功样本或自动提案")
        patterns.append(PatternSummary(
            pattern_id="pat_" + sha256(":".join(seq).encode()).hexdigest()[:12],
            pattern_name=" -> ".join(seq), tool_sequence=list(seq), frequency=0,
            sample_run_ids=[], counterexample_run_ids=failed,
            conflicts=[f"Run {identity} 存在执行失败/取消或人工拒绝证据；不是已验证业务失败"
                       for identity in failed],
            is_viable=False, unviability_reason=reason, suggested_skill_key=skill_key,
            suggested_toolset_version="1.0.0", unverified_run_ids=unknown,
            invalidated_run_ids=invalid, business_outcome="unknown"))
    return patterns


def generate_skill_spec(
    pattern: PatternSummary,
    *,
    prompt_template: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """No success-labelled specification may be minted from an external summary.

    Phase B must add a trusted, scoped criterion/evidence receipt and revalidation
    before this can be enabled. Manual skill_service.create_draft is unchanged.
    """
    raise PatternDiscoveryError("pattern_business_outcome_unverified")


def _revalidate_pattern_sources(session, actor, pattern, current):
    """Re-read actual scoped evidence; caller summary fields confer no authority."""
    if (type(pattern) is not PatternSummary or type(pattern.tool_sequence) is not list
            or not 2 <= len(pattern.tool_sequence) <= 128
            or any(type(key) is not str or not 0 < len(key) <= 120 for key in pattern.tool_sequence)):
        raise PatternDiscoveryError("pattern_sources_invalid")
    buckets = [pattern.sample_run_ids, pattern.counterexample_run_ids,
               pattern.unverified_run_ids, pattern.invalidated_run_ids]
    if any(type(bucket) is not list for bucket in buckets):
        raise PatternDiscoveryError("pattern_sources_invalid")
    identities = [identity for bucket in buckets for identity in bucket]
    if (not 1 <= len(identities) <= 200 or any(type(identity) is not str or not 0 < len(identity) <= 120
            for identity in identities) or len(set(identities)) != len(identities)):
        raise PatternDiscoveryError("pattern_sources_invalid")
    with session.no_autoflush:
        traces = {trace.run_id: trace for trace in extract_run_traces(
            session, actor, limit=200, since_days=90, now=current)}
    wanted = tuple(pattern.tool_sequence)
    expected = {"unverified": set(), "failed": set(), "invalidated": set()}
    for identity in identities:
        trace = traces.get(identity)
        if trace is None or not any(tuple(trace.tool_sequence[i:i+len(wanted)]) == wanted
                                   for i in range(len(trace.tool_sequence)-len(wanted)+1)):
            raise PatternDiscoveryError("pattern_sources_changed")
        bucket = ("invalidated" if trace.is_invalidated else
                  "failed" if trace.execution_failure else "unverified")
        expected[bucket].add(identity)
    # Legacy/external success claims never become a trusted acceptance receipt.
    if pattern.sample_run_ids or type(pattern.frequency) is not int or pattern.frequency != 0:
        raise PatternDiscoveryError("pattern_business_outcome_unverified")
    if (set(pattern.unverified_run_ids) != expected["unverified"]
            or set(pattern.counterexample_run_ids) != expected["failed"]
            or set(pattern.invalidated_run_ids) != expected["invalidated"]
            or pattern.pattern_id != "pat_" + sha256(":".join(wanted).encode()).hexdigest()[:12]):
        raise PatternDiscoveryError("pattern_sources_changed")
    raise PatternDiscoveryError("pattern_business_outcome_unverified")


def propose_skill_draft_from_pattern(
    session: Session,
    actor: AuthenticatedSubject,
    pattern: PatternSummary,
    *,
    release_tag: str,
    provider_key: str = "deepseek",
    model: str = "deepseek-chat",
    prompt_version: str = "1.0.0",
    toolset_version: str = "1.0.0",
    reason: str,
    request_id: str,
    prompt_template: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Generates an unapproved SKL-001 draft version from a viable pattern.
    
    Fail-closed:
    - If pattern is not viable (insufficient samples, conflicts), raises PatternDiscoveryError.
    - If any sensitive commercial topic is present without permission, raises PatternDiscoveryError.
    - Draft enters SKL-001 in 'draft' state, strictly requiring independent human review.
    """
    _require_clean_session(session)
    current = _now(now)
    with session.no_autoflush:
        _require_human_actor(session, actor, PATTERN_PROPOSE_PERMISSION, current)

    # Validate that prompt template or reason does not leak unredacted restricted commercial information
    combined_text = f"{reason} {prompt_template or ''}"
    classification = classify_content(combined_text)
    if classification["is_restricted"]:
        can_read_restricted = False
        try:
            authorize_scope(session, actor, organization_id=actor.organization_id,
                            store_id=actor.store_id, permission="pricing.cost.read", now=current)
            can_read_restricted = True
        except Exception:
            can_read_restricted = False

        if not can_read_restricted:
            raise PatternDiscoveryError(
                "pattern_restricted_topic_unauthorized",
                "技能草案或理由中包含受限商业机密（如底价/进价/返点），未获授权不得创建技能"
            )

    _revalidate_pattern_sources(session, actor, pattern, current)
    raise PatternDiscoveryError("pattern_business_outcome_unverified")
