"""Authorized materialization and pure, dependency-free KNW-002 scoring."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from backend.app.core.security import AuthenticatedSubject


@dataclass(frozen=True, slots=True)
class KnowledgeCandidate:
    entry_id: str
    revision_id: str
    entry_version: int
    revision_version: int
    title: str
    body: str
    source_type: str
    source_ref: str | None
    scope_type: str
    scope_ref: str | None
    submitted_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    expires_at: datetime | None


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r'\s+', (text or '').lower()) if token}


def score_materialized(candidates, *, query: str, limit: int = 5) -> dict:
    """Original full-text/token/tie behavior; no DB, auth, I/O or model imports."""
    text = (query or '').strip()
    if not text:
        return {'query': query or '', 'citations': [], 'insufficient': True,
                'reason': 'empty_query', 'answer': None}
    tokens = _tokens(text)
    scored = []
    for item in candidates:
        if item.scope_type in ('product', 'category'):
            reference = (item.scope_ref or '').strip()
            if not reference:
                continue
            pattern = r'(?<![A-Za-z0-9_-])' + re.escape(reference) + r'(?![A-Za-z0-9_-])'
            if re.search(pattern, text, flags=re.IGNORECASE) is None:
                continue
        haystack = ' '.join(filter(None, (item.title, item.body, item.source_ref, item.scope_ref)))
        lowered = haystack.lower()
        overlap = sum(1 for token in tokens if token in lowered)
        if overlap:
            scored.append((overlap, item))
    scored.sort(key=lambda item: (-item[0], item[1].submitted_at), reverse=False)
    citations = []
    for _, item in scored[:max(1, min(limit, 20))]:
        citations.append({'entry_id': item.entry_id, 'revision_id': item.revision_id,
            'version': item.revision_version, 'title': item.title, 'excerpt': item.body[:400],
            'source_type': item.source_type, 'source_ref': item.source_ref,
            'approved_by': item.approved_by,
            'approved_at': item.approved_at.isoformat() if item.approved_at else None,
            'expires_at': item.expires_at.isoformat() if item.expires_at else None,
            'scope_type': item.scope_type, 'scope_ref': item.scope_ref})
    return {'query': text, 'citations': citations, 'insufficient': not citations,
            'reason': None if citations else 'insufficient_evidence', 'answer': None}


def materialize_knowledge(session: Session, actor: AuthenticatedSubject, *, query: str,
                          now: datetime | None = None) -> tuple[KnowledgeCandidate, ...]:
    # Preserve the legacy empty-query behavior: no auth or content query.
    if not (query or '').strip():
        return ()
    from backend.app.modules.knowledge import service
    return service.materialize_usable_entries(session, actor, limit=10_000, now=now)


def search_knowledge(session: Session, actor: AuthenticatedSubject, *, query: str,
                     limit: int = 5, now: datetime | None = None) -> dict:
    candidates = materialize_knowledge(session, actor, query=query, now=now)
    return score_materialized(candidates, query=query, limit=limit)
