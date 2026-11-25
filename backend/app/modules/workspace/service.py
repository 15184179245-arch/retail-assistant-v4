"""Business logic for WSP-001 workspace artifacts, L1 locks, partial undo, submit, and thread workspace."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit import service as audit_service
from backend.app.modules.identity.service import authorize_scope, resolve_effective_capabilities
from backend.app.modules.workspace.models import (
    WorkspaceArtifact,
    WorkspaceArtifactOp,
    WorkspaceThreadBinding,
    WorkspaceThreadEvent,
)
from backend.app.modules.workspace.schemas import (
    ArtifactCreateRequest,
    ArtifactDetailData,
    ArtifactLockRequest,
    ArtifactMutationData,
    ArtifactOpRecord,
    ArtifactPatchRequest,
    ArtifactRecord,
    ArtifactSubmitRequest,
    ArtifactSuggestionDecisionRequest,
    ArtifactUndoRequest,
    ArtifactUnlockRequest,
    PendingSuggestionRecord,
    ThreadEventRecord,
    ThreadEventsData,
    ThreadWorkspaceRecord,
    ThreadWorkspaceUpsertRequest,
    validate_artifact_payload,
)

# Kind -> (read permissions, write permissions, submit permissions).
# Each slot lists accepted permissions (any match satisfies capability).
_KIND_PERMISSION_MAP: dict[str, dict[str, tuple[str, ...]]] = {
    "quote_draft": {
        "read": ("sales.read",),
        "write": ("sales.draft",),
        "submit": ("sales.draft", "sales.confirm"),
    },
    "purchase_draft": {
        "read": ("purchasing.read",),
        "write": ("purchasing.write",),
        "submit": ("purchasing.write",),
    },
    "stocktake_review": {
        "read": ("stocktake.read",),
        "write": ("stocktake.write",),
        "submit": ("stocktake.write",),
    },
    "receiving_batch": {
        "read": ("receiving.read",),
        "write": ("receiving.write",),
        "submit": ("receiving.write", "receiving.review"),
    },
    "work_item_sheet": {
        "read": ("tasks.read_self", "tasks.read"),
        "write": ("tasks.suggest", "tasks.manage"),
        "submit": ("tasks.suggest", "tasks.manage"),
    },
}


class WorkspaceError(Exception):
    """Domain error carrying canonical 6-field ApiEnvelope fields and HTTP status."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        error: str,
        user_message: str,
        hint: str | None = None,
        data: Any = None,
    ) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.code = code
        self.error = error
        self.user_message = user_message
        self.hint = hint
        self.data = data


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _payload_digest(value: Any) -> str:
    import json

    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _authorize_store_member(
    session: Session,
    actor: AuthenticatedSubject,
    *,
    permission: str | None = None,
) -> None:
    try:
        authorize_scope(
            session,
            actor,
            organization_id=actor.organization_id,
            store_id=actor.store_id,
            permission=permission,
        )
    except HTTPException as exc:
        if exc.status_code == 403:
            raise WorkspaceError(
                status_code=403,
                code="FORBIDDEN",
                error=str(exc.detail),
                user_message="当前账号缺少访问该门店工作区的权限",
                hint="请确认门店归属与功能授权后重试",
            ) from exc
        raise


def require_kind_permission(
    session: Session,
    actor: AuthenticatedSubject,
    kind: str,
    action: str,
) -> None:
    """Enforce store scope and kind-specific domain capability from KNOWN_PERMISSIONS."""
    _authorize_store_member(session, actor)
    per_kind = _KIND_PERMISSION_MAP.get(kind)
    if per_kind is None:
        raise WorkspaceError(
            status_code=422,
            code="UNSUPPORTED_ARTIFACT_KIND",
            error=f"unsupported_kind:{kind}",
            user_message=f"不支持的工作区对象类型：{kind}",
        )
    accepted = per_kind[action]
    caps, _ = resolve_effective_capabilities(session, actor)
    cap_set = set(caps)
    if not any(perm in cap_set for perm in accepted):
        raise WorkspaceError(
            status_code=403,
            code="FORBIDDEN",
            error=f"permission_denied:{'|'.join(accepted)}",
            user_message="当前员工无权执行该工作区对象操作",
            hint=f"需要权限：{' 或 '.join(accepted)}",
        )


