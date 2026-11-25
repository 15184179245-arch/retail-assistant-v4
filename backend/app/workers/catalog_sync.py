"""CAT-003: default dry-run catalog sync with resumable cursor and idempotency."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from backend.app.modules.catalog import repository, service
from backend.app.modules.integrations.guanjia.catalog_mapper import map_product_item
from backend.app.modules.integrations.guanjia.client import (GuanJiaClient, PageError,
                                                             RateLimited, ReadOnlyViolation)

DEFAULT_STATE = Path("data-local/catalog/sync-state.json")


class SyncError(RuntimeError):
    pass


@dataclass
class SyncReport:
    scanned: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    pages: int = 0
    next_skip: int = 0
    completed: bool = False
    dry_run: bool = True


def _load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SyncError(f"invalid sync cursor: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("next_skip", 0), int):
        raise SyncError("invalid sync cursor shape")
    return data


def _save_state(path: Path, *, next_skip: int, completed: bool,
                source_version: str, source_sequence: int | None,
                organization_id: str, store_id: str,
                source_system: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "next_skip": next_skip,
        "completed": completed,
        "source_version": source_version,
        "source_sequence": source_sequence,
        "organization_id": organization_id,
        "store_id": store_id,
        "source_system": source_system,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False), encoding="utf-8")


def run_sync(session: Session, client: GuanJiaClient, *, organization_id: str,
             store_id: str, source_system: str, source_version: str,
             state_path: Path = DEFAULT_STATE, page_size: int = 100,
             dry_run: bool = True, max_retries: int = 2,
             sleep: Callable[[float], None] = time.sleep,
             resume: bool = True, source_sequence: int | None = None) -> SyncReport:
    # The persisted source watermark is a safety invariant, not a progress
    # preference: it must be checked even when resume=False.
    state = _load_state(state_path)
    report = SyncReport(dry_run=dry_run)
    # A resumed run always revalidates the source from the beginning. The
    # persisted cursor is an audit checkpoint, not permission to skip pages:
    # otherwise a restart could miss duplicates/early end across the boundary.
    start = 0
    if state:
        bound = (state.get("organization_id"), state.get("store_id"),
                 state.get("source_system"))
        if bound != (organization_id, store_id, source_system):
            raise SyncError("sync cursor belongs to another organization/store/source")
        stored_version = state.get("source_version")
        stored_sequence = state.get("source_sequence")
        if stored_version != source_version:
            if (source_sequence is None or stored_sequence is None
                    or source_sequence <= int(stored_sequence)):
                raise SyncError(
                    "source version changed without an increasing source_sequence")
        elif source_sequence is not None and stored_sequence is not None:
            if source_sequence < int(stored_sequence):
                raise SyncError("source_sequence regressed; refusing to publish older data")
    skip = start
    completed = False
    try:
        iterator = client.iter_product_pages(page_size=page_size, start_skip=start)
        while True:
            try:
                page = next(iterator)
            except StopIteration:
                completed = True
                break
            except RateLimited as error:
                if max_retries <= 0:
                    raise SyncError(f"rate limited at skip {skip}") from error
                max_retries -= 1
                sleep(error.retry_after)
                # Restart from 0 so already-seen identities/page content are
                # rebuilt; skipping directly to the failed page would forget
                # what was already verified in this round.
                iterator = client.iter_product_pages(page_size=page_size, start_skip=0)
                continue
            for item in page.items:
                try:
                    payload = map_product_item(item)
                except ValueError as error:
                    raise SyncError(f"invalid source item: {error}") from error
                report.scanned += 1
                existing = repository.find_by_external(
                    session, organization_id, store_id, source_system, payload["code"])
                if dry_run:
                    if existing is None:
                        report.new += 1
                    else:
                        report.unchanged += 1
                    continue
                before_snapshot = existing.current_snapshot_id if existing else None
                product, snapshot, _ = service.record_snapshot(
                    session, organization_id=organization_id, store_id=store_id,
                    source_system=source_system, source_version=source_version,
                    source_updated_at=None, payload=payload)
                if existing is None:
                    report.new += 1
                elif before_snapshot != snapshot.id:
                    report.changed += 1
                else:
                    report.unchanged += 1
            report.pages += 1
            skip = page.skip + len(page.items)
            report.next_skip = skip
            if not dry_run:
                # A page cursor is only meaningful after the page's V4 writes
                # are committed; otherwise a rollback would leave a cursor
                # pointing past uncommitted data.
                session.commit()  # commit before persisting a page cursor
                _save_state(state_path, next_skip=skip, completed=False,
                            source_version=source_version,
                            source_sequence=source_sequence,
                            organization_id=organization_id, store_id=store_id,
                            source_system=source_system)
            if not page.items:
                completed = True
                break
    except (PageError, ReadOnlyViolation) as error:
        # Do not advance the cursor on a duplicate/regression or write attempt.
        raise SyncError(str(error)) from error
    report.completed = completed
    report.next_skip = skip
    if not dry_run and completed:
        from backend.app.modules.search.projection import rebuild
        rebuild(session, organization_id=organization_id, store_id=store_id)
        session.commit()
        _save_state(state_path, next_skip=skip, completed=True,
                    source_version=source_version,
                    source_sequence=source_sequence,
                    organization_id=organization_id, store_id=store_id,
                    source_system=source_system)
    return report
