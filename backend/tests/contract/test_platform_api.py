import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.core.config import database_url
from backend.app.db.base import Base
from backend.app.db.session import get_session, make_engine
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.core.idempotency import require_idempotency_key
from backend.app.main import app, http_error_handler, openapi_document
from backend.app.modules.identity.models import Organization, Store, Subject
from backend.app.modules.identity.permission_admin import KNOWN_PERMISSIONS
from sqlalchemy.orm import sessionmaker

ENVELOPE_KEYS = {"success", "data", "code", "error", "user_message", "hint"}


async def request(target_app, path, headers=(), *, method="GET", payload=None):
    messages = []
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": json.dumps(payload).encode() if payload is not None else b"", "more_body": False}

    async def send(message):
        messages.append(message)

    parsed = urlsplit(path)
    request_headers = [(b"host", b"testserver"), *headers]
    if payload is not None:
        request_headers.append((b"content-type", b"application/json"))
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1", "method": method, "scheme": "http", "path": parsed.path, "raw_path": parsed.path.encode(), "query_string": parsed.query.encode(), "headers": request_headers, "client": ("testclient", 1), "server": ("testserver", 80)}
    await target_app(scope, receive, send)
    response = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return response["status"], json.loads(body)


