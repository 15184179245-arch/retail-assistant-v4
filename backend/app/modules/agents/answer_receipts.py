"""Trusted evidence capture and fail-closed answer/context reads.

Only the server's read-tool results can supply sources. The model cannot name
citations. Historical text is returned only while ALL recorded dependencies
remain accessible and unchanged; references are never silently upgraded.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import time

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents.models import AgentAnswer, AgentConversation, AgentMessage, AgentRun
from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.knowledge.models import KnowledgeEntry, KnowledgeRevision
from backend.app.modules.knowledge import access_policy
from backend.app.modules.memory import service as memory_service

# Bounded metadata capacity for large windows, leaving room for current input
# and new tool sources. This is not a grant to reuse invalid evidence.
MAX_EVIDENCE = 16384
MAX_CONTEXT_EVIDENCE = MAX_EVIDENCE - 128


class EvidenceError(ValueError):
    pass


def merge_evidence(*groups: list[dict], limit: int = MAX_EVIDENCE) -> list[dict]:
    unique: dict[str, dict] = {}
    for group in groups:
        for reference in group:
            unique[json.dumps(reference, sort_keys=True, separators=(",", ":"))] = reference
    if len(unique) > limit:
        raise EvidenceError("agent_evidence_limit")
    return list(unique.values())


def _scoped(session: Session, model, actor: AuthenticatedSubject, **values):
    conditions = [model.organization_id == actor.organization_id,
                  model.store_id == actor.store_id]
    conditions.extend(getattr(model, key) == value for key, value in values.items())
    return session.scalar(select(model).where(*conditions).execution_options(populate_existing=True))


def _time(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EvidenceError("agent_evidence_invalid")
    return value.astimezone(timezone.utc)


def _current_knowledge(actor, entry, revision, *, entry_version, revision_version):
    """Check again after fresh content policy; it may refresh the selected entry."""
    return (entry is not None and revision is not None
            and (entry.organization_id, entry.store_id) == (actor.organization_id, actor.store_id)
            and (revision.organization_id, revision.store_id) == (actor.organization_id, actor.store_id)
            and revision.entry_id == entry.id and entry.current_revision_id == revision.id
            and entry.version == entry_version and revision.version == revision_version
            and entry.status == 'approved'
            and (entry.expires_at is None or entry.expires_at > datetime.now(timezone.utc)))


def capture_tool_evidence(session: Session, actor: AuthenticatedSubject, *,
                          tool_key: str, permission: str, result: dict) -> list[dict]:
    """Called with the registry's validated return value, never model JSON."""
    evidence = [{"kind": "permission", "key": permission}]
    if tool_key == "knowledge.search":
        for citation in result["citations"]:
            entry = _scoped(session, KnowledgeEntry, actor, id=citation["entry_id"])
            revision = _scoped(session, KnowledgeRevision, actor, id=citation["revision_id"])
            observed_version = entry.version if entry is not None else None
            if (not _current_knowledge(actor, entry, revision, entry_version=observed_version,
                                       revision_version=citation["version"])
                    or not _allowed(session, actor, "knowledge.read")
                    or not access_policy.can_read(session, actor, entry, revision)
                    or not _current_knowledge(actor, entry, revision, entry_version=observed_version,
                                              revision_version=citation["version"])):
                raise EvidenceError("agent_evidence_changed")
            evidence.append({"kind": "knowledge", "id": entry.id,
                             "revision_id": revision.id, "version": entry.version,
                             "revision_version": revision.version})
    elif tool_key == "memory.search":
        for item in result["items"]:
            current = _current_memory(session, actor, item['id'], item['version'])
            if current is None or current['content'] != item['content']:
                raise EvidenceError('agent_evidence_changed')
            evidence.append({'kind': 'memory', 'id': current['id'], 'version': current['version']})
    elif tool_key in ('memory.save', 'memory.correct', 'memory.withdraw'):
        reference = {'kind': 'memory_change', 'id': result['memory_id'],
                     'version': result['version'], 'operation': result['operation']}
        if result['operation'] != tool_key.split('.')[1] or _current_memory_change(session, actor, reference) is None:
            raise EvidenceError('agent_evidence_changed')
        evidence.extend([{'kind': 'permission', 'key': 'memory.read'}, reference])
    elif tool_key == "catalog.search":
        for match in [*result["exact"], *result["candidates"]]:
            product = _scoped(session, CatalogProduct, actor, id=match["product_id"])
            snapshot = (_scoped(session, CatalogSnapshot, actor, id=product.current_snapshot_id)
                        if product else None)
            if (product is None or snapshot is None or snapshot.product_id != product.id
                    or product.source_version != match["source_version"]
                    or snapshot.captured_at != _time(match["captured_at"])):
                raise EvidenceError("agent_evidence_changed")
            evidence.append({"kind": "catalog", "id": product.id,
                             "snapshot_id": snapshot.id, "version": product.version})
    elif tool_key != "tasks.list_mine":
        # New read tools need a source adapter before their data becomes history.
        raise EvidenceError("agent_evidence_unsupported")
    return merge_evidence(evidence)


