"""Local immutable blob store; never stores secrets or mutable paths."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
SIGNATURES = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),
}


class MediaStorageError(ValueError):
    pass


def media_root() -> Path:
    return Path(os.environ.get("MEDIA_ROOT", "data-local/media")).resolve()


def detect_content_type(data: bytes) -> str:
    if not data:
        raise MediaStorageError("media_empty")
    if len(data) > MAX_BYTES:
        raise MediaStorageError("media_too_large")
    for content_type, signatures in SIGNATURES.items():
        if content_type == "image/webp":
            if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                return content_type
            continue
        if any(data.startswith(signature) for signature in signatures):
            return content_type
    raise MediaStorageError("media_type_unsupported")


def storage_key(organization_id: str, store_id: str, sha256: str) -> str:
    return f"{organization_id}/{store_id}/{sha256[:2]}/{sha256}.bin"


def write_immutable(data: bytes, key: str) -> Path:
    root = media_root()
    target = (root / key).resolve()
    if root not in target.parents:
        raise MediaStorageError("media_path_escape")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return target
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return target


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
