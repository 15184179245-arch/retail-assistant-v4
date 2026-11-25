"""Candidate CORE-009 citation sidecar; not a production integration.

The caller, not this pure formatter, owns authorization, complete dependency
revalidation and own-evidence filtering. No ORM serializer is executed here.
Existing citation values are preserved, not validated against an ORM schema.
Only the outer list and each citation dict are copied; nested values may alias.
Unknown metadata does not establish verification, completeness or a watermark.
"""

from __future__ import annotations

_CITATION_KEYS = frozenset({
    "kind", "object_id", "version_ref", "version",
    "label", "source_ref", "observed_at",
})


def build_citation_sidecar(citations: list[dict]) -> dict:
    """Return a structural candidate projection without reading or changing state."""
    if type(citations) is not list:
        raise ValueError("invalid_citations")
    copied = []
    for citation in citations:
        if (type(citation) is not dict
                or set(citation) != _CITATION_KEYS
                or type(citation["kind"]) is not str
                or citation["kind"] not in ("catalog", "knowledge")):
            raise ValueError("invalid_citations")
        copied.append(dict(citation))
    return {
        "citations": copied,
        "evidence_metadata": {
            "schema_version": 1,
            "as_of": "unknown",
            "watermark": "unknown",
            "coverage": "unknown",
            "truncated": "unknown",
            "verification": "unknown",
        },
    }


# This DTO is separate from the legacy citation sidecar above. It is constructed
# only by the authorized read service, not by model-supplied citation JSON.
def build_query_page(kind, items, *, offset, limit, truncated, queried_at):
    """JSON-only projection; a counted local population is not upstream coverage."""
    from copy import deepcopy
    from datetime import datetime

    selected = deepcopy(items[offset:offset + limit])
    incomplete = bool(truncated)
    has_more = incomplete or offset + len(selected) < len(items)
    dates = [item['as_of'] for item in items]
    watermark = 'unknown'
    if dates and not incomplete and all(value != 'unknown' for value in dates):
        watermark = max(dates, key=lambda value: datetime.fromisoformat(value))
    for item in selected:
        item['truncated'] = has_more
    return {
        'schema_version': 1, 'kind': kind, 'items': selected,
        'queried_at': queried_at, 'coverage': 'unknown',
        'truncated': has_more, 'verification': 'unknown',
        'watermark': watermark,
        'watermark_basis': 'visible_local_sources' if watermark != 'unknown' else 'unknown',
        'page': {'offset': offset, 'limit': limit, 'returned': len(selected),
                 'total': 'unknown' if incomplete else len(items),
                 'total_basis': 'visible_local_sources', 'has_more': has_more},
    }
