"""P1-C projection ranking (corrected A/B boundary).

The formal path never re-reads a body: A gets the exact database token score
and compact citation metadata from ``projection.load_window``; the fresh B
worker only applies the original product/category scope regex, the stable
``(-score, submitted_at)`` order and the 1..20 citation cap.  The legacy
full-text scorer is kept below as a *test oracle* for value-for-value parity;
it is not called by the P1-C chain.
"""
from __future__ import annotations

from datetime import datetime
import json
import re

SNAPSHOT_FORMAT = 'agt045-p1c-projection-v1'
SNAPSHOT_PROFILE = 'knowledge_projection_rank@1'
CITATION_FIELDS = (
    'entry_id', 'revision_id', 'version', 'title', 'excerpt', 'source_type',
    'source_ref', 'approved_by', 'approved_at', 'expires_at', 'scope_type',
    'scope_ref',
)
PROJECTION_CANDIDATE_FIELDS = (
    'entry_id', 'revision_id', 'entry_version', 'revision_version', 'title',
    'excerpt', 'source_type', 'source_ref', 'scope_type', 'scope_ref',
    'submitted_at', 'approved_by', 'approved_at', 'expires_at', 'score',
)
HEADER_FIELDS = (
    'format', 'profile', 'count', 'generation', 'build_id',
    'source_generation', 'window_limit', 'window_expires_at',
    'resolver_version', 'normalizer_version', 'projection_schema_version', 'query',
)


def tokens(text: str) -> set[str]:
    return {token for token in re.split(r'\s+', (text or '').lower()) if token}


def _timestamp(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value


def _scope_matches(text: str, item: dict) -> bool:
    if item['scope_type'] not in ('product', 'category'):
        return True
    reference = (item.get('scope_ref') or '').strip()
    if not reference:
        return False
    pattern = r'(?<![A-Za-z0-9_-])' + re.escape(reference) + r'(?![A-Za-z0-9_-])'
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def rank_candidates(candidates, *, query: str, limit: int = 5) -> dict:
    """B: original scope regex, stable score order, cap and citation shape."""
    text = (query or '').strip()
    if not text:
        return {'query': query or '', 'citations': [], 'insufficient': True,
                'reason': 'empty_query', 'answer': None}
    scored = []
    for item in candidates:
        if not _scope_matches(text, item):
            continue
        scan = int(item['score'])
        if scan <= 0:
            continue
        scored.append((scan, _timestamp(item.get('submitted_at')), item))
    scored.sort(key=lambda row: (-row[0], row[1]))
    cap = max(1, min(int(limit), 20))
    citations = []
    for _score, _submitted, item in scored[:cap]:
        citations.append({
            'entry_id': item['entry_id'],
            'revision_id': item['revision_id'],
            'version': item['revision_version'],
            'title': item['title'],
            'excerpt': item.get('excerpt') or '',
            'source_type': item['source_type'],
            'source_ref': item.get('source_ref'),
            'approved_by': item.get('approved_by'),
            'approved_at': item.get('approved_at'),
            'expires_at': item.get('expires_at'),
            'scope_type': item['scope_type'],
            'scope_ref': item.get('scope_ref'),
        })
    return {'query': text, 'citations': citations, 'insufficient': not citations,
            'reason': None if citations else 'insufficient_evidence', 'answer': None}


def legacy_score_candidates(candidates, *, query: str, limit: int = 5) -> dict:
    """Test-only oracle: the original full-text ``score_materialized`` shape.

    ``candidates`` here must still carry ``body``; P1-C production never calls
    this function and never carries a body across the read boundary.
    """
    text = (query or '').strip()
    if not text:
        return {'query': query or '', 'citations': [], 'insufficient': True,
                'reason': 'empty_query', 'answer': None}
    query_tokens = tokens(text)
    scored = []
    for item in candidates:
        if item['scope_type'] in ('product', 'category'):
            reference = (item.get('scope_ref') or '').strip()
            if not reference:
                continue
            pattern = r'(?<![A-Za-z0-9_-])' + re.escape(reference) + r'(?![A-Za-z0-9_-])'
            if re.search(pattern, text, flags=re.IGNORECASE) is None:
                continue
        haystack = ' '.join(filter(None, (item.get('title'), item.get('body'),
                                          item.get('source_ref'), item.get('scope_ref'))))
        lowered = haystack.lower()
        overlap = sum(1 for token in query_tokens if token in lowered)
        if overlap:
            scored.append((overlap, _timestamp(item.get('submitted_at')), item))
    scored.sort(key=lambda row: (-row[0], row[1]))
    citations = []
    for _, _, item in scored[:max(1, min(limit, 20))]:
        approved_at = item.get('approved_at')
        expires_at = item.get('expires_at')
        citations.append({
            'entry_id': item['entry_id'],
            'revision_id': item['revision_id'],
            'version': item['revision_version'],
            'title': item['title'],
            'excerpt': (item.get('body') or '')[:400],
            'source_type': item['source_type'],
            'source_ref': item.get('source_ref'),
            'approved_by': item.get('approved_by'),
            'approved_at': _timestamp(approved_at).isoformat() if approved_at else None,
            'expires_at': _timestamp(expires_at).isoformat() if expires_at else None,
            'scope_type': item['scope_type'],
            'scope_ref': item.get('scope_ref'),
        })
    return {'query': text, 'citations': citations, 'insufficient': not citations,
            'reason': None if citations else 'insufficient_evidence', 'answer': None}


def snapshot_bytes(header: dict, candidates) -> bytes:
    """Canonical private snapshot: one metadata header plus compact rows."""
    if set(header) != set(HEADER_FIELDS):
        raise ValueError('snapshot_header_fields')
    lines = [json.dumps(header, sort_keys=True, ensure_ascii=False,
                        separators=(',', ':'))]
    for candidate in candidates:
        if set(candidate) != set(PROJECTION_CANDIDATE_FIELDS):
            raise ValueError('candidate_fields')
        lines.append(json.dumps(candidate, sort_keys=True, ensure_ascii=False,
                                separators=(',', ':')))
    return ('\n'.join(lines) + '\n').encode('utf-8')


def result_bytes(result: dict) -> bytes:
    return json.dumps(result, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')