def message_reference(message: AgentMessage) -> dict:
    return {"kind": "message", "id": message.id, "version": message.version,
            "conversation_id": message.conversation_id}


def _current_memory(session, actor, identity, version):
    try:
        current = memory_service.get_memory(session, actor, identity)
    except (memory_service.MemoryError, HTTPException):
        return None
    return current if current['version'] == version else None


def _current_memory_change(session, actor, reference):
    operation = reference['operation']
    if operation not in ('save', 'correct', 'withdraw'):
        return None
    try:
        instant = memory_service._now()
        memory_service._require(session, actor, memory_service.READ, instant)
        row = memory_service._row(session, actor, reference['id'], instant, withdrawn=operation == 'withdraw')
        if row.version != reference['version'] or row.active != (operation != 'withdraw'):
            return None
        # Never return the old body or purpose for a withdrawn record.
        return {'id': row.id, 'version': row.version, 'updated_at': row.updated_at}
    except (memory_service.MemoryError, HTTPException):
        return None


def advance_memory_evidence(existing, captured):
    """Only use with Host-captured successful write refs in the effect transaction.

    A correction supersedes this Run's previous reference, never other Runs or
    historical answers. Metadata references still invalidate on later changes.
    """
    identities = {r['id'] for r in captured if r.get('kind') == 'memory_change'}
    retained = [r for r in existing if not (r.get('kind') in ('memory', 'memory_change') and r.get('id') in identities)]
    return merge_evidence(retained, captured)


def _allowed(session: Session, actor: AuthenticatedSubject, permission: str) -> bool:
    try:
        authorize_scope(session, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission=permission)
        return True
    except HTTPException as exc:
        if exc.status_code == 403 and exc.detail == "permission_denied":
            return False
        raise


def _reference_shape(reference) -> bool:
    if not isinstance(reference, dict):
        return False
    fields = {
        "permission": {"key": str},
        "message": {"id": str, "version": int, "conversation_id": str},
        "knowledge": {"id": str, "revision_id": str, "version": int, "revision_version": int},
        "catalog": {"id": str, "snapshot_id": str, "version": int},
        "memory": {"id": str, "version": int},
        "memory_change": {"id": str, "version": int, "operation": str},
        "guanjia_observation": {"id": str, "sha256": str},
        "guanjia_category": {"id": str, "sha256": str},
        "guanjia_sales_ranking": {"id": str, "sha256": str},
        "research": {"id": str, "sha256": str},
    }.get(reference.get("kind") if isinstance(reference.get("kind"), str) else None)
    return (fields is not None and set(reference) == {"kind", *fields}
            and all(type(reference[key]) is kind and
                    (0 < reference[key] if kind is int else 0 < len(reference[key]) <= 120)
                    for key, kind in fields.items()))


