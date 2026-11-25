"""Read a complete GuanJia category directory without treating display IDs as business IDs.

This is a Host-internal read primitive. It does not authorize a caller, publish
the directory, or make a Pi tool visible. The transport owns fixed HTTPS and
authentication; this layer refuses partial or inconsistent results.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json


PAGE_SIZE = 500
MAX_CATEGORIES = 10_000


class CategoryDirectoryError(ValueError):
    pass


def _deny() -> None:
    raise CategoryDirectoryError('guanjia_category_directory_incomplete')


def _text(value: object, *, allow_root: bool = False) -> str:
    if (type(value) is not str or not value or len(value) > 200
            or value != value.strip() or any(ord(char) < 32 for char in value)):
        _deny()
    if value == '0' and not allow_root:
        _deny()
    return value


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        _deny()


@dataclass(frozen=True)
class Category:
    business_id: str
    name: str
    code: str
    parent_business_id: str | None
    path: tuple[str, ...]


@dataclass(frozen=True)
class CategoryHit:
    category: Category
    descendant_business_ids: tuple[str, ...]


@dataclass(frozen=True)
class CategoryDirectory:
    categories: tuple[Category, ...]
    content_sha256: str
    observed_at: datetime
    complete: bool = True

    def find(self, query: str, *, limit: int = 20) -> tuple[tuple[CategoryHit, ...], bool]:
        """Return all matching category identities, with bounded presentation.

        An ambiguous name stays a list of hits. Children are expanded using
        business IDs; callers must not silently choose the first hit.
        """
        if (type(query) is not str or not query.strip() or len(query) > 200
                or type(limit) is not int or not 1 <= limit <= 100):
            _deny()
        needle = query.strip().casefold()
        matches = [category for category in self.categories
                   if needle in category.name.casefold() or needle in category.code.casefold()]
        by_id = {category.business_id: category for category in self.categories}
        hits = []
        for category in matches[:limit]:
            descendants = tuple(item.business_id for item in self.categories
                                if category.business_id == item.business_id
                                or category.business_id in _ancestors(item, by_id))
            hits.append(CategoryHit(category, descendants))
        return tuple(hits), len(matches) > limit


def _ancestors(category: Category, by_id: dict[str, Category]) -> tuple[str, ...]:
    seen: set[str] = set()
    result = []
    parent = category.parent_business_id
    while parent is not None:
        if parent in seen or parent not in by_id:
            _deny()
        seen.add(parent)
        result.append(parent)
        parent = by_id[parent].parent_business_id
    return tuple(result)


def _pages(transport: object, action: str) -> tuple[dict, ...]:
    if action not in ('category_flat', 'category_tree'):
        _deny()
    result: list[dict] = []
    total: int | None = None
    while True:
        skip = len(result)
        params: dict[str, object] = {'skipCount': skip, 'maxResultCount': PAGE_SIZE}
        if action == 'category_flat':
            params['keyword'] = ''
        page = transport.query(action, params)
        if type(page) is not dict or type(page.get('totalCount')) is not int:
            _deny()
        reported = page['totalCount']
        items = page.get('items')
        if (reported < 0 or reported > MAX_CATEGORIES or type(items) is not list
                or len(items) > PAGE_SIZE or any(type(item) is not dict for item in items)
                or (total is not None and reported != total)
                or skip + len(items) > reported
                or (not items and skip < reported)):
            _deny()
        total = reported
        result.extend(items)
        if len(result) == total:
            return tuple(result)


def read_category_directory(transport: object) -> CategoryDirectory:
    """Read both paged views once; fail closed on incomplete or incoherent trees."""
    flat_rows = _pages(transport, 'category_flat')
    tree_rows = _pages(transport, 'category_tree')
    flat: dict[str, tuple[str, str]] = {}
    for row in flat_rows:
        identity = _text(row.get('id'))
        if identity in flat:
            _deny()
        flat[identity] = (_text(row.get('name')), _text(row.get('code')))
    tree: dict[str, tuple[str, str | None]] = {}
    for row in tree_rows:
        _text(row.get('id'), allow_root=True)  # display only; siblings may reuse 0
        identity = _text(row.get('value'))
        parent = _text(row.get('parId'), allow_root=True)
        if identity in tree:
            _deny()
        tree[identity] = (_text(row.get('name')), None if parent == '0' else parent)
    if set(tree) != set(flat):
        _deny()
    for identity, (name, parent) in tree.items():
        if name != flat[identity][0] or (parent is not None and parent not in tree):
            _deny()
    categories = []
    for identity in sorted(tree):
        names = [tree[identity][0]]
        seen = {identity}
        parent = tree[identity][1]
        while parent is not None:
            if parent in seen:
                _deny()
            seen.add(parent)
            names.append(tree[parent][0])
            parent = tree[parent][1]
        categories.append(Category(identity, tree[identity][0], flat[identity][1],
                                   tree[identity][1], tuple(reversed(names))))
    digest = sha256(_canonical({'flat': flat_rows, 'tree': tree_rows}).encode()).hexdigest()
    return CategoryDirectory(tuple(categories), digest, datetime.now(timezone.utc))
