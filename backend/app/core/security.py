from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated
import re

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from backend.app.db.session import get_auth_session


@dataclass(frozen=True)
class AuthenticatedSubject:
    subject_id: str
    organization_id: str
    store_id: str
    auth_expires_at: datetime
    account_id: str | None = None
    session_id: str | None = None
    account_version: int | None = None


def require_bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    if authorization is None:
        raise HTTPException(401, "authentication_required", headers={"WWW-Authenticate": "Bearer"})
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or re.fullmatch(r"sx1_[A-Za-z0-9_-]{43}", parts[1]) is None:
        raise HTTPException(401, "invalid_credentials", headers={"WWW-Authenticate": "Bearer"})
    return parts[1]


def require_authenticated_subject(
    token: Annotated[str, Depends(require_bearer_token)],
    session: Annotated[Session, Depends(get_auth_session)],
) -> AuthenticatedSubject:
    from backend.app.modules.auth.service import authenticate_token
    return authenticate_token(session, token)


def ensure_subject_active(subject: AuthenticatedSubject, now: datetime | None = None) -> None:
    current = now or datetime.now(timezone.utc)
    expires = subject.auth_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= current:
        raise HTTPException(status_code=401, detail="credential_expired")


def require_machine_bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    """Separate bearer parser. Never allow sx2 machine credentials into sx1 employee APIs."""
    if authorization is None:
        raise HTTPException(401, "authentication_required", headers={"WWW-Authenticate": "Bearer"})
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or re.fullmatch(r"sx2_[A-Za-z0-9_-]{43}", parts[1]) is None:
        raise HTTPException(401, "invalid_credentials", headers={"WWW-Authenticate": "Bearer"})
    return parts[1]


def require_authenticated_machine(
    token: Annotated[str, Depends(require_machine_bearer_token)],
    session: Annotated[Session, Depends(get_auth_session)],
):
    """Dedicated internal-machine dependency; not installed on any person route."""
    from backend.app.modules.identity.machine_service import authenticate_machine_token
    return authenticate_machine_token(session, token)
