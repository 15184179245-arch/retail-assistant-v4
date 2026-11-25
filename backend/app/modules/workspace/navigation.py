"""Neutral cross-device navigation references, not artifact content or authority."""
from __future__ import annotations

import re
from typing import NotRequired, TypedDict

_KIND = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_ROUTE = re.compile(r"(?:/[A-Za-z0-9][A-Za-z0-9_-]*)+")
_MAX_DEVICE_VERSION = 2147483647


class NavigationRef(TypedDict):
    kind: str
    id: str
    artifact_id: NotRequired[str | None]
    path: NotRequired[str]
    object_id: NotRequired[str]
    source_route: NotRequired[str]
    version: NotRequired[int]


def _opaque_id(value: object) -> str | None:
    return value if isinstance(value, str) and _OPAQUE_ID.fullmatch(value) else None


def _route(value: object, *, android: bool = False) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        return None
    path = "/" + value if android and not value.startswith("/") else value
    return value if path == "/" or _ROUTE.fullmatch(path) else None


def _object_id(value: object) -> str | None:
    return _opaque_id(value) or _route(value)


def project_navigation_ref(value: object) -> NavigationRef | None:
    """Copy only routing primitives, even if current detail permission exists."""
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    if not isinstance(kind, str) or not _KIND.fullmatch(kind):
        return None
    legacy_id = _object_id(value.get("object_id"))
    artifact_id = _opaque_id(value.get("artifact_id"))
    object_id = _object_id(value.get("id")) or legacy_id or artifact_id
    if object_id is None:
        return None
    projected: NavigationRef = {"kind": kind, "id": object_id}
    if legacy_id is not None:
        projected["object_id"] = legacy_id
    if artifact_id is not None or ("artifact_id" in value and value["artifact_id"] is None):
        projected["artifact_id"] = artifact_id
    path = _route(value.get("path"))
    if path is not None:
        projected["path"] = path
    source_route = _route(value.get("source_route"), android=True)
    if source_route is not None:
        projected["source_route"] = source_route
    version = value.get("version")
    if type(version) is int and 1 <= version <= _MAX_DEVICE_VERSION:
        projected["version"] = version
    return projected


def project_navigation_refs(value: object) -> list[NavigationRef]:
    if not isinstance(value, list):
        return []
    return [ref for item in value if (ref := project_navigation_ref(item)) is not None]


def project_navigation_update(payload: object, *, thread_id: str) -> dict:
    """Reproject old workspace update payloads without rewriting them or dropping events."""
    value = payload if isinstance(payload, dict) else {}
    projected = {"thread_id": thread_id, "active_object_ref": project_navigation_ref(value.get("active_object_ref"))}
    for key, minimum in (("version", 1), ("open_count", 0)):
        number = value.get(key)
        if type(number) is int and minimum <= number <= _MAX_DEVICE_VERSION:
            projected[key] = number
    device = value.get("device")
    if isinstance(device, str) and 1 <= len(device) <= 32:
        projected["device"] = device
    return projected
