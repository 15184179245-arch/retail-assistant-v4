"""SKL-001: fail-closed, store-scoped skill release governance.

This is a host/service primitive, not a model-call tool or an HTTP release route.
The caller owns the transaction. No automatic runner selection is installed here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from hashlib import sha256
import json
import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agents.evals.provenance import results_digest
from agents.evals.scoring import cases_digest, score_suite
from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents.models import AgentRun, RunSkillBinding, SkillVersion, SkillResourceManifest
from backend.app.modules.agents import skill_resources as resources
from backend.app.modules.audit.repository import get_by_idempotency_key
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.models import Store, Subject
from backend.app.modules.identity.service import authorize_scope


class SkillReleaseError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_STATES = {"draft", "evaluated", "approved", "active", "rolled_back"}


def _fail(code: str):
    raise SkillReleaseError(code)


def _now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail("skill_datetime_invalid")
    return value.astimezone(timezone.utc)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _valid_text(value: str, maximum: int = 500) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        _fail("skill_text_invalid")
    return value.strip()


def _person_scope(session: Session, actor: AuthenticatedSubject, permission: str) -> None:
    if (not isinstance(actor, AuthenticatedSubject)
            or actor.account_id is None or actor.session_id is None
            or actor.account_version is None):
        raise HTTPException(401, "skill_employee_session_required")
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission=permission)
    kind = session.scalar(select(Subject.subject_type).where(
        Subject.id == actor.subject_id, Subject.organization_id == actor.organization_id))
    if kind != "person":
        raise HTTPException(403, "skill_person_required")


def _lock_store(session: Session, actor: AuthenticatedSubject) -> None:
    # No-op UPDATE serializes competing releases within the same store on SQLite/PG.
    changed = session.execute(update(Store).where(
        Store.id == actor.store_id, Store.organization_id == actor.organization_id,
        Store.active.is_(True)
    ).values(active=Store.active).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        _fail("skill_store_inactive")


def _load(session: Session, actor: AuthenticatedSubject, skill_id: str) -> SkillVersion:
    row = session.scalar(select(SkillVersion).where(
        SkillVersion.id == skill_id,
        SkillVersion.organization_id == actor.organization_id,
        SkillVersion.store_id == actor.store_id
    ).execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(404, "skill_not_found")
    _reject_resource(session, row)
    return row


def _active(session: Session, actor: AuthenticatedSubject, key: str) -> SkillVersion | None:
    row = session.scalar(select(SkillVersion).where(
        SkillVersion.organization_id == actor.organization_id,
        SkillVersion.store_id == actor.store_id,
        SkillVersion.skill_key == key, SkillVersion.state == "active"
    ).execution_options(populate_existing=True))
    if row is not None:
        _reject_resource(session, row)
    return row


def _reject_resource(session, row):
    if (row.spec_kind != resources.LEGACY or row.skill_key.startswith(resources.PREFIX)
            or session.get(SkillResourceManifest, row.id) is not None):
        _fail("skill_resource_not_ready")


def _command(session: Session, actor: AuthenticatedSubject, *, permission: str,
             operation: str, request_id: str, reason: str, payload: dict) -> tuple[str, dict | None]:
    _person_scope(session, actor, permission)
    _lock_store(session, actor)
    # Lock the scope before reading the idempotency receipt.
    _person_scope(session, actor, permission)
    # P1 resource releases cannot replay an old audit receipt around the gate.
    if operation in ("evaluate", "approve", "activate", "rollback"):
        _load(session, actor, payload["skill_id"])
    request_id = _valid_text(request_id, 200)
    reason = _valid_text(reason)
    key = "skill:" + _digest([actor.organization_id, actor.store_id,
                              actor.subject_id, request_id])
    request = {"reason": reason, **payload}
    existing = get_by_idempotency_key(session, actor.organization_id, actor.store_id, key)
    if existing is None:
        return key, None
    result = existing.details.get("result")
    if (existing.actor_id != actor.subject_id or existing.action != "skill." + operation
            or existing.target_type != "skill_version" or
            existing.details.get("request") != request or
            not isinstance(result, dict) or existing.target_id != result.get("skill_id")):
        _fail("skill_idempotency_conflict")
    return key, {**result, "replayed": True}


def _audit(session: Session, actor: AuthenticatedSubject, *, key: str, operation: str,
           row: SkillVersion, reason: str, payload: dict, result: dict, now: datetime) -> None:
    record_event(session, actor_id=actor.subject_id,
        organization_id=row.organization_id, store_id=row.store_id,
        action="skill." + operation, target_type="skill_version", target_id=row.id,
        idempotency_key=key, occurred_at=now,
        details={"request": {"reason": reason, **payload}, "result": result})


def _cas(session: Session, row: SkillVersion, expected_version: int,
         old_states: tuple[str, ...], now: datetime, **changes) -> SkillVersion:
    if type(expected_version) is not int or expected_version < 1:
        _fail("skill_version_invalid")
    if row.version != expected_version or row.state not in old_states:
        _fail("skill_version_conflict")
    changed = session.execute(update(SkillVersion).where(
        SkillVersion.id == row.id, SkillVersion.organization_id == row.organization_id,
        SkillVersion.store_id == row.store_id, SkillVersion.version == expected_version,
        SkillVersion.state == row.state,
    ).values(**changes, version=expected_version + 1, updated_at=now)
        .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        _fail("skill_version_conflict")
    session.refresh(row)
    return row


def _binding(row: SkillVersion) -> dict:
    return {"skill_id": row.id, "skill_key": row.skill_key, "release_tag": row.release_tag,
            "provider_key": row.provider_key, "model": row.model,
            "prompt_version": row.prompt_version, "toolset_version": row.toolset_version,
            "spec_sha256": row.spec_sha256, "version": row.version, "state": row.state}


def create_draft(session: Session, actor: AuthenticatedSubject, *, skill_key: str,
                 release_tag: str, provider_key: str, model: str,
                 prompt_version: str, toolset_version: str, spec_sha256: str,
                 reason: str, request_id: str, now: datetime | None = None) -> dict:
    current = _now(now)
    if isinstance(skill_key, str) and skill_key.startswith(resources.PREFIX):
        _fail("skill_resource_manifest_required")
    if (not all(isinstance(x, str) and _KEY.fullmatch(x)
                for x in (skill_key, release_tag, provider_key, prompt_version, toolset_version))
            or not isinstance(model, str) or not 1 <= len(model) <= 200
            or not isinstance(spec_sha256, str) or not _SHA.fullmatch(spec_sha256)):
        _fail("skill_binding_invalid")
    payload = {"skill_key": skill_key, "release_tag": release_tag,
               "provider_key": provider_key, "model": model,
               "prompt_version": prompt_version, "toolset_version": toolset_version,
               "spec_sha256": spec_sha256}
    key, replay = _command(session, actor, permission="skills.submit",
                           operation="draft", request_id=request_id,
                           reason=reason, payload=payload)
    if replay is not None:
        return replay
    conflict = session.scalar(select(SkillVersion.id).where(
        SkillVersion.organization_id == actor.organization_id,
        SkillVersion.store_id == actor.store_id,
        SkillVersion.skill_key == skill_key, SkillVersion.release_tag == release_tag))
    if conflict is not None:
        _fail("skill_release_exists")
    row = SkillVersion(id=str(uuid4()), organization_id=actor.organization_id,
        store_id=actor.store_id, skill_key=skill_key, release_tag=release_tag,
        provider_key=provider_key, model=model, prompt_version=prompt_version,
        toolset_version=toolset_version, spec_sha256=spec_sha256,
        created_by=actor.subject_id, state="draft", version=1,
        created_at=current, updated_at=current)
    session.add(row)
    session.flush()
    result = {"skill_id": row.id, "state": row.state, "version": row.version}
    _audit(session, actor, key=key, operation="draft", row=row,
           reason=reason, payload=payload, result=result, now=current)
    session.flush()
    return {**result, "replayed": False}


def create_resource_draft(session: Session, actor: AuthenticatedSubject, *,
                          manifest: dict | str, provider_key: str, model: str,
                          prompt_version: str, toolset_version: str,
                          reason: str, request_id: str, now: datetime | None = None) -> dict:
    """P1-A atomic DRAFT only. Does not evaluate, approve, load or execute resources."""
    if session.new or session.dirty or session.deleted:
        _fail("skill_resource_clean_session_required")
    # A clean retained Session can still hold committed, obsolete auth rows.
    # Refresh only authority facts before creation OR idempotent replay.
    from backend.app.modules.identity.command_guard import refresh_authority
    refresh_authority(session)
    current = _now(now)
    _person_scope(session, actor, "skills.submit")
    if (not all(isinstance(x, str) and _KEY.fullmatch(x)
                for x in (provider_key, prompt_version, toolset_version))
            or not isinstance(model, str) or not 1 <= len(model) <= 200):
        _fail("skill_binding_invalid")
    try:
        value, canonical, spec_sha = resources.canonical_manifest(manifest)
        payload = {"manifest": value, "provider_key": provider_key, "model": model,
                   "prompt_version": prompt_version, "toolset_version": toolset_version,
                   "spec_sha256": spec_sha}
        # SQLite legacy transaction mode does not BEGIN for the reads above.
        # A top-level SAVEPOINT would otherwise commit on RELEASE and escape the
        # caller's outer rollback. Match the established knowledge ACL pattern.
        connection = session.connection()
        if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")
        # Savepoint prevents a caught exception from committing an orphan draft.
        # No caller-owned pending work can be flushed by begin_nested or ACL helpers.
        with session.begin_nested():
            key, replay = _command(session, actor, permission="skills.submit",
                operation="resource_draft", request_id=request_id, reason=reason, payload=payload)
            entry, revision = resources.validate_public_source(session, actor, value, now=current)
            if replay is not None:
                row = session.get(SkillVersion, replay["skill_id"], populate_existing=True)
                saved = session.get(SkillResourceManifest, replay["skill_id"], populate_existing=True)
                if (row is None or saved is None or row.spec_kind != resources.KIND
                        or row.state != "draft" or row.version != 1
                        or (row.organization_id, row.store_id) != (actor.organization_id, actor.store_id)
                        or (saved.organization_id, saved.store_id) != (actor.organization_id, actor.store_id)
                        or saved.canonical_manifest != canonical or saved.spec_sha256 != spec_sha
                        or row.spec_sha256 != spec_sha or row.skill_key != value["skill_key"]
                        or row.release_tag != value["release_tag"]
                        or (row.provider_key, row.model, row.prompt_version, row.toolset_version)
                           != (provider_key, model, prompt_version, toolset_version)
                        or (saved.knowledge_entry_id, saved.revision_id) != (entry.id, revision.id)):
                    _fail("skill_resource_replay_invalid")
                return replay
            exists = session.scalar(select(SkillVersion.id).where(
                SkillVersion.organization_id == actor.organization_id, SkillVersion.store_id == actor.store_id,
                SkillVersion.skill_key == value["skill_key"], SkillVersion.release_tag == value["release_tag"]))
            if exists is not None:
                _fail("skill_release_exists")
            row = SkillVersion(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
                skill_key=value["skill_key"], release_tag=value["release_tag"], spec_kind=resources.KIND,
                spec_sha256=spec_sha, provider_key=provider_key, model=model, prompt_version=prompt_version,
                toolset_version=toolset_version, state="draft", version=1, created_by=actor.subject_id,
                created_at=current, updated_at=current)
            session.add(row)
            session.flush()
            session.add(SkillResourceManifest(skill_id=row.id, organization_id=actor.organization_id,
                store_id=actor.store_id, knowledge_entry_id=entry.id, revision_id=revision.id,
                canonical_manifest=canonical, spec_sha256=spec_sha, created_at=current))
            result = {"skill_id": row.id, "state": "draft", "version": 1,
                      "spec_kind": resources.KIND, "spec_sha256": spec_sha}
            _audit(session, actor, key=key, operation="resource_draft", row=row, reason=reason,
                   payload=payload, result=result, now=current)
            session.flush()
            return {**result, "replayed": False}
    except resources.ResourceManifestError as error:
        raise SkillReleaseError(error.code) from error


def evaluate_draft(session: Session, actor: AuthenticatedSubject, *, skill_id: str,
                   expected_version: int, cases: list[dict], results: dict,
                   metadata: dict, reason: str, request_id: str,
                   now: datetime | None = None) -> dict:
    current = _now(now)
    if not isinstance(cases, list) or not isinstance(results, dict) or not isinstance(metadata, dict):
        _fail("skill_evidence_invalid")
    try:
        evid = {"cases_sha256": cases_digest(cases),
                "results_sha256": results_digest(results),
                "metadata_sha256": _digest(metadata)}
    except (TypeError, ValueError, KeyError):
        _fail("skill_evidence_invalid")
    payload = {"skill_id": skill_id, "expected_version": expected_version, **evid}
    key, replay = _command(session, actor, permission="skills.evaluate",
                           operation="evaluate", request_id=request_id,
                           reason=reason, payload=payload)
    if replay is not None:
        return replay
    row = _load(session, actor, skill_id)
    if row.state != "draft":
        _fail("skill_state_conflict")
    if (metadata.get("model") != row.model or metadata.get("prompt_version") != row.prompt_version
            or metadata.get("toolset_version") != row.toolset_version
            or metadata.get("provider_key") != row.provider_key):
        _fail("skill_eval_binding_mismatch")
    score = score_suite(cases, results, metadata)
    if (not score["active"] or score["total"] != len(results) or
            score["total"] < 1 or score["passed"] != score["total"]):
        _fail("skill_eval_failed")
    snapshot = {**evid, "eval_version": score["eval_version"], "mode": score["mode"],
        "model": score["model"], "provider_key": row.provider_key,
        "spec_sha256": row.spec_sha256, "prompt_version": score["prompt_version"],
        "toolset_version": score["toolset_version"], "total": score["total"],
        "passed": score["passed"], "case_ids": [item["id"] for item in score["cases"]],
        "provenance_verified": score["provenance_verified"],
        "eligible_for_activation": score["mode"] == "online" and score["provenance_verified"]}
    digest = _digest(snapshot)
    _cas(session, row, expected_version, ("draft",), current,
         state="evaluated", evaluated_by=actor.subject_id,
         evaluation_digest=digest, evaluation_snapshot=snapshot)
    result = {"skill_id": row.id, "state": row.state, "version": row.version,
              "evaluation_digest": digest, "eligible_for_activation": snapshot["eligible_for_activation"]}
    _audit(session, actor, key=key, operation="evaluate", row=row,
           reason=reason, payload=payload, result=result, now=current)
    session.flush()
    return {**result, "replayed": False}


def approve_version(session: Session, actor: AuthenticatedSubject, *, skill_id: str,
                    expected_version: int, reason: str, request_id: str,
                    now: datetime | None = None) -> dict:
    current = _now(now)
    payload = {"skill_id": skill_id, "expected_version": expected_version}
    key, replay = _command(session, actor, permission="skills.approve",
                           operation="approve", request_id=request_id, reason=reason, payload=payload)
    if replay is not None:
        return replay
    row = _load(session, actor, skill_id)
    if actor.subject_id in (row.created_by, row.evaluated_by):
        _fail("skill_separation_of_duties")
    if row.state != "evaluated" or not _trusted_evaluation(row, require_online=False):
        _fail("skill_evaluation_required")
    _cas(session, row, expected_version, ("evaluated",), current,
         state="approved", approved_by=actor.subject_id)
    result = {"skill_id": row.id, "state": row.state, "version": row.version}
    _audit(session, actor, key=key, operation="approve", row=row,
           reason=reason, payload=payload, result=result, now=current)
    session.flush()
    return {**result, "replayed": False}


def _trusted_evaluation(row: SkillVersion, *, require_online: bool) -> bool:
    if row.spec_kind != resources.LEGACY or row.skill_key.startswith(resources.PREFIX):
        return False
    snapshot = row.evaluation_snapshot
    if not isinstance(snapshot, dict) or not isinstance(row.evaluation_digest, str):
        return False
    try:
        if _digest(snapshot) != row.evaluation_digest:
            return False
    except (TypeError, ValueError):
        return False
    return bool(row.evaluated_by is not None
                and snapshot.get("passed") == snapshot.get("total")
                and type(snapshot.get("passed")) is int and snapshot["passed"] >= 1
                and isinstance(snapshot.get("case_ids"), list)
                and len(snapshot["case_ids"]) == snapshot["total"]
                and all(isinstance(item, str) and item for item in snapshot["case_ids"])
                and len(set(snapshot["case_ids"])) == snapshot["total"]
                and snapshot.get("model") == row.model
                and snapshot.get("provider_key") == row.provider_key
                and snapshot.get("spec_sha256") == row.spec_sha256
                and snapshot.get("prompt_version") == row.prompt_version
                and snapshot.get("toolset_version") == row.toolset_version
                and (not require_online or
                     (snapshot.get("mode") == "online"
                      and snapshot.get("provenance_verified") is True
                      and snapshot.get("eligible_for_activation") is True)))


def _publish(session: Session, actor: AuthenticatedSubject, *, skill_id: str,
             expected_version: int, reason: str, request_id: str,
             operation: str, now: datetime) -> dict:
    payload = {"skill_id": skill_id, "expected_version": expected_version}
    key, replay = _command(session, actor, permission="skills.activate",
                           operation=operation, request_id=request_id,
                           reason=reason, payload=payload)
    if replay is not None:
        return replay
    row = _load(session, actor, skill_id)
    source_state = "approved" if operation == "activate" else "rolled_back"
    if row.state != source_state:
        _fail("skill_state_conflict")
    if row.created_by == actor.subject_id:
        _fail("skill_separation_of_duties")
    if (row.approved_by is None or row.approved_by in (row.created_by, row.evaluated_by)
            or not _trusted_evaluation(row, require_online=True)):
        _fail("skill_online_evaluation_required")
    former = _active(session, actor, row.skill_key)
    if operation == "rollback" and former is None:
        _fail("skill_rollback_requires_active")
    if former is not None and former.id == row.id:
        _fail("skill_already_active")
    if former is not None:
        if not _trusted_evaluation(former, require_online=True):
            _fail("skill_current_evaluation_invalid")
        if former.evaluation_snapshot.get("cases_sha256") != row.evaluation_snapshot.get("cases_sha256"):
            _fail("skill_eval_suite_regression")
    # Retire first: database partial unique index also guards concurrent activates.
    if former is not None:
        _cas(session, former, former.version, ("active",), now, state="rolled_back")
    _cas(session, row, expected_version, (source_state,), now,
         state="active", activated_by=actor.subject_id)
    result = {"skill_id": row.id, "state": row.state, "version": row.version,
              "superseded_skill_id": former.id if former else None,
              "evaluation_digest": row.evaluation_digest}
    _audit(session, actor, key=key, operation=operation, row=row,
           reason=reason, payload=payload, result=result, now=now)
    session.flush()
    return {**result, "replayed": False}


def activate_version(session: Session, actor: AuthenticatedSubject, *, skill_id: str,
                     expected_version: int, reason: str, request_id: str,
                     now: datetime | None = None) -> dict:
    return _publish(session, actor, skill_id=skill_id, expected_version=expected_version,
                    reason=reason, request_id=request_id, operation="activate", now=_now(now))


def rollback_to_version(session: Session, actor: AuthenticatedSubject, *, skill_id: str,
                        expected_version: int, reason: str, request_id: str,
                        now: datetime | None = None) -> dict:
    return _publish(session, actor, skill_id=skill_id, expected_version=expected_version,
                    reason=reason, request_id=request_id, operation="rollback", now=_now(now))


def current_binding(session: Session, actor: AuthenticatedSubject, *, skill_key: str) -> dict:
    """Explicit read-only lookup; no fallback to a draft or implicit active upgrade."""
    _person_scope(session, actor, "agents.run")
    if not isinstance(skill_key, str) or not _KEY.fullmatch(skill_key):
        _fail("skill_key_invalid")
    row = _active(session, actor, skill_key)
    if row is None or not _trusted_evaluation(row, require_online=True):
        _fail("skill_active_release_unavailable")
    return _binding(row)


def verify_run_binding(session: Session, actor: AuthenticatedSubject, *,
                       run_id: str, skill_id: str) -> dict:
    """Verify, never rewrite, an existing run's four persisted binding fields."""
    _person_scope(session, actor, "agents.run")
    row = _load(session, actor, skill_id)
    run = session.scalar(select(AgentRun).where(
        AgentRun.id == run_id, AgentRun.organization_id == actor.organization_id,
        AgentRun.store_id == actor.store_id, AgentRun.requester_id == actor.subject_id))
    if run is None:
        raise HTTPException(404, "skill_run_not_found")
    if (row.state not in ("active", "rolled_back") or row.approved_by is None
            or not _trusted_evaluation(row, require_online=True)
            or (run.provider_key, run.model, run.prompt_version, run.toolset_version)
            != (row.provider_key, row.model, row.prompt_version, row.toolset_version)):
        _fail("skill_run_binding_mismatch")
    return _binding(row)


