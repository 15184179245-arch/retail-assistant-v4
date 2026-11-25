"""Media metadata is immutable; bytes are content-addressed and scope-bound."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.media import local_storage
from backend.app.modules.media.local_storage import MediaStorageError
from backend.app.modules.media.models import MediaAsset

WRITE = "media.write"
READ = "media.read"
CATEGORIES = ("category_image", "inventory_photo")


class MediaError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise MediaError("media_time_invalid")
    return current.astimezone(timezone.utc)


def _require(session: Session, actor: AuthenticatedSubject, permission: str,
             now: datetime) -> None:
    # Preserve 401/403 from the shared authorization layer; media errors are
    # only for media-specific validation/not-found cases.
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission=permission, now=now)


def _asset_out(asset: MediaAsset) -> dict:
    return {
        "id": asset.id, "category": asset.category, "source": asset.source,
        "original_name": asset.original_name, "content_type": asset.content_type,
        "byte_size": asset.byte_size, "sha256": asset.sha256,
        "created_by": asset.created_by, "created_at": asset.created_at,
    }


def upload(session: Session, actor: AuthenticatedSubject, *, filename: str,
           data: bytes, category: str, source: str, now: datetime | None = None) -> dict:
    current = _now(now)
    _require(session, actor, WRITE, current)
    if category not in CATEGORIES:
        raise MediaError("media_category_invalid")
    if not source or len(source) > 80:
        raise MediaError("media_source_invalid")
    if not filename or len(filename) > 255:
        raise MediaError("media_name_invalid")
    try:
        content_type = local_storage.detect_content_type(data)
    except MediaStorageError as error:
        raise MediaError(str(error)) from error
    sha256 = local_storage.digest(data)
    existing = session.scalar(select(MediaAsset).where(
        MediaAsset.organization_id == actor.organization_id,
        MediaAsset.store_id == actor.store_id,
        MediaAsset.sha256 == sha256,
    ))
    if existing is not None:
        if existing.category != category:
            raise MediaError("media_dedupe_conflict")
        return _asset_out(existing)
    key = local_storage.storage_key(actor.organization_id, actor.store_id, sha256)
    try:
        local_storage.write_immutable(data, key)
    except MediaStorageError as error:
        raise MediaError(str(error)) from error
    asset = MediaAsset(
        id=str(uuid4()), organization_id=actor.organization_id,
        store_id=actor.store_id, category=category, source=source,
        original_name=filename, content_type=content_type, byte_size=len(data),
        sha256=sha256, storage_key=key, created_by=actor.subject_id,
        created_at=current,
    )
    try:
        with session.begin_nested():
            session.add(asset)
            session.flush()
    except IntegrityError:
        replay = session.scalar(select(MediaAsset).where(
            MediaAsset.organization_id == actor.organization_id,
            MediaAsset.store_id == actor.store_id,
            MediaAsset.sha256 == sha256,
        ))
        if replay is None:
            raise
        if replay.category != category:
            raise MediaError("media_dedupe_conflict")
        return _asset_out(replay)
    record_event(session, actor_id=actor.subject_id,
                 organization_id=actor.organization_id, store_id=actor.store_id,
                 action="media.asset.created", target_type="media_asset",
                 target_id=asset.id, idempotency_key=f"media:{sha256}",
                 details={"category": category, "sha256": sha256,
                          "content_type": content_type, "byte_size": len(data)},
                 occurred_at=current)
    return _asset_out(asset)


def list_assets(session: Session, actor: AuthenticatedSubject, *, limit: int,
                now: datetime | None = None) -> list[dict]:
    current = _now(now)
    _require(session, actor, READ, current)
    rows = session.scalars(select(MediaAsset).where(
        MediaAsset.organization_id == actor.organization_id,
        MediaAsset.store_id == actor.store_id,
    ).order_by(MediaAsset.created_at.desc(), MediaAsset.id).limit(limit))
    return [_asset_out(row) for row in rows]


def get_asset(session: Session, actor: AuthenticatedSubject, asset_id: str,
              *, now: datetime | None = None) -> tuple[MediaAsset, dict]:
    current = _now(now)
    _require(session, actor, READ, current)
    asset = session.scalar(select(MediaAsset).where(
        MediaAsset.organization_id == actor.organization_id,
        MediaAsset.store_id == actor.store_id,
        MediaAsset.id == asset_id,
    ))
    if asset is None:
        raise MediaError("media_asset_not_found")
    return asset, _asset_out(asset)


def content_path(asset: MediaAsset) -> Path:
    root = local_storage.media_root()
    path = (root / asset.storage_key).resolve()
    if root not in path.parents or not path.is_file():
        raise MediaError("media_content_missing")
    return path
