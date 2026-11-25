"""Versioned password hashes; no plaintext or truncation is persisted."""

import base64
import hashlib
import hmac
import re
import secrets

ITERATIONS = 600_000
_SCHEME = "pbkdf2_sha256"


def normalize_username(username: str) -> str:
    value = username.strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", value, flags=re.ASCII) is None:
        raise ValueError("invalid username")
    return value


def validate_password(password: str) -> None:
    if not 12 <= len(password) <= 128:
        raise ValueError("password must contain 12 to 128 characters")
    try:
        password.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("password must contain valid Unicode") from exc


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS, dklen=32)
    return f"{_SCHEME}${ITERATIONS}${_encode(salt)}${_encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or not isinstance(encoded, str) or len(password) > 128:
        return False
    try:
        scheme, iterations, salt_text, expected_text = encoded.split("$")
        if scheme != _SCHEME or iterations != str(ITERATIONS):
            return False
        salt = base64.b64decode(salt_text, validate=True)
        expected = base64.b64decode(expected_text, validate=True)
        if len(salt) != 16 or len(expected) != 32:
            return False
    except (ValueError, TypeError):
        return False
    try:
        encoded_password = password.encode("utf-8")
    except UnicodeEncodeError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", encoded_password, salt, ITERATIONS, dklen=32)
    return hmac.compare_digest(actual, expected)


def dummy_verify(password: str) -> None:
    # Equal KDF work for nonexistent accounts; this is not an account password.
    verify_password(password, f"{_SCHEME}${ITERATIONS}${_encode(bytes(16))}${_encode(bytes(32))}")
