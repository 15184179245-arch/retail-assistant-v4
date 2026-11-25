from typing import Annotated

from fastapi import Header, HTTPException


def require_idempotency_key(value: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> str:
    """Dependency hook for future formal write routes; no write route is enabled yet."""
    if value is None or not value.strip():
        raise HTTPException(status_code=400, detail="idempotency_key_required")
    if len(value) > 200:
        raise HTTPException(status_code=400, detail="idempotency_key_invalid")
    return value.strip()