def _to_artifact_record(row: WorkspaceArtifact) -> ArtifactRecord:
    return ArtifactRecord(
        id=row.id,
        organization_id=row.organization_id,
        store_id=row.store_id,
        owner_subject_id=row.owner_subject_id,
        thread_id=row.thread_id,
        kind=row.kind,  # type: ignore[arg-type]
        title=row.title,
        status=row.status,  # type: ignore[arg-type]
        version=row.version,
        payload=copy.deepcopy(row.payload or {}),
        source_refs=copy.deepcopy(row.source_refs or []),
        locked_fields=list(row.locked_fields or []),
        pending_suggestions=[
            PendingSuggestionRecord.model_validate(item)
            for item in (row.pending_suggestions or [])
        ],
        submitted_receipt_ref=row.submitted_receipt_ref,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_op_record(row: WorkspaceArtifactOp) -> ArtifactOpRecord:
    return ArtifactOpRecord(
        id=row.id,
        artifact_id=row.artifact_id,
        version_before=row.version_before,
        version_after=row.version_after,
        actor_type=row.actor_type,  # type: ignore[arg-type]
        actor_subject_id=row.actor_subject_id,
        op_type=row.op_type,  # type: ignore[arg-type]
        patch=copy.deepcopy(row.patch or {}),
        applied_fields=list(row.applied_fields or []),
        skipped_fields=list(row.skipped_fields or []),
        undo_of_op_id=row.undo_of_op_id,
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
    )


def _emit_thread_event(
    session: Session,
    *,
    organization_id: str,
    store_id: str,
    thread_id: str,
    subject_id: str,
    artifact_id: str | None,
    event_type: str,
    payload: dict[str, Any],
    now: datetime,
) -> WorkspaceThreadEvent:
    event = WorkspaceThreadEvent(
        event_id=_new_id("wev"),
        organization_id=organization_id,
        store_id=store_id,
        thread_id=thread_id,
        subject_id=subject_id,
        artifact_id=artifact_id,
        event_type=event_type,
        payload=copy.deepcopy(payload),
        created_at=now,
    )
    session.add(event)
    session.flush()
    return event


def _touch_binding_editor_hint(
    session: Session,
    *,
    organization_id: str,
    store_id: str,
    thread_id: str,
    subject_id: str,
    device: str,
    now: datetime,
) -> None:
    binding = session.execute(
        select(WorkspaceThreadBinding).where(
            WorkspaceThreadBinding.organization_id == organization_id,
            WorkspaceThreadBinding.store_id == store_id,
            WorkspaceThreadBinding.thread_id == thread_id,
            WorkspaceThreadBinding.subject_id == subject_id,
        )
    ).scalar_one_or_none()
    if binding is not None:
        binding.last_editor_subject_id = subject_id
        binding.last_editor_device = device
        binding.last_edited_at = now
        binding.updated_at = now


def _load_owned_artifact(
    session: Session,
    actor: AuthenticatedSubject,
    artifact_id: str,
) -> WorkspaceArtifact:
    artifact = session.execute(
        select(WorkspaceArtifact).where(WorkspaceArtifact.id == artifact_id)
    ).scalar_one_or_none()
    if artifact is None:
        raise WorkspaceError(
            status_code=404,
            code="ARTIFACT_NOT_FOUND",
            error=f"artifact_not_found:{artifact_id}",
            user_message="未找到对应的工作区对象",
        )
    if artifact.organization_id != actor.organization_id or artifact.store_id != actor.store_id:
        raise WorkspaceError(
            status_code=403,
            code="FORBIDDEN",
            error="cross_store_artifact_access_denied",
            user_message="无权访问其他门店或组织的工作区对象",
        )
    if artifact.owner_subject_id != actor.subject_id:
        raise WorkspaceError(
            status_code=403,
            code="FORBIDDEN",
            error="cross_employee_artifact_access_denied",
            user_message="无权访问或修改其他员工的工作区对象",
        )
    return artifact


def _check_thread_ownership(
    session: Session,
    actor: AuthenticatedSubject,
    thread_id: str,
) -> None:
    bindings = session.execute(
        select(WorkspaceThreadBinding).where(WorkspaceThreadBinding.thread_id == thread_id)
    ).scalars().all()
    same_scope_binding = None
    for row in bindings:
        if row.organization_id == actor.organization_id and row.store_id == actor.store_id:
            same_scope_binding = row
            break
    if same_scope_binding is not None:
        if same_scope_binding.subject_id != actor.subject_id:
            raise WorkspaceError(
                status_code=403,
                code="FORBIDDEN",
                error="cross_employee_thread_access_denied",
                user_message="无权访问其他员工的线程工作区",
            )
        return
    if bindings:
        raise WorkspaceError(
            status_code=403,
            code="FORBIDDEN",
            error="cross_store_thread_access_denied",
            user_message="无权访问其他门店或组织的线程工作区",
        )
    artifacts = session.execute(
        select(WorkspaceArtifact).where(WorkspaceArtifact.thread_id == thread_id)
    ).scalars().all()
    for art in artifacts:
        if art.organization_id != actor.organization_id or art.store_id != actor.store_id:
            raise WorkspaceError(
                status_code=403,
                code="FORBIDDEN",
                error="cross_store_thread_access_denied",
                user_message="无权访问其他门店或组织的线程工作区",
            )
        if art.owner_subject_id != actor.subject_id:
            raise WorkspaceError(
                status_code=403,
                code="FORBIDDEN",
                error="cross_employee_thread_access_denied",
                user_message="无权访问其他员工的线程工作区",
            )


def _find_idempotent_op(
    session: Session,
    *,
    organization_id: str,
    store_id: str,
    artifact_id: str | None,
    op_type: str,
    idempotency_key: str | None,
) -> WorkspaceArtifactOp | None:
    if not idempotency_key:
        return None
    stmt = select(WorkspaceArtifactOp).where(
        WorkspaceArtifactOp.organization_id == organization_id,
        WorkspaceArtifactOp.store_id == store_id,
        WorkspaceArtifactOp.op_type == op_type,
        WorkspaceArtifactOp.idempotency_key == idempotency_key,
    )
    if artifact_id is not None:
        stmt = stmt.where(WorkspaceArtifactOp.artifact_id == artifact_id)
    return session.execute(stmt).scalar_one_or_none()


def create_artifact(
    session: Session,
    actor: AuthenticatedSubject,
    req: ArtifactCreateRequest,
    *,
    header_idempotency_key: str | None = None,
) -> ArtifactMutationData:
    require_kind_permission(session, actor, req.kind, "write")
    _check_thread_ownership(session, actor, req.thread_id)
    idem_key = req.idempotency_key or header_idempotency_key

    try:
        normalized_payload = validate_artifact_payload(req.kind, req.payload, apply_defaults=True)
    except ValueError as exc:
        raise WorkspaceError(
            status_code=422,
            code="INVALID_PAYLOAD",
            error=str(exc),
            user_message=f"工作区对象数据校验失败：{exc}",
        ) from exc

    if idem_key:
        existing_op = session.execute(
            select(WorkspaceArtifactOp).where(
                WorkspaceArtifactOp.organization_id == actor.organization_id,
                WorkspaceArtifactOp.store_id == actor.store_id,
                WorkspaceArtifactOp.actor_subject_id == actor.subject_id,
                WorkspaceArtifactOp.op_type == "create",
                WorkspaceArtifactOp.idempotency_key == idem_key,
            )
        ).scalar_one_or_none()
        if existing_op is not None:
            existing_art = _load_owned_artifact(session, actor, existing_op.artifact_id)
            expected_fp = _payload_digest(
                {
                    "thread_id": req.thread_id,
                    "kind": req.kind,
                    "title": req.title,
                    "payload": normalized_payload,
                }
            )
            recorded_fp = (existing_op.before_values or {}).get("_create_fingerprint")
            if recorded_fp and recorded_fp != expected_fp:
                raise WorkspaceError(
                    status_code=409,
                    code="IDEMPOTENCY_CONFLICT",
                    error="idempotency_key_payload_mismatch",
                    user_message="幂等键已被不同创建请求占用",
                )
            return ArtifactMutationData(
                artifact=_to_artifact_record(existing_art),
                op=_to_op_record(existing_op),
                message="已返回幂等创建结果",
                idempotent_replay=True,
            )

    now = _utcnow()
    artifact_id = _new_id("art")
    op_id = _new_id("op")
    create_fp = _payload_digest(
        {
            "thread_id": req.thread_id,
            "kind": req.kind,
            "title": req.title,
            "payload": normalized_payload,
        }
    )

    artifact = WorkspaceArtifact(
        id=artifact_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        owner_subject_id=actor.subject_id,
        thread_id=req.thread_id,
        kind=req.kind,
        title=req.title,
        status="draft",
        version=1,
        payload=copy.deepcopy(normalized_payload),
        source_refs=copy.deepcopy(req.source_refs),
        locked_fields=[],
        pending_suggestions=[],
        created_at=now,
        updated_at=now,
    )
    op = WorkspaceArtifactOp(
        id=op_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        artifact_id=artifact_id,
        version_before=0,
        version_after=1,
        actor_type=req.actor_type,
        actor_subject_id=actor.subject_id,
        op_type="create",
        patch=copy.deepcopy(normalized_payload),
        before_values={"_create_fingerprint": create_fp},
        applied_fields=list(normalized_payload.keys()),
        skipped_fields=[],
        idempotency_key=idem_key,
        created_at=now,
    )
    session.add(artifact)
    session.flush()
    session.add(op)
    _touch_binding_editor_hint(
        session,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        thread_id=req.thread_id,
        subject_id=actor.subject_id,
        device="agent" if req.actor_type == "agent" else "web",
        now=now,
    )
    _emit_thread_event(
        session,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        thread_id=req.thread_id,
        subject_id=actor.subject_id,
        artifact_id=artifact_id,
        event_type="artifact_created",
        payload={"artifact_id": artifact_id, "kind": req.kind, "version": 1, "title": req.title},
        now=now,
    )
    return ArtifactMutationData(
        artifact=_to_artifact_record(artifact),
        op=_to_op_record(op),
        message="工作区对象已创建",
    )


def list_artifacts(
    session: Session,
    actor: AuthenticatedSubject,
    *,
    thread_id: str | None = None,
) -> list[ArtifactRecord]:
    _authorize_store_member(session, actor)
    if thread_id is not None:
        _check_thread_ownership(session, actor, thread_id)
    stmt = select(WorkspaceArtifact).where(
        WorkspaceArtifact.organization_id == actor.organization_id,
        WorkspaceArtifact.store_id == actor.store_id,
        WorkspaceArtifact.owner_subject_id == actor.subject_id,
    )
    if thread_id is not None:
        stmt = stmt.where(WorkspaceArtifact.thread_id == thread_id)
    stmt = stmt.order_by(WorkspaceArtifact.updated_at.desc())
    rows = session.execute(stmt).scalars().all()
    caps, _ = resolve_effective_capabilities(session, actor)
    cap_set = set(caps)
    visible = [
        _to_artifact_record(row)
        for row in rows
        if any(p in cap_set for p in _KIND_PERMISSION_MAP[row.kind]["read"])
    ]
    return visible


def get_artifact_detail(
    session: Session,
    actor: AuthenticatedSubject,
    artifact_id: str,
) -> ArtifactDetailData:
    artifact = _load_owned_artifact(session, actor, artifact_id)
    require_kind_permission(session, actor, artifact.kind, "read")
    ops = session.execute(
        select(WorkspaceArtifactOp)
        .where(
            WorkspaceArtifactOp.organization_id == actor.organization_id,
            WorkspaceArtifactOp.store_id == actor.store_id,
            WorkspaceArtifactOp.artifact_id == artifact.id,
        )
        .order_by(WorkspaceArtifactOp.version_after.asc(), WorkspaceArtifactOp.created_at.asc())
    ).scalars().all()
    return ArtifactDetailData(
        artifact=_to_artifact_record(artifact),
        ops=[_to_op_record(op) for op in ops],
    )


def patch_artifact(
    session: Session,
    actor: AuthenticatedSubject,
    artifact_id: str,
    req: ArtifactPatchRequest,
    *,
    header_idempotency_key: str | None = None,
) -> ArtifactMutationData:
    artifact = _load_owned_artifact(session, actor, artifact_id)
    require_kind_permission(session, actor, artifact.kind, "write")
    idem_key = req.idempotency_key or header_idempotency_key

    if idem_key:
        existing_op = _find_idempotent_op(
            session,
            organization_id=actor.organization_id,
            store_id=actor.store_id,
            artifact_id=artifact.id,
            op_type="patch",
            idempotency_key=idem_key,
        )
        if existing_op is not None:
            expected_fp = _payload_digest(
                {"patch": req.patch, "title": req.title, "actor_type": req.actor_type}
            )
            recorded_fp = (existing_op.before_values or {}).get("_request_fingerprint")
            if recorded_fp and recorded_fp != expected_fp:
                raise WorkspaceError(
                    status_code=409,
                    code="IDEMPOTENCY_CONFLICT",
                    error="idempotency_key_patch_mismatch",
                    user_message="同一幂等键不能用于不同的修改内容",
                )
            return ArtifactMutationData(
                artifact=_to_artifact_record(artifact),
                op=_to_op_record(existing_op),
                message="已返回幂等修改结果",
                skipped_fields=list(existing_op.skipped_fields or []),
                idempotent_replay=True,
            )

    if artifact.status != "draft":
        raise WorkspaceError(
            status_code=409,
            code="ARTIFACT_NOT_DRAFT",
            error=f"artifact_status_{artifact.status}",
            user_message="已确认保存或已归档的工作区草稿不可修改",
        )
    if req.base_version != artifact.version:
        raise WorkspaceError(
            status_code=409,
            code="VERSION_CONFLICT",
            error="version_conflict",
            user_message=f"版本冲突：当前版本为 v{artifact.version}，请求基线为 v{req.base_version}",
            hint="请刷新最新草稿版本后再合并或提交修改",
            data={"current_version": artifact.version, "base_version": req.base_version},
        )
    if not req.patch and req.title is None:
        raise WorkspaceError(
            status_code=422,
            code="EMPTY_PATCH",
            error="empty_patch",
            user_message="修改内容不能为空",
        )

    now = _utcnow()
    op_id = _new_id("op")
    version_before = artifact.version
    new_payload = copy.deepcopy(artifact.payload or {})
    new_locked = list(artifact.locked_fields or [])
    locked_set = set(new_locked)
    new_suggestions = list(artifact.pending_suggestions or [])
    added_suggestions: list[PendingSuggestionRecord] = []
    before_values: dict[str, Any] = {
        "_request_fingerprint": _payload_digest(
            {"patch": req.patch, "title": req.title, "actor_type": req.actor_type}
        )
    }
    applied_fields: list[str] = []
    skipped_fields: list[str] = []

    candidate_patch: dict[str, Any] = {}
    if req.actor_type == "human":
        candidate_patch = dict(req.patch)
    else:
        for key, value in req.patch.items():
            if key in locked_set:
                skipped_fields.append(key)
                suggestion = PendingSuggestionRecord(
                    op_id=op_id,
                    field=key,
                    suggested_value=copy.deepcopy(value),
                    current_value=copy.deepcopy(new_payload.get(key)),
                    reason=req.reason or "AI 建议修改人工锁定字段",
                    actor_subject_id=actor.subject_id,
                    created_at=now,
                )
                added_suggestions.append(suggestion)
                new_suggestions.append(suggestion.model_dump(mode="json"))
            else:
                candidate_patch[key] = value

    try:
        validated_payload = validate_artifact_payload(
            artifact.kind,
            {**new_payload, **candidate_patch},
            apply_defaults=False,
        )
    except ValueError as exc:
        raise WorkspaceError(
            status_code=422,
            code="INVALID_PAYLOAD",
            error=str(exc),
            user_message=f"工作区对象数据校验失败：{exc}",
        ) from exc

    for key in candidate_patch:
        before_values[key] = {
            "present": key in artifact.payload,
            "value": copy.deepcopy(artifact.payload.get(key)),
        }
        applied_fields.append(key)
        if req.actor_type == "human" and key not in locked_set:
            new_locked.append(key)
            locked_set.add(key)

    new_title = artifact.title
    if req.title is not None:
        if req.actor_type == "agent" and "title" in locked_set:
            skipped_fields.append("title")
            suggestion = PendingSuggestionRecord(
                op_id=op_id,
                field="title",
                suggested_value=req.title,
                current_value=artifact.title,
                reason=req.reason or "AI 建议修改人工锁定标题",
                actor_subject_id=actor.subject_id,
                created_at=now,
            )
            added_suggestions.append(suggestion)
            new_suggestions.append(suggestion.model_dump(mode="json"))
        else:
            before_values["title"] = {"present": True, "value": artifact.title}
            new_title = req.title
            applied_fields.append("title")
            if req.actor_type == "human" and "title" not in locked_set:
                new_locked.append("title")
                locked_set.add("title")

    artifact.payload = validated_payload
    artifact.title = new_title
    artifact.locked_fields = new_locked
    artifact.pending_suggestions = new_suggestions
    artifact.version = version_before + 1
    artifact.updated_at = now

    recorded_patch = dict(req.patch)
    if req.title is not None:
        recorded_patch["title"] = req.title

    op = WorkspaceArtifactOp(
        id=op_id,
        organization_id=artifact.organization_id,
        store_id=artifact.store_id,
        artifact_id=artifact.id,
        version_before=version_before,
        version_after=artifact.version,
        actor_type=req.actor_type,
        actor_subject_id=actor.subject_id,
        op_type="patch",
        patch=recorded_patch,
        before_values=before_values,
        applied_fields=applied_fields,
        skipped_fields=skipped_fields,
        idempotency_key=idem_key,
        created_at=now,
    )
    session.add(op)
    _touch_binding_editor_hint(
        session,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        thread_id=artifact.thread_id,
        subject_id=actor.subject_id,
        device="agent" if req.actor_type == "agent" else "web",
        now=now,
    )
    _emit_thread_event(
        session,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        thread_id=artifact.thread_id,
        subject_id=actor.subject_id,
        artifact_id=artifact.id,
        event_type="artifact_patched",
        payload={
            "artifact_id": artifact.id,
            "op_id": op_id,
            "actor_type": req.actor_type,
            "version": artifact.version,
            "applied_fields": applied_fields,
            "skipped_fields": skipped_fields,
            "pending_suggestions_count": len(new_suggestions),
        },
        now=now,
    )
    msg = (
        f"已应用 {len(applied_fields)} 个字段，{len(skipped_fields)} 个锁定字段转为待确认建议"
        if skipped_fields
        else f"已更新 {len(applied_fields)} 个字段"
    )
    return ArtifactMutationData(
        artifact=_to_artifact_record(artifact),
        op=_to_op_record(op),
        message=msg,
        skipped_fields=skipped_fields,
        pending_suggestions_added=added_suggestions,
    )


def lock_fields(
    session: Session,
    actor: AuthenticatedSubject,
    artifact_id: str,
    req: ArtifactLockRequest,
) -> ArtifactMutationData:
    artifact = _load_owned_artifact(session, actor, artifact_id)
    require_kind_permission(session, actor, artifact.kind, "write")
    if artifact.status != "draft":
        raise WorkspaceError(
            status_code=409,
            code="ARTIFACT_NOT_DRAFT",
            error=f"artifact_status_{artifact.status}",
            user_message="已确认保存或已归档的工作区草稿不可调整字段锁",
        )
    if req.base_version != artifact.version:
        raise WorkspaceError(
            status_code=409,
            code="VERSION_CONFLICT",
            error="version_conflict",
            user_message=f"版本冲突：当前版本为 v{artifact.version}",
            data={"current_version": artifact.version, "base_version": req.base_version},
        )
    now = _utcnow()
    version_before = artifact.version
    locked = list(artifact.locked_fields or [])
    newly_locked = [f for f in req.fields if f not in locked]
    locked.extend(newly_locked)
    artifact.locked_fields = locked
    artifact.version = version_before + 1
    artifact.updated_at = now
    op = WorkspaceArtifactOp(
        id=_new_id("op"),
        organization_id=artifact.organization_id,
        store_id=artifact.store_id,
        artifact_id=artifact.id,
        version_before=version_before,
        version_after=artifact.version,
        actor_type="human",
        actor_subject_id=actor.subject_id,
        op_type="lock",
        patch={"fields": list(req.fields)},
        before_values={},
        applied_fields=list(req.fields),
        skipped_fields=[],
        created_at=now,
    )
    session.add(op)
    _emit_thread_event(
        session,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        thread_id=artifact.thread_id,
        subject_id=actor.subject_id,
        artifact_id=artifact.id,
        event_type="artifact_locked",
        payload={"artifact_id": artifact.id, "fields": req.fields, "version": artifact.version},
        now=now,
    )
    return ArtifactMutationData(
        artifact=_to_artifact_record(artifact),
        op=_to_op_record(op),
        message="已锁定指定字段",
    )


def unlock_fields(
    session: Session,
    actor: AuthenticatedSubject,
    artifact_id: str,
    req: ArtifactUnlockRequest,
) -> ArtifactMutationData:
    artifact = _load_owned_artifact(session, actor, artifact_id)
    require_kind_permission(session, actor, artifact.kind, "write")
    if artifact.status != "draft":
        raise WorkspaceError(
            status_code=409,
            code="ARTIFACT_NOT_DRAFT",
            error=f"artifact_status_{artifact.status}",
            user_message="已确认保存或已归档的工作区草稿不可解锁字段",
        )
    if req.base_version != artifact.version:
        raise WorkspaceError(
            status_code=409,
            code="VERSION_CONFLICT",
            error="version_conflict",
            user_message=f"版本冲突：当前版本为 v{artifact.version}",
            data={"current_version": artifact.version, "base_version": req.base_version},
        )
    now = _utcnow()
    targets = set(req.fields or [])
    version_before = artifact.version
    artifact.locked_fields = [f for f in (artifact.locked_fields or []) if f not in targets]
    artifact.version = version_before + 1
    artifact.updated_at = now
    op = WorkspaceArtifactOp(
        id=_new_id("op"),
        organization_id=artifact.organization_id,
        store_id=artifact.store_id,
        artifact_id=artifact.id,
        version_before=version_before,
        version_after=artifact.version,
        actor_type="human",
        actor_subject_id=actor.subject_id,
        op_type="unlock",
        patch={"fields": list(req.fields or [])},
        before_values={},
        applied_fields=list(req.fields or []),
        skipped_fields=[],
        created_at=now,
    )
    session.add(op)
    _emit_thread_event(
        session,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        thread_id=artifact.thread_id,
        subject_id=actor.subject_id,
        artifact_id=artifact.id,
        event_type="artifact_unlocked",
        payload={"artifact_id": artifact.id, "fields": list(req.fields or []), "version": artifact.version},
        now=now,
    )
    return ArtifactMutationData(
        artifact=_to_artifact_record(artifact),
        op=_to_op_record(op),
        message="已解除字段锁定",
    )


def accept_suggestion(
    session: Session,
    actor: AuthenticatedSubject,
    artifact_id: str,
    op_id: str,
    req: ArtifactSuggestionDecisionRequest,
) -> ArtifactMutationData:
    artifact = _load_owned_artifact(session, actor, artifact_id)
    require_kind_permission(session, actor, artifact.kind, "write")
    if artifact.status != "draft":
        raise WorkspaceError(
            status_code=409,
            code="ARTIFACT_NOT_DRAFT",
            error=f"artifact_status_{artifact.status}",
            user_message="已确认保存或已归档的工作区草稿不可采纳建议",
        )
    if req.base_version != artifact.version:
        raise WorkspaceError(
            status_code=409,
            code="VERSION_CONFLICT",
            error="version_conflict",
            user_message=f"版本冲突：当前版本为 v{artifact.version}",
            data={"current_version": artifact.version, "base_version": req.base_version},
        )
    suggestions = list(artifact.pending_suggestions or [])
    matched = [
        item
        for item in suggestions
        if item.get("op_id") == op_id and (req.field is None or item.get("field") == req.field)
    ]
    if not matched:
        raise WorkspaceError(
            status_code=404,
            code="SUGGESTION_NOT_FOUND",
            error=f"suggestion_not_found:{op_id}",
            user_message="未找到对应的待确认 AI 修改建议",
        )

    now = _utcnow()
    version_before = artifact.version
    new_payload = copy.deepcopy(artifact.payload or {})
    new_title = artifact.title
    new_locked = list(artifact.locked_fields or [])
    before_values: dict[str, Any] = {}
    applied_fields: list[str] = []
    patch_written: dict[str, Any] = {}

    for item in matched:
        field = str(item["field"])
        val = copy.deepcopy(item.get("suggested_value"))
        if field == "title":
            before_values["title"] = {"present": True, "value": new_title}
            new_title = str(val)
        else:
            before_values[field] = {
                "present": field in new_payload,
                "value": copy.deepcopy(new_payload.get(field)),
            }
            new_payload[field] = val
        patch_written[field] = val
        applied_fields.append(field)
        if field not in new_locked:
            new_locked.append(field)

    try:
        validated_payload = validate_artifact_payload(artifact.kind, new_payload, apply_defaults=False)
    except ValueError as exc:
        raise WorkspaceError(
            status_code=422,
            code="INVALID_PAYLOAD",
            error=str(exc),
            user_message=f"建议值不符合字段规范：{exc}",
        ) from exc

    matched_keys = {(item.get("op_id"), item.get("field")) for item in matched}
    remaining = [
        item for item in suggestions if (item.get("op_id"), item.get("field")) not in matched_keys
    ]

    artifact.payload = validated_payload
    artifact.title = new_title
    artifact.locked_fields = new_locked
    artifact.pending_suggestions = remaining
    artifact.version = version_before + 1
    artifact.updated_at = now

    op = WorkspaceArtifactOp(
        id=_new_id("op"),
        organization_id=artifact.organization_id,
        store_id=artifact.store_id,
        artifact_id=artifact.id,
        version_before=version_before,
        version_after=artifact.version,
        actor_type="human",
        actor_subject_id=actor.subject_id,
        op_type="accept_suggestion",
        patch=patch_written,
        before_values=before_values,
        applied_fields=applied_fields,
        skipped_fields=[],
        undo_of_op_id=op_id,
        created_at=now,
    )
    session.add(op)
    _emit_thread_event(
        session,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        thread_id=artifact.thread_id,
        subject_id=actor.subject_id,
        artifact_id=artifact.id,
        event_type="artifact_suggestion_accepted",
        payload={
            "artifact_id": artifact.id,
            "suggestion_op_id": op_id,
            "fields": applied_fields,
            "version": artifact.version,
        },
        now=now,
    )
    return ArtifactMutationData(
        artifact=_to_artifact_record(artifact),
        op=_to_op_record(op),
        message="已采纳建议并保持字段人工锁定",
    )


def ignore_suggestion(
    session: Session,
    actor: AuthenticatedSubject,
    artifact_id: str,
    op_id: str,
    req: ArtifactSuggestionDecisionRequest,
) -> ArtifactMutationData:
    artifact = _load_owned_artifact(session, actor, artifact_id)
    require_kind_permission(session, actor, artifact.kind, "write")
    if artifact.status != "draft":
        raise WorkspaceError(
            status_code=409,
            code="ARTIFACT_NOT_DRAFT",
            error=f"artifact_status_{artifact.status}",
            user_message="已确认保存或已归档的工作区草稿不可忽略建议",
        )
    if req.base_version != artifact.version:
        raise WorkspaceError(
            status_code=409,
            code="VERSION_CONFLICT",
            error="version_conflict",
            user_message=f"版本冲突：当前版本为 v{artifact.version}",
            data={"current_version": artifact.version, "base_version": req.base_version},
        )
    suggestions = list(artifact.pending_suggestions or [])
    matched = [
        item
        for item in suggestions
        if item.get("op_id") == op_id and (req.field is None or item.get("field") == req.field)
    ]
    if not matched:
        raise WorkspaceError(
            status_code=404,
            code="SUGGESTION_NOT_FOUND",
            error=f"suggestion_not_found:{op_id}",
            user_message="未找到对应的待确认 AI 修改建议",
        )

    now = _utcnow()
    version_before = artifact.version
    matched_keys = {(item.get("op_id"), item.get("field")) for item in matched}
    remaining = [
        item for item in suggestions if (item.get("op_id"), item.get("field")) not in matched_keys
    ]
    ignored_fields = [str(item["field"]) for item in matched]

    artifact.pending_suggestions = remaining
    artifact.version = version_before + 1
    artifact.updated_at = now

    op = WorkspaceArtifactOp(
        id=_new_id("op"),
        organization_id=artifact.organization_id,
        store_id=artifact.store_id,
        artifact_id=artifact.id,
        version_before=version_before,
        version_after=artifact.version,
        actor_type="human",
        actor_subject_id=actor.subject_id,
        op_type="ignore_suggestion",
        patch={"ignored_fields": ignored_fields},
        before_values={},
        applied_fields=[],
        skipped_fields=ignored_fields,
        undo_of_op_id=op_id,
        created_at=now,
    )
    session.add(op)
    _emit_thread_event(
        session,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        thread_id=artifact.thread_id,
        subject_id=actor.subject_id,
        artifact_id=artifact.id,
        event_type="artifact_suggestion_ignored",
        payload={
            "artifact_id": artifact.id,
            "suggestion_op_id": op_id,
            "fields": ignored_fields,
            "version": artifact.version,
        },
        now=now,
    )
    return ArtifactMutationData(
        artifact=_to_artifact_record(artifact),
        op=_to_op_record(op),
        message="已忽略 AI 建议并保留原人工锁定值",
    )


def undo_op(
    session: Session,
    actor: AuthenticatedSubject,
    artifact_id: str,
    req: ArtifactUndoRequest,
    *,
    header_idempotency_key: str | None = None,
) -> ArtifactMutationData:
    artifact = _load_owned_artifact(session, actor, artifact_id)
    require_kind_permission(session, actor, artifact.kind, "write")
    idem_key = req.idempotency_key or header_idempotency_key

    if idem_key:
        existing_undo = _find_idempotent_op(
            session,
            organization_id=actor.organization_id,
            store_id=actor.store_id,
            artifact_id=artifact.id,
            op_type="undo",
            idempotency_key=idem_key,
        )
        if existing_undo is not None:
            if existing_undo.undo_of_op_id != req.target_op_id:
                raise WorkspaceError(
                    status_code=409,
                    code="IDEMPOTENCY_CONFLICT",
                    error="idempotency_key_undo_mismatch",
                    user_message="同一幂等键不能撤销不同的操作",
                )
            skipped = list(existing_undo.skipped_fields or [])
            msg = f"{len(skipped)} 个字段已被人工修改，未撤销" if skipped else "已返回幂等撤销结果"
            return ArtifactMutationData(
                artifact=_to_artifact_record(artifact),
                op=_to_op_record(existing_undo),
                message=msg,
                skipped_fields=skipped,
                idempotent_replay=True,
            )

    if artifact.status != "draft":
        raise WorkspaceError(
            status_code=409,
            code="ARTIFACT_NOT_DRAFT",
            error=f"artifact_status_{artifact.status}",
            user_message="已确认保存或已归档的工作区草稿不可撤销",
        )
    if req.base_version != artifact.version:
        raise WorkspaceError(
            status_code=409,
            code="VERSION_CONFLICT",
            error="version_conflict",
            user_message=f"版本冲突：当前版本为 v{artifact.version}",
            data={"current_version": artifact.version, "base_version": req.base_version},
        )

    target_op = session.execute(
        select(WorkspaceArtifactOp).where(
            WorkspaceArtifactOp.organization_id == actor.organization_id,
            WorkspaceArtifactOp.store_id == actor.store_id,
            WorkspaceArtifactOp.artifact_id == artifact.id,
            WorkspaceArtifactOp.id == req.target_op_id,
        )
    ).scalar_one_or_none()
    if target_op is None:
        raise WorkspaceError(
            status_code=404,
            code="OP_NOT_FOUND",
            error=f"op_not_found:{req.target_op_id}",
            user_message="未找到要撤销的操作记录",
        )
    if target_op.op_type != "patch":
        raise WorkspaceError(
            status_code=409,
            code="OP_NOT_UNDOABLE",
            error=f"op_not_undoable:{target_op.op_type}",
            user_message="仅支持撤销补丁修改操作",
        )

    prior_undo = session.execute(
        select(WorkspaceArtifactOp).where(
            WorkspaceArtifactOp.organization_id == actor.organization_id,
            WorkspaceArtifactOp.store_id == actor.store_id,
            WorkspaceArtifactOp.artifact_id == artifact.id,
            WorkspaceArtifactOp.op_type == "undo",
            WorkspaceArtifactOp.undo_of_op_id == req.target_op_id,
        )
    ).scalar_one_or_none()
    if prior_undo is not None:
        raise WorkspaceError(
            status_code=409,
            code="ALREADY_UNDONE",
            error=f"already_undone:{req.target_op_id}",
            user_message="该操作已被撤销，不能重复撤销",
        )

    subsequent_ops = session.execute(
        select(WorkspaceArtifactOp)
        .where(
            WorkspaceArtifactOp.organization_id == actor.organization_id,
            WorkspaceArtifactOp.store_id == actor.store_id,
            WorkspaceArtifactOp.artifact_id == artifact.id,
            WorkspaceArtifactOp.version_after > target_op.version_after,
        )
        .order_by(WorkspaceArtifactOp.version_after.asc())
    ).scalars().all()

    human_touched: set[str] = set()
    for later_op in subsequent_ops:
        if later_op.actor_type == "human" and later_op.op_type in ("patch", "accept_suggestion"):
            human_touched.update(later_op.applied_fields or [])

    now = _utcnow()
    version_before = artifact.version
    new_payload = copy.deepcopy(artifact.payload or {})
    new_title = artifact.title
    before_map = target_op.before_values or {}
    reverted_fields: list[str] = []
    skipped_fields: list[str] = []
    reverted_patch: dict[str, Any] = {}
    undo_before_values: dict[str, Any] = {}

    for field in target_op.applied_fields or []:
        if field in human_touched:
            skipped_fields.append(field)
            continue
        prior = before_map.get(field)
        if field == "title":
            undo_before_values["title"] = {"present": True, "value": new_title}
            if prior and prior.get("present"):
                new_title = str(prior.get("value"))
                reverted_patch["title"] = new_title
        else:
            undo_before_values[field] = {
                "present": field in new_payload,
                "value": copy.deepcopy(new_payload.get(field)),
            }
            if prior and prior.get("present"):
                new_payload[field] = copy.deepcopy(prior.get("value"))
                reverted_patch[field] = copy.deepcopy(prior.get("value"))
            else:
                new_payload.pop(field, None)
                reverted_patch[field] = None
        reverted_fields.append(field)

    validated_payload = validate_artifact_payload(artifact.kind, new_payload, apply_defaults=False)
    remaining_suggestions = [
        item for item in (artifact.pending_suggestions or []) if item.get("op_id") != req.target_op_id
    ]

    artifact.payload = validated_payload
    artifact.title = new_title
    artifact.pending_suggestions = remaining_suggestions
    artifact.version = version_before + 1
    artifact.updated_at = now

    op = WorkspaceArtifactOp(
        id=_new_id("op"),
        organization_id=artifact.organization_id,
        store_id=artifact.store_id,
        artifact_id=artifact.id,
        version_before=version_before,
        version_after=artifact.version,
        actor_type="human",
        actor_subject_id=actor.subject_id,
        op_type="undo",
        patch=reverted_patch,
        before_values=undo_before_values,
        applied_fields=reverted_fields,
        skipped_fields=skipped_fields,
        undo_of_op_id=req.target_op_id,
        idempotency_key=idem_key,
        created_at=now,
    )
    session.add(op)
    _emit_thread_event(
        session,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        thread_id=artifact.thread_id,
        subject_id=actor.subject_id,
        artifact_id=artifact.id,
        event_type="artifact_undone",
        payload={
            "artifact_id": artifact.id,
            "target_op_id": req.target_op_id,
            "reverted_fields": reverted_fields,
            "skipped_fields": skipped_fields,
            "version": artifact.version,
        },
        now=now,
    )

    msg = (
        f"{len(skipped_fields)} 个字段已被人工修改，未撤销"
        if skipped_fields
        else f"已撤销 {len(reverted_fields)} 个字段"
    )
    return ArtifactMutationData(
        artifact=_to_artifact_record(artifact),
        op=_to_op_record(op),
        message=msg,
        skipped_fields=skipped_fields,
    )


def submit_artifact(
    session: Session,
    actor: AuthenticatedSubject,
    artifact_id: str,
    req: ArtifactSubmitRequest,
    *,
    header_idempotency_key: str | None = None,
) -> ArtifactMutationData:
    """Seal a workspace draft and audit it; never submit a formal business document.

    The legacy submitted status and rcpt reference are local workspace metadata,
    not evidence of inventory, financial or ERP execution.
    """
    artifact = _load_owned_artifact(session, actor, artifact_id)
    require_kind_permission(session, actor, artifact.kind, "submit")
    idem_key = req.idempotency_key or header_idempotency_key
    if not idem_key:
        raise WorkspaceError(
            status_code=400,
            code="MISSING_IDEMPOTENCY_KEY",
            error="missing_idempotency_key",
            user_message="提交工作区对象必须提供 Idempotency-Key",
            hint="请在请求头 Idempotency-Key 或请求体 idempotency_key 中提供唯一幂等键",
        )

    if artifact.status == "submitted":
        existing_op = _find_idempotent_op(
            session,
            organization_id=actor.organization_id,
            store_id=actor.store_id,
            artifact_id=artifact.id,
            op_type="submit",
            idempotency_key=idem_key,
        )
        if existing_op is not None and artifact.submit_idempotency_key == idem_key:
            return ArtifactMutationData(
                artifact=_to_artifact_record(artifact),
                op=_to_op_record(existing_op),
                message=f"已幂等返回草稿保存凭据：{artifact.submitted_receipt_ref}；正式单据未提交",
                idempotent_replay=True,
            )
        raise WorkspaceError(
            status_code=409,
            code="ARTIFACT_ALREADY_SUBMITTED",
            error="artifact_already_submitted",
            user_message="该工作区草稿已确认保存，不能用不同幂等键重复保存；正式单据未提交",
        )

    if artifact.status != "draft":
        raise WorkspaceError(
            status_code=409,
            code="ARTIFACT_NOT_DRAFT",
            error=f"artifact_status_{artifact.status}",
            user_message="仅草稿状态对象允许提交",
        )
    if req.base_version != artifact.version:
        raise WorkspaceError(
            status_code=409,
            code="VERSION_CONFLICT",
            error="version_conflict",
            user_message=f"版本冲突：当前版本为 v{artifact.version}",
            data={"current_version": artifact.version, "base_version": req.base_version},
        )

    try:
        validated_payload = validate_artifact_payload(
            artifact.kind,
            copy.deepcopy(artifact.payload or {}),
            apply_defaults=False,
        )
    except ValueError as exc:
        raise WorkspaceError(
            status_code=422,
            code="INVALID_PAYLOAD",
            error=str(exc),
            user_message=f"提交前数据校验失败：{exc}",
        ) from exc

    now = _utcnow()
    digest = hashlib.sha256(
        f"{artifact.organization_id}:{artifact.store_id}:{artifact.id}:{idem_key}".encode("utf-8")
    ).hexdigest()[:16]
    receipt_ref = f"rcpt-{artifact.kind.split('_')[0]}-{digest}"

    audit_service.record_event(
        session,
        actor_id=actor.subject_id,
        organization_id=artifact.organization_id,
        store_id=artifact.store_id,
        action="workspace.artifact.submit",
        target_type=artifact.kind,
        target_id=artifact.id,
        idempotency_key=f"wsp-submit:{artifact.id}:{idem_key}"[:200],
        details={
            "receipt_ref": receipt_ref,
            "version": artifact.version,
            "note": req.note,
            "payload": validated_payload,
        },
        occurred_at=now,
    )

    version_before = artifact.version
    artifact.payload = validated_payload
    artifact.status = "submitted"
    artifact.submitted_receipt_ref = receipt_ref
    artifact.submit_idempotency_key = idem_key
    artifact.version = version_before + 1
    artifact.updated_at = now

    op = WorkspaceArtifactOp(
        id=_new_id("op"),
        organization_id=artifact.organization_id,
        store_id=artifact.store_id,
        artifact_id=artifact.id,
        version_before=version_before,
        version_after=artifact.version,
        actor_type="human",
        actor_subject_id=actor.subject_id,
        op_type="submit",
        patch={"submitted_receipt_ref": receipt_ref, "note": req.note},
        before_values={"status": {"present": True, "value": "draft"}},
        applied_fields=["status", "submitted_receipt_ref"],
        skipped_fields=[],
        idempotency_key=idem_key,
        created_at=now,
    )
    session.add(op)
    _emit_thread_event(
        session,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        thread_id=artifact.thread_id,
        subject_id=actor.subject_id,
        artifact_id=artifact.id,
        event_type="artifact_submitted",
        payload={
            "artifact_id": artifact.id,
            "receipt_ref": receipt_ref,
            "version": artifact.version,
        },
        now=now,
    )
    return ArtifactMutationData(
        artifact=_to_artifact_record(artifact),
        op=_to_op_record(op),
        message=f"已保存核对结果，正式单据未提交。草稿保存凭据：{receipt_ref}",
    )


def _format_recent_editor_hint(binding: WorkspaceThreadBinding) -> str | None:
    if not binding.last_editor_subject_id or not binding.last_editor_device:
        return None
    return f"最近由 {binding.last_editor_subject_id} 在 {binding.last_editor_device} 端编辑"


def _to_thread_workspace_record(binding: WorkspaceThreadBinding) -> ThreadWorkspaceRecord:
    return ThreadWorkspaceRecord(
        thread_id=binding.thread_id,
        organization_id=binding.organization_id,
        store_id=binding.store_id,
        subject_id=binding.subject_id,
        open_objects=copy.deepcopy(binding.open_objects or []),
        active_object_ref=copy.deepcopy(binding.active_object_ref),
        last_editor_subject_id=binding.last_editor_subject_id,
        last_editor_device=binding.last_editor_device,
        last_edited_at=binding.last_edited_at,
        recent_editor_hint=_format_recent_editor_hint(binding),
        version=binding.version,
        updated_at=binding.updated_at,
    )


def get_thread_workspace(
    session: Session,
    actor: AuthenticatedSubject,
    thread_id: str,
) -> ThreadWorkspaceRecord:
    _authorize_store_member(session, actor)
    _check_thread_ownership(session, actor, thread_id)
    binding = session.execute(
        select(WorkspaceThreadBinding).where(
            WorkspaceThreadBinding.organization_id == actor.organization_id,
            WorkspaceThreadBinding.store_id == actor.store_id,
            WorkspaceThreadBinding.thread_id == thread_id,
        )
    ).scalar_one_or_none()
    if binding is None:
        return ThreadWorkspaceRecord(
            thread_id=thread_id,
            organization_id=actor.organization_id,
            store_id=actor.store_id,
            subject_id=actor.subject_id,
            open_objects=[],
            active_object_ref=None,
            version=0,
        )
    return _to_thread_workspace_record(binding)


def upsert_thread_workspace(
    session: Session,
    actor: AuthenticatedSubject,
    thread_id: str,
    req: ThreadWorkspaceUpsertRequest,
) -> ThreadWorkspaceRecord:
    _authorize_store_member(session, actor)
    _check_thread_ownership(session, actor, thread_id)
    now = _utcnow()

    binding = session.execute(
        select(WorkspaceThreadBinding).where(
            WorkspaceThreadBinding.organization_id == actor.organization_id,
            WorkspaceThreadBinding.store_id == actor.store_id,
            WorkspaceThreadBinding.thread_id == thread_id,
        )
    ).scalar_one_or_none()

    if binding is None:
        if req.base_version not in (None, 0):
            raise WorkspaceError(
                status_code=409,
                code="VERSION_CONFLICT",
                error="version_conflict",
                user_message="线程工作区尚未创建，期望基线版本为 0",
                data={"current_version": 0, "base_version": req.base_version},
            )
        binding = WorkspaceThreadBinding(
            id=_new_id("wtb"),
            thread_id=thread_id,
            organization_id=actor.organization_id,
            store_id=actor.store_id,
            subject_id=actor.subject_id,
            open_objects=copy.deepcopy(req.open_objects),
            active_object_ref=copy.deepcopy(req.active_object_ref),
            last_editor_subject_id=actor.subject_id,
            last_editor_device=req.device,
            last_edited_at=now,
            version=1,
            updated_at=now,
        )
        session.add(binding)
        session.flush()
    else:
        if req.base_version is not None and req.base_version != binding.version:
            raise WorkspaceError(
                status_code=409,
                code="VERSION_CONFLICT",
                error="version_conflict",
                user_message=f"线程工作区版本冲突：当前版本为 v{binding.version}",
                data={"current_version": binding.version, "base_version": req.base_version},
            )
        binding.open_objects = copy.deepcopy(req.open_objects)
        binding.active_object_ref = copy.deepcopy(req.active_object_ref)
        binding.last_editor_subject_id = actor.subject_id
        binding.last_editor_device = req.device
        binding.last_edited_at = now
        binding.version = binding.version + 1
        binding.updated_at = now

    _emit_thread_event(
        session,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        thread_id=thread_id,
        subject_id=actor.subject_id,
        artifact_id=None,
        event_type="thread_workspace_updated",
        payload={
            "thread_id": thread_id,
            "version": binding.version,
            "device": req.device,
            "open_count": len(binding.open_objects or []),
            "active_object_ref": binding.active_object_ref,
        },
        now=now,
    )
    return _to_thread_workspace_record(binding)


def list_thread_events(
    session: Session,
    actor: AuthenticatedSubject,
    thread_id: str,
    *,
    cursor: int = 0,
    limit: int = 100,
) -> ThreadEventsData:
    _authorize_store_member(session, actor)
    _check_thread_ownership(session, actor, thread_id)
    rows = session.execute(
        select(WorkspaceThreadEvent)
        .where(
            WorkspaceThreadEvent.organization_id == actor.organization_id,
            WorkspaceThreadEvent.store_id == actor.store_id,
            WorkspaceThreadEvent.thread_id == thread_id,
            WorkspaceThreadEvent.subject_id == actor.subject_id,
            WorkspaceThreadEvent.seq > cursor,
        )
        .order_by(WorkspaceThreadEvent.seq.asc())
        .limit(limit)
    ).scalars().all()
    events = [
        ThreadEventRecord(
            seq=row.seq,
            event_id=row.event_id,
            thread_id=row.thread_id,
            subject_id=row.subject_id,
            artifact_id=row.artifact_id,
            event_type=row.event_type,
            payload=copy.deepcopy(row.payload or {}),
            created_at=row.created_at,
        )
        for row in rows
    ]
    next_cursor = events[-1].seq if events else cursor
    return ThreadEventsData(
        thread_id=thread_id,
        cursor=cursor,
        next_cursor=next_cursor,
        events=events,
    )