def validate_evidence(session: Session, actor: AuthenticatedSubject,
                      evidence: list[dict]) -> tuple[bool, list[dict]]:
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
        return False, []
    deadline=time.monotonic()+30.0
    if not all(_reference_shape(reference) for reference in evidence):return False,[]
    # Recheck even when this Session already saw the old permission/source rows.
    session.flush()
    session.expire_all()
    citations = []
    try:
        research_refs=[reference for reference in evidence if reference['kind']=='research']
        research_citations=[]
        if research_refs:
            from . import research_host
            research_citations=research_host.citations(session.get_bind().engine,actor,research_refs,deadline=deadline)
            if research_citations is None:return False,[]
        research_citations=iter(research_citations)
        for reference in evidence:
            if research_refs and time.monotonic()>=deadline:return False,[]
            if not _reference_shape(reference):
                return False, []
            kind = reference["kind"]
            if kind == "permission":
                if not _allowed(session, actor, reference["key"]):
                    return False, []
            elif kind == "message":
                message = _scoped(session, AgentMessage, actor, id=reference["id"])
                conversation = (_scoped(session, AgentConversation, actor,
                                         id=message.conversation_id) if message else None)
                if (message is None or message.version != reference["version"]
                        or message.state != "active" or conversation is None
                        or conversation.subject_id != actor.subject_id or conversation.status != "active"
                        or message.conversation_id != reference["conversation_id"]):
                    return False, []
            elif kind == "knowledge":
                if not _allowed(session, actor, "knowledge.read"):
                    return False, []
                entry = _scoped(session, KnowledgeEntry, actor, id=reference["id"])
                revision = _scoped(session, KnowledgeRevision, actor, id=reference["revision_id"])
                if (not _current_knowledge(actor, entry, revision, entry_version=reference["version"],
                                           revision_version=reference["revision_version"])
                        or not access_policy.can_read(session, actor, entry, revision)
                        or not _current_knowledge(actor, entry, revision, entry_version=reference["version"],
                                                  revision_version=reference["revision_version"])):
                    return False, []
                citations.append({"kind": "knowledge", "object_id": entry.id,
                                  "version_ref": revision.id, "version": str(revision.version),
                                  "label": revision.title, "source_ref": revision.source_ref,
                                  "observed_at": entry.approved_at.isoformat() if entry.approved_at else None})
            elif kind == "memory":
                current = _current_memory(session, actor, reference['id'], reference['version'])
                if current is None:
                    return False, []
                citations.append({'kind': 'memory', 'object_id': current['id'],
                                  'version_ref': str(current['version']), 'version': str(current['version']),
                                  'label': '个人记忆：' + current['purpose'],
                                  'source_ref': 'memory:' + current['id'],
                                  'observed_at': current['updated_at'].isoformat()})
            elif kind == 'memory_change':
                current = _current_memory_change(session, actor, reference)
                if current is None:
                    return False, []
                citations.append({'kind': 'memory', 'object_id': current['id'],
                    'version_ref': str(current['version']), 'version': str(current['version']),
                    'label': '助理记忆操作：' + reference['operation'],
                    'source_ref': 'memory:' + current['id'], 'observed_at': current['updated_at'].isoformat()})
            elif kind == 'research':
                citations.append(next(research_citations))
            elif kind == 'guanjia_sales_ranking':
                from backend.app.modules.integrations.guanjia import sales_ranking_source
                citation = sales_ranking_source.citation(session.get_bind().engine, actor, reference)
                if citation is None:
                    return False, []
                citations.append(citation)
            elif kind == 'guanjia_category':
                from backend.app.modules.integrations.guanjia import category_host
                # Category facts and current authority are re-read only in the
                # fixed FD-attested child; this Session supplies no facts.
                citation = category_host.citation(session.get_bind().engine,actor,reference)
                if citation is None:
                    return False, []
                citations.append(citation)
            elif kind == "guanjia_observation":
                from backend.app.modules.integrations.guanjia import observation_receipts
                citation = observation_receipts.citation(session.get_bind().engine, actor, reference)
                if citation is None:
                    return False, []
                citations.append(citation)
            elif kind == "catalog":
                if not _allowed(session, actor, "catalog.read"):
                    return False, []
                product = _scoped(session, CatalogProduct, actor, id=reference["id"])
                snapshot = _scoped(session, CatalogSnapshot, actor, id=reference["snapshot_id"])
                if (product is None or snapshot is None or not product.active
                        or snapshot.product_id != product.id or product.current_snapshot_id != snapshot.id
                        or product.version != reference["version"]):
                    return False, []
                citations.append({"kind": "catalog", "object_id": product.id,
                                  "version_ref": snapshot.id, "version": snapshot.source_version,
                                  "label": str((snapshot.payload or {}).get("name") or product.external_code),
                                  "source_ref": product.source_system + ":" + product.external_code,
                                  "observed_at": snapshot.captured_at.isoformat()})
            else:
                return False, []
    except (KeyError, TypeError, ValueError):
        return False, []
    if research_refs and time.monotonic()>=deadline:return False,[]
    return True, citations


