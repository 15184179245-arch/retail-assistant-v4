"""Literal term collector adapted from pi-hermes-memory 0.10.0 (MIT).

Copyright (c) Chandra Teja; full license and exact source under
third_party/pi-hermes-memory-query. Only collectLikeTerms is ported: explicit
FTS operators are never executed by this adapter. No model or subprocess.
"""
import re

_TOKENS = re.compile(r'"([^"]*)"|(\S+)')
_CONNECTORS = frozenset(('and', 'or', 'not', 'near'))


def literal_terms(query: str) -> list[str]:
    terms = []
    for match in _TOKENS.finditer(query):
        phrase, term = match.groups()
        if phrase is None and term and term.lower() in _CONNECTORS:
            continue
        value = phrase if phrase is not None else (term or '')
        if value:
            terms.append(value)
    return terms


def literal_pattern(term: str) -> str:
    """LIKE metacharacters are literal user text, never wildcard expansion."""
    return '%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
