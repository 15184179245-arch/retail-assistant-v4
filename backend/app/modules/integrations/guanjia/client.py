"""Read-only GuanJia client boundary. Write actions are not representable."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

READ_ACTIONS = frozenset({"authenticate", "product_page"})
PAGE_SIZE_LIMIT = 500


class GuanJiaError(RuntimeError):
    pass


class ReadOnlyViolation(GuanJiaError):
    pass


class RateLimited(GuanJiaError):
    def __init__(self, retry_after: float = 1.0) -> None:
        super().__init__(f"rate limited; retry after {retry_after}s")
        self.retry_after = retry_after


class PageError(GuanJiaError):
    pass


class Transport(Protocol):
    def fetch(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class ProductPage:
    skip: int
    items: tuple[dict[str, Any], ...]
    total: int | None


class GuanJiaClient:
    """Only the two registered read actions can be called; all else raises."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._seen_page_signatures: set[tuple[int, int]] = set()

    def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action not in READ_ACTIONS:
            raise ReadOnlyViolation(f"read-only client rejects action {action!r}")
        return self._transport.fetch(action, params)

    def iter_product_pages(self, *, page_size: int = 100,
                           limit: int | None = None,
                           start_skip: int = 0) -> Iterator[ProductPage]:
        if page_size <= 0 or page_size > PAGE_SIZE_LIMIT:
            raise ValueError("page_size must be within 1..500")
        if start_skip < 0:
            raise ValueError("start_skip must be non-negative")
        skip = start_skip
        fetched = 0
        last_total: int | None = None
        seen_identities: set[str] = set()
        seen_page_content: set[tuple[str, ...]] = set()
        self._seen_page_signatures.clear()
        while limit is None or fetched < limit:
            response = self.call("product_page", {"skip": skip, "take": page_size})
            items = response.get("items")
            if not isinstance(items, list):
                raise PageError("product_page.items must be a list")
            total = response.get("total")
            if total is not None and last_total is not None and total < last_total:
                raise PageError("total_count regressed; refusing to continue")
            if total is not None and last_total is not None and total != last_total:
                raise PageError("source total changed during full read; refusing completion")
            signature = (skip, len(items))
            if signature in self._seen_page_signatures and items:
                raise PageError("duplicate page offset/size detected")
            self._seen_page_signatures.add(signature)
            identities: list[str] = []
            for item in items:
                if not isinstance(item, dict):
                    raise PageError("product page item must be an object")
                identity = str(item.get("code") or json.dumps(item, sort_keys=True))
                if identity in seen_identities:
                    raise PageError(f"duplicate product across pages: {identity!r}")
                seen_identities.add(identity)
                identities.append(identity)
            content = tuple(sorted(identities))
            if content and content in seen_page_content:
                raise PageError("duplicate page content across different offsets")
            seen_page_content.add(content)
            if not items and total is not None and skip < total:
                raise PageError(
                    f"source ended early at skip={skip} but total={total}")
            page = ProductPage(skip=skip, items=tuple(items), total=total)
            yield page
            if not items:
                return
            fetched += len(items)
            skip += len(items)  # use actual page length, never assume page_size
            last_total = total
            if total is not None and skip >= total:
                return