# SKL-003: host-owned instruction templates. The SKL-001 database stores
# ONLY the content digest, not executable spec bytes. Never turn arbitrary
# uploaded skill metadata or tool/model text into an execution instruction.
# Any different digest is NOT the reviewed executable template.
_RUNTIME_DOMAINS = {
    "catalog-lookup": {
        "phrases": ("查商品", "商品查询", "找商品", "查询条码", "查条码"),
        "specialty": "catalog", "task_kind": "catalog_lookup",
        "tool": ("catalog.search", "1"), "toolset_version": "catalog-read-v1",
        "instruction": "只用已授权的商品候选搜索；不能猜测库存、价格或规格，必须让员工确认。",
    },
    "knowledge-lookup": {
        "phrases": ("查知识", "知识查询", "查询流程", "查询规定", "查操作规程"),
        "specialty": "knowledge", "task_kind": "knowledge_lookup",
        "tool": ("knowledge.search", "1"), "toolset_version": "knowledge-read-v1",
        "instruction": "只用同店、已审核、仍有效且有引用的知识；不足时明确无法确认。",
    },
    "task-lookup": {
        "phrases": ("我的任务", "查看任务", "查询待办", "查待办", "我的待办"),
        "specialty": "tasks", "task_kind": "own_tasks",
        "tool": ("tasks.list_mine", "1"), "toolset_version": "tasks-read-v1",
        "instruction": "只读取当前登录员工本人任务摘要；不得分派、批准或代为修改任务。",
    },
}


