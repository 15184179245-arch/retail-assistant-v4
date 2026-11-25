"""Local immutable blob store; never stores secrets or mutable paths."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import os
import stat
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


def read_verified(*, organization_id: str, store_id: str, key: str, sha256: str,
                  byte_size: int, content_type: str) -> bytes:
    """Read one bounded immutable snapshot below the configured media root."""
    missing = "media_content_missing"
    scope = (organization_id, store_id)
    if (any(type(part) is not str or part in ("", ".", "..") or
            any(char in part for char in ("/", "\\", "\x00")) for part in scope)
            or type(sha256) is not str or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)
            or type(byte_size) is not int or not 1 <= byte_size <= MAX_BYTES
            or key != storage_key(*scope, sha256)):
        raise MediaStorageError(missing)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        with ExitStack() as descriptors:
            directory = os.open(media_root(), directory_flags)
            descriptors.callback(os.close, directory)
            for part in (*scope, sha256[:2]):
                directory = os.open(part, directory_flags, dir_fd=directory)
                descriptors.callback(os.close, directory)
            fd = os.open(sha256 + ".bin", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
            descriptors.callback(os.close, fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != byte_size:
                raise MediaStorageError(missing)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(byte_size + 1)
        if len(data) != byte_size or digest(data) != sha256 or detect_content_type(data) != content_type:
            raise MediaStorageError(missing)
    except (OSError, MediaStorageError):
        raise MediaStorageError(missing) from None
    return data