def _unavailable(status: str) -> dict:
    return {"answer": None, "answer_status": status,
            "answer_unavailable_reason": "answer_or_source_unavailable" if status == "unavailable" else None,
            "citations": []}


def read_answer(session: Session, actor: AuthenticatedSubject, run: AgentRun) -> dict:
    """Caller checks agents.run; this read additionally checks every source."""
    session.flush()
    session.expire_all()
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission="agents.run")
    if run.requester_id != actor.subject_id:
        return _unavailable("unavailable")
    receipt = _scoped(session, AgentAnswer, actor, run_id=run.id)
    if receipt is None:
        return _unavailable("not_recorded")
    message = _scoped(session, AgentMessage, actor, id=receipt.message_id)
    conversation = (_scoped(session, AgentConversation, actor, id=message.conversation_id)
                    if message else None)
    if (run.state != "completed" or message is None or message.state != "active"
            or message.role != "assistant" or message.source_ref != f"run:{run.id}"
            or conversation is None or conversation.subject_id != actor.subject_id
            or conversation.status != "active"):
        return _unavailable("unavailable")
    valid, citations = validate_evidence(session, actor, receipt.evidence)
    if (not valid or message.state != "active" or conversation.status != "active"):
        return _unavailable("unavailable")
    # Display what this answer actually saw: receipts written with own_evidence
    # show only the sources captured from this run's tool calls, while the full
    # evidence list above still decides availability (fail-closed, unchanged).
    own = receipt.own_evidence
    if isinstance(own, list):
        identities = set()
        for reference in own:
            if not isinstance(reference, dict):
                continue
            if reference.get("kind") not in ("catalog", "knowledge", "memory", "memory_change", "guanjia_observation", "guanjia_category", "guanjia_sales_ranking", "research"):
                continue
            version_ref = (str(reference['version']) if reference['kind'] in ('memory', 'memory_change')
                           else reference.get("snapshot_id") or reference.get("revision_id") or reference.get('sha256'))
            if version_ref:
                identities.add(("memory" if reference["kind"] == "memory_change" else reference["kind"], reference["id"], version_ref))
        own_citations = [c for c in citations
                         if (c["kind"], c["object_id"], c["version_ref"]) in identities]
        citations = own_citations
    return {"answer": message.content, "answer_status": "available",
            "answer_unavailable_reason": None, "citations": citations}


def reusable_message_evidence(session: Session, actor: AuthenticatedSubject,
                              message: AgentMessage) -> list[dict] | None:
    if message.state != "active":
        return None
    if message.role == "assistant" and (message.source_ref or "").startswith("run:"):
        run_id = message.source_ref[4:]
        run = _scoped(session, AgentRun, actor, id=run_id)
        receipt = _scoped(session, AgentAnswer, actor, run_id=run_id)
        if (run is None or receipt is None or receipt.message_id != message.id
                or read_answer(session, actor, run)["answer_status"] != "available"):
            return None
        return merge_evidence(receipt.evidence, [message_reference(message)])
    # Unsourced user input/manual context remains untrusted, scoped text. This
    # lineage reference ensures revoking that message invalidates descendants.
    return [message_reference(message)]


def read_answer_with_evidence(session: Session, actor: AuthenticatedSubject, run_id: str) -> dict:
    """Opt-in CORE-009 view; legacy read_answer and stored receipt bytes stay intact.

    The query layer obtains citations from the persisted, currently valid
    receipt. A model-provided list cannot enter this API or mint trusted history.
    """
    from backend.app.modules.evidence.service import read_receipt_evidence
    return read_receipt_evidence(session, actor, run_id)