def host_runtime_spec(skill_key: str) -> dict:
    """Immutable, auditable host recipe. No user-provided executable bytes."""
    entry = _RUNTIME_DOMAINS.get(skill_key)
    if entry is None:
        _fail("skill_runtime_unknown_template")
    return {
        "schema_version": 1, "skill_key": skill_key,
        "tool_key": entry["tool"][0], "tool_version": entry["tool"][1],
        "instruction": entry["instruction"],
    }


def host_runtime_digest(skill_key: str) -> str:
    return _digest(host_runtime_spec(skill_key))


def runtime_intent(text: str) -> tuple[str | None, str]:
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
        return None, "invalid_input"
    # Short phrases are intentionally exact and deterministic. Model text,
    # knowledge content and arbitrary role names never grant permissions.
    matched = [
        key for key, entry in _RUNTIME_DOMAINS.items()
        if any(phrase in text for phrase in entry["phrases"])
    ]
    if not matched:
        return None, "low_confidence"
    if len(matched) != 1:
        return None, "ambiguous_intent"
    return matched[0], "exact_trusted_phrase"


def choose_runtime_skill(session: Session, actor: AuthenticatedSubject,
                         agent: AuthenticatedSubject, text: str, *,
                         provider_key: str, model: str,
                         prompt_version: str) -> tuple[dict | None, str]:
    """Select only currently-active, signed, host-matched, single-read-tool skills.

    A negative result is not permission to execute an unapproved skill; caller
    may fall back to its existing ordinary Agent flow.
    """
    if os.environ.get("AGENT_SKILL_RUNTIME_ENABLED") != "1":
        return None, "feature_disabled"
    key, reason = runtime_intent(text)
    if key is None:
        return None, reason
    entry = _RUNTIME_DOMAINS[key]
    from backend.app.modules.agents.routing import RoleRoutingError, select_role
    # Revalidate the real employee session, Agent subject and BOTH tool grants
    # through AGT-012. This is a scope filter, never a permission grant.
    try:
        plan = select_role(session, requester=actor, agent=agent,
                           role_key="specialist", specialty=entry["specialty"],
                           task_kind=entry["task_kind"])
    except (RoleRoutingError, HTTPException):
        return None, "role_scope_or_permission_denied"
    if plan.allowed_tools != (entry["tool"],):
        return None, "tool_capability_missing"
    try:
        binding = current_binding(session, actor, skill_key=key)
    except (SkillReleaseError, HTTPException):
        return None, "skill_not_active_or_untrusted"
    if (binding["spec_sha256"] != host_runtime_digest(key)
            or binding["toolset_version"] != entry["toolset_version"]
            or (binding["provider_key"], binding["model"], binding["prompt_version"])
               != (provider_key, model, prompt_version)):
        return None, "skill_host_binding_mismatch"
    if type(binding.get("version")) is not int or binding["version"] < 1:
        return None, "skill_version_invalid"
    row = _load(session, actor, binding["skill_id"])
    if (row.state != "active" or row.approved_by is None
            or row.activated_by is None
            or row.approved_by in (row.created_by, row.evaluated_by)
            or not _trusted_evaluation(row, require_online=True)):
        return None, "skill_not_active_or_untrusted"
    return {
        "binding": binding, "evaluation_digest": row.evaluation_digest,
        "plan": plan, "tool_descriptions": list(plan.tool_descriptions),
        "instruction": entry["instruction"], "reason": reason,
    }, "selected"


