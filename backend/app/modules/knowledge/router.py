from typing import Annotated

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session

from backend.app.core.idempotency import require_idempotency_key
from backend.app.modules.knowledge.commands import execute_command
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.knowledge import service, access_policy
from backend.app.modules.knowledge.models import KnowledgeEntry, KnowledgeRevision
from backend.app.modules.knowledge.schemas import (
    KnowledgeCreate,
    KnowledgeDetailEnvelope,
    KnowledgeDetailOut,
    KnowledgeEnvelope,
    KnowledgeEntryOut,
    KnowledgeListEnvelope,
    KnowledgeReview,
    KnowledgeRevisionCreate,
    KnowledgeRevisionOut,
    KnowledgeWithdraw,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

_HUMAN_PREVIEW_SEAL = object()


class _HumanPreview:
    __slots__ = ("actor", "seal")

    def __init__(self, actor, seal):
        if seal is not _HUMAN_PREVIEW_SEAL:
            raise ValueError("human_preview_server_only")
        self.actor, self.seal = actor, seal


def _valid_human_preview(context, actor):
    return (type(context) is _HumanPreview and context.seal is _HUMAN_PREVIEW_SEAL
            and context.actor is actor)


def _employee_preview(session, actor):
    # Only fixed employee HTTP handlers call this AFTER their actual sx1 auth
    # dependency. Worker ctx.person can have the same valid fields, but ordinary
    # services/tools never mint or receive this sealed route capability.
    from backend.app.modules.auth.service import validate_actor_session
    from backend.app.modules.identity.models import Subject
    validate_actor_session(session, actor)
    person = session.get(Subject, actor.subject_id, populate_existing=True)
    if person is None or person.subject_type != "person":
        raise HTTPException(403, "knowledge_human_review_required")
    return _HumanPreview(actor, _HUMAN_PREVIEW_SEAL)



def _revision_out(revision: KnowledgeRevision) -> KnowledgeRevisionOut:
    return KnowledgeRevisionOut.model_validate(revision)


def _entry_out(entry: KnowledgeEntry,
               revision: KnowledgeRevision | None, *, session=None, actor=None) -> KnowledgeEntryOut:
    return KnowledgeEntryOut(
        id=entry.id, organization_id=entry.organization_id,
        store_id=entry.store_id, scope_type=entry.scope_type,
        scope_ref=(entry.scope_ref if revision is not None and session is not None and actor is not None
                   and access_policy.can_read_reference(session, actor, entry.scope_ref) else None),
        status=entry.status, version=entry.version,
        submitted_by=entry.submitted_by, submitted_at=entry.submitted_at,
        approved_by=entry.approved_by, approved_at=entry.approved_at,
        expires_at=entry.expires_at,
        revision=_revision_out(revision) if revision is not None else None,
    )


def _current(session: Session, entry: KnowledgeEntry) -> KnowledgeRevision | None:
    return (session.get(KnowledgeRevision, entry.current_revision_id)
            if entry.current_revision_id else None)


def _write(session: Session, actor: AuthenticatedSubject, *, operation: str,
           key: str, payload: dict, permission: str | None, execute,
           message: str) -> KnowledgeEnvelope:
    with session.begin():
        if permission is not None:
            service._require(session, actor, permission, service._now(None))
        else:
            # Withdrawal accepts either capability but always revalidates scope/session.
            now = service._now(None)
            if not (service._has_permission(session, actor, service.SUBMIT_PERMISSION, now)
                    or service._has_permission(session, actor, service.REVIEW_PERMISSION, now)):
                from fastapi import HTTPException
                raise HTTPException(403, "knowledge_permission_denied")

        def perform() -> dict:
            entry = execute()
            return KnowledgeEnvelope(success=True, data=_entry_out(entry, _current(session, entry), session=session, actor=actor),
                                     code="OK", error=None, user_message=message,
                                     hint=None).model_dump(mode="json")

        result = execute_command(session, actor, operation=operation, key=key,
                                 payload=payload, execute=perform)
        result = access_policy.project_response(session, actor, result)
        if operation == "review" and payload.get("decision") == "verify_source":
            result["hint"] = "仅核验已有来源证据；证据不足的资料仍受保护，内容状态未改变。"
    return KnowledgeEnvelope.model_validate(result)


@router.post("", response_model=KnowledgeEnvelope, status_code=201)
def submit_route(
    payload: KnowledgeCreate,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
) -> KnowledgeEnvelope:
    return _write(session, actor, operation="submit", key=key,
                  payload=payload.model_dump(mode="json"), permission=service.SUBMIT_PERMISSION,
                  execute=lambda: service.submit_knowledge(session, actor, **payload.model_dump()),
                  message="知识已提交，等待审核。")


@router.post("/{entry_id}/revisions", response_model=KnowledgeEnvelope)
def revise_route(
    entry_id: str,
    payload: KnowledgeRevisionCreate,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
) -> KnowledgeEnvelope:
    return _write(session, actor, operation="revise", key=key,
                  payload={"entry_id": entry_id, **payload.model_dump(mode="json")},
                  permission=service.SUBMIT_PERMISSION,
                  execute=lambda: service.revise_knowledge(session, actor, entry_id, **payload.model_dump()),
                  message="知识更正已提交，等待重新审核。")


@router.post("/{entry_id}/review", response_model=KnowledgeEnvelope)
def review_route(
    entry_id: str,
    payload: KnowledgeReview,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
) -> KnowledgeEnvelope:
    return _write(session, actor, operation="review", key=key,
                  payload={"entry_id": entry_id, **payload.model_dump(mode="json")},
                  permission=service.REVIEW_PERMISSION,
                  execute=lambda: service.review_knowledge(session, actor, entry_id, **payload.model_dump()),
                  message=("来源核验已完成，内容状态未改变，未批准。" if payload.decision == "verify_source"
                           else "知识已通过审核。" if payload.decision == "approve" else "知识已驳回。"))


@router.post("/{entry_id}/withdraw", response_model=KnowledgeEnvelope)
def withdraw_route(
    entry_id: str,
    payload: KnowledgeWithdraw,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str, Depends(require_idempotency_key)],
) -> KnowledgeEnvelope:
    return _write(session, actor, operation="withdraw", key=key,
                  payload={"entry_id": entry_id, **payload.model_dump(mode="json")}, permission=None,
                  execute=lambda: service.withdraw_knowledge(session, actor, entry_id, **payload.model_dump()),
                  message="知识已撤回。")


