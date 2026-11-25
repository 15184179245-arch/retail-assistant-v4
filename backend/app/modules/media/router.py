from typing import Annotated

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Response
from sqlalchemy.orm import Session

from backend.app.core.errors import ApiEnvelope
from backend.app.modules.media.schemas import (MediaAssetEnvelope,
                                               MediaAssetListEnvelope)
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.media import service

router = APIRouter(prefix="/media", tags=["media"])


def _http(error: service.MediaError) -> HTTPException:
    status = 400
    if error.code == "media_asset_not_found":
        status = 404
    elif error.code == "media_dedupe_conflict":
        status = 409
    return HTTPException(status_code=status, detail=error.code)


@router.post("/assets", response_model=MediaAssetEnvelope, status_code=201,
             openapi_extra={"requestBody": {
                 "required": True,
                 "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
             }})
async def upload_route(
    request: Request,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    category: Annotated[str, Query()],
    source: Annotated[str, Query(min_length=1, max_length=80)],
) -> ApiEnvelope:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > 5 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="media_too_large")
        except ValueError:
            raise HTTPException(status_code=400, detail="media_empty")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 5 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="media_too_large")
        chunks.append(chunk)
    data = b"".join(chunks)
    try:
        asset = service.upload(session, actor, filename=filename,
                               data=data, category=category, source=source)
        session.commit()
    except service.MediaError as error:
        session.rollback()
        raise _http(error) from error
    return MediaAssetEnvelope(success=True, data=asset, code="OK", error=None,
                              user_message="媒体元数据已保存。", hint=None)


@router.get("/assets", response_model=MediaAssetListEnvelope)
def list_route(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    limit: int = Query(default=50, ge=1, le=100),
) -> ApiEnvelope:
    try:
        items = service.list_assets(session, actor, limit=limit)
    except service.MediaError as error:
        raise _http(error) from error
    return MediaAssetListEnvelope(success=True, data={"items": items}, code="OK",
                                  error=None, user_message="媒体列表已读取。", hint=None)


@router.get("/assets/{asset_id}", response_model=MediaAssetEnvelope)
def detail_route(
    asset_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> ApiEnvelope:
    try:
        _, asset = service.get_asset(session, actor, asset_id)
    except service.MediaError as error:
        raise _http(error) from error
    return MediaAssetEnvelope(success=True, data=asset, code="OK", error=None,
                              user_message="媒体元数据已读取。", hint=None)


@router.get("/assets/{asset_id}/content", response_class=Response,
            responses={200: {"content": {
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
                "image/webp": {"schema": {"type": "string", "format": "binary"}},
            }}})
def content_route(
    asset_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    try:
        asset, _ = service.get_asset(session, actor, asset_id)
        data = service.content_bytes(asset)
    except service.MediaError as error:
        raise _http(error) from error
    encoded = quote(asset.original_name, safe="")
    return Response(content=data, media_type=asset.content_type,
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{encoded}"})
