"""Owner-scoped on-demand recall over the single Host memory authority.

Hermes literal fallback is used for Chinese short words and quoted phrases.
No second store, embedding call, persistent index, transcript, or cache. Every
query revalidates the current login and permission and scopes before matching.
"""
from sqlalchemy import and_, or_, select

from backend.app.modules.memory import service
from backend.app.modules.memory.hermes_query import literal_pattern, literal_terms
from backend.app.modules.memory.models import PersonalMemory


def validate_search_query(query, limit=5):
    """Pure existing semantics; callers must retain their authority checks first."""
    if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 256
            or '\x00' in query or any(0xD800 <= ord(c) <= 0xDFFF for c in query)):
        raise service.MemoryError('memory_query_invalid')
    if type(limit) is not int or not 1 <= limit <= 20:
        raise service.MemoryError('memory_limit_invalid')
    terms = literal_terms(query.strip())
    if len(terms) > 16:
        raise service.MemoryError('memory_query_invalid')
    return terms


def search_memories(session, actor, *, query, limit=5, now=None):
    current = service._now(now)
    service._require(session, actor, service.READ, current)
    terms = validate_search_query(query, limit)
    if not terms:
        return {'items': [], 'match_mode': 'none', 'authority': 'reference_only'}
    base = select(PersonalMemory).where(
        *service._scope(actor), PersonalMemory.active.is_(True),
        service._unexpired(current),
        service._visibility_clause(actor),
    ).order_by(PersonalMemory.updated_at.desc(), PersonalMemory.id).limit(limit)
    # ilike compiles to a bound parameter and explicit ESCAPE on both supported
    # databases; no FTS syntax or query interpolation is accepted from the model.
    matches = [PersonalMemory.content.ilike(literal_pattern(term), escape='\\') for term in terms]
    rows = list(session.scalars(base.where(and_(*matches))))
    mode = 'all_terms'
    if not rows and len(terms) > 1:
        rows = list(session.scalars(base.where(or_(*matches))))
        mode = 'any_terms'
    return {'items': [service._out(row) for row in rows],
            'match_mode': mode if rows else 'none', 'authority': 'reference_only'}