@router.get("", response_model=KnowledgeListEnvelope)
def list_route(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    limit: int = Query(default=50, ge=1, le=100),
) -> KnowledgeListEnvelope:
    pairs = service.usable_entries(session, actor, limit=limit)
    return KnowledgeListEnvelope(
        success=True,
        data={"items": [_entry_out(entry, revision, session=session, actor=actor).model_dump()
                        for entry, revision in pairs]},
        code="OK", error=None, user_message="可用知识已读取。", hint=None,
    )


@router.get("/review-queue", response_model=KnowledgeListEnvelope)
def review_queue_route(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    limit: int = Query(default=50, ge=1, le=100),
) -> KnowledgeListEnvelope:
    context = _employee_preview(session, actor)
    pairs = service.review_queue(session, actor, limit=limit, _human_preview=context)
    return KnowledgeListEnvelope(
        success=True,
        data={"items": [_entry_out(entry, revision, session=session, actor=actor).model_dump()
                        for entry, revision in pairs]},
        code="OK", error=None, user_message="知识复核队列已读取。", hint=None,
    )


@router.get("/{entry_id}", response_model=KnowledgeDetailEnvelope)
def detail_route(
    entry_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> KnowledgeDetailEnvelope:
    entry, revisions = service.entry_detail(session, actor, entry_id)
    detail = KnowledgeDetailOut(
        **_entry_out(entry, next((r for r in revisions if r.id == entry.current_revision_id), None),
                     session=session, actor=actor).model_dump(),
        history=[_revision_out(revision) for revision in revisions],
    )
    return KnowledgeDetailEnvelope(success=True, data=detail, code="OK", error=None,
                                   user_message="知识详情已读取。", hint=None)


@router.get("/{entry_id}/review-material", response_model=KnowledgeDetailEnvelope)
def review_material_route(
    entry_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> KnowledgeDetailEnvelope:
    context = _employee_preview(session, actor)
    now = service._now(None)
    service._require(session, actor, service.REVIEW_PERMISSION, now)
    entry = service._entry(session, actor, entry_id)
    revisions, _, _, _ = access_policy.inspect_entry(session, entry)
    visible = [r for r in revisions if access_policy.can_read(
        session, actor, entry, r, now=now, human_preview=context)]
    current = next((r for r in visible if r.id == entry.current_revision_id), None)
    detail = KnowledgeDetailOut(**_entry_out(entry, current, session=session, actor=actor).model_dump(),
                                history=[_revision_out(r) for r in sorted(visible, key=lambda r: r.version)])
    return KnowledgeDetailEnvelope(success=True, data=detail, code="OK", error=None,
        user_message="人工核验资料已读取。", hint="人工核验资料，未核来源不能自动用于回答")