def freeze_run_skill(session: Session, actor: AuthenticatedSubject,
                     run: AgentRun, choice: dict) -> RunSkillBinding:
    """Write one immutable pin in the same transaction as the new Run."""
    if run.requester_id != actor.subject_id or (
            run.organization_id, run.store_id) != (
            actor.organization_id, actor.store_id):
        _fail("skill_runtime_scope_denied")
    b = choice["binding"]
    # Publication/rollback uses this SAME scoped Store write-lock. Serialize
    # the frozen insertion with any competing skill release transition.
    _person_scope(session, actor, "agents.run")
    _lock_store(session, actor)
    _person_scope(session, actor, "agents.run")
    live = _load(session, actor, b["skill_id"])
    if (live.state != "active" or live.version != b["version"]
            or live.skill_key != b["skill_key"]
            or live.release_tag != b["release_tag"]
            or live.spec_sha256 != b["spec_sha256"]
            or (live.provider_key, live.model, live.prompt_version, live.toolset_version)
               != (b["provider_key"], b["model"], b["prompt_version"], b["toolset_version"])
            or live.evaluation_digest != choice["evaluation_digest"]
            or live.approved_by is None or live.activated_by is None
            or live.approved_by in (live.created_by, live.evaluated_by)
            or not _trusted_evaluation(live, require_online=True)):
        _fail("skill_runtime_release_changed")
    key = b["skill_key"]
    entry = _RUNTIME_DOMAINS.get(key)
    plan = choice["plan"]
    if (entry is None or b["spec_sha256"] != host_runtime_digest(key)
            or plan.allowed_tools != (entry["tool"],)
            or (run.provider_key, run.model, run.prompt_version, run.toolset_version)
               != (b["provider_key"], b["model"], b["prompt_version"], b["toolset_version"])):
        _fail("skill_runtime_binding_invalid")
    row = RunSkillBinding(
        run_id=run.id, organization_id=run.organization_id,
        store_id=run.store_id, skill_id=b["skill_id"],
        skill_key=key, release_tag=b["release_tag"],
        spec_sha256=b["spec_sha256"], evaluation_digest=choice["evaluation_digest"],
        skill_version=b["version"], provider_key=run.provider_key,
        model=run.model, prompt_version=run.prompt_version,
        toolset_version=run.toolset_version, role_key="specialist",
        specialty=entry["specialty"], role_spec_sha256=plan.config_digest,
        tool_contract_sha256=_digest(choice["tool_descriptions"]),
        allowed_tools=[{"key": entry["tool"][0], "version": entry["tool"][1]}],
        selection_reason=choice["reason"], created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def runtime_pin(session: Session, run: AgentRun) -> RunSkillBinding | None:
    return session.scalar(select(RunSkillBinding).where(
        RunSkillBinding.run_id == run.id,
        RunSkillBinding.organization_id == run.organization_id,
        RunSkillBinding.store_id == run.store_id,
    ).execution_options(populate_existing=True))


def verify_runtime_binding(session: Session, actor: AuthenticatedSubject,
                           agent: AuthenticatedSubject, run: AgentRun
                           ) -> RunSkillBinding | None:
    """Strict runtime/restore fence: never upgrade, change skill, or grant tools."""
    session.flush()
    session.expire_all()  # stale ORM grants/skill statuses are NOT authority
    pin = runtime_pin(session, run)
    if pin is None:
        return None
    if os.environ.get("AGENT_SKILL_RUNTIME_ENABLED") != "1":
        _fail("skill_runtime_disabled")
    if run.requester_id != actor.subject_id or (
            run.organization_id, run.store_id) != (
            actor.organization_id, actor.store_id):
        _fail("skill_runtime_scope_denied")
    entry = _RUNTIME_DOMAINS.get(pin.skill_key)
    if (entry is None or pin.role_key != "specialist"
            or pin.specialty != entry["specialty"]
            or pin.spec_sha256 != host_runtime_digest(pin.skill_key)
            or pin.allowed_tools != [{"key": entry["tool"][0],
                                      "version": entry["tool"][1]}]):
        _fail("skill_runtime_pin_invalid")
    if (pin.provider_key, pin.model, pin.prompt_version, pin.toolset_version) != (
            run.provider_key, run.model, run.prompt_version, run.toolset_version):
        _fail("skill_runtime_pin_mismatch")
    # The original SkillVersion ID, digest, release and ACTIVE state must
    # still match. A later release does not silently swap this Run's skill.
    try:
        _person_scope(session, actor, "agents.run")
        _lock_store(session, actor)
        _person_scope(session, actor, "agents.run")
        row = _load(session, actor, pin.skill_id)
    except HTTPException as exc:
        raise SkillReleaseError("skill_runtime_scope_revoked") from exc
    if (row.state != "active" or row.version != pin.skill_version
            or row.approved_by is None or row.activated_by is None
            or row.approved_by in (row.created_by, row.evaluated_by)
            or row.skill_key != pin.skill_key or row.release_tag != pin.release_tag
            or row.spec_sha256 != pin.spec_sha256
            or row.evaluation_digest != pin.evaluation_digest
            or (row.provider_key, row.model, row.prompt_version, row.toolset_version)
               != (pin.provider_key, pin.model, pin.prompt_version, pin.toolset_version)
            or not _trusted_evaluation(row, require_online=True)):
        _fail("skill_runtime_release_changed")
    from backend.app.modules.agents.routing import RoleRoutingError, select_role
    try:
        plan = select_role(session, requester=actor, agent=agent,
                           role_key="specialist", specialty=pin.specialty,
                           task_kind=entry["task_kind"])
    except (RoleRoutingError, HTTPException) as exc:
        raise SkillReleaseError("skill_runtime_role_revoked") from exc
    if (plan.config_digest != pin.role_spec_sha256
            or _digest(list(plan.tool_descriptions)) != pin.tool_contract_sha256
            or plan.allowed_tools != (entry["tool"],)):
        _fail("skill_runtime_tool_revoked")
    return pin


def propose_candidate_draft_from_pattern(
    session: Session,
    actor: AuthenticatedSubject,
    pattern: Any,
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
) -> dict:
    """SKL-002: Propose a draft skill version from discovered workflow pattern.

    The created version is always in 'draft' state and requires human evaluation & review.
    """
    from backend.app.modules.agents.skill_pattern_service import propose_skill_draft_from_pattern
    return propose_skill_draft_from_pattern(
        session, actor, pattern, release_tag=release_tag,
        provider_key=provider_key, model=model, prompt_version=prompt_version,
        toolset_version=toolset_version, reason=reason, request_id=request_id,
        prompt_template=prompt_template, now=now,
    )

