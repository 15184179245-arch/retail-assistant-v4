from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from backend.app.core.errors import ApiEnvelope
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject, require_bearer_token
from backend.app.db.session import get_session
from backend.app.modules.auth.schemas import LoginData, LoginEnvelope, LoginRequest, PasswordChangeRequest
from backend.app.modules.auth.service import change_password, login, logout, logout_all

router = APIRouter(prefix="/auth", tags=["auth"])


def _success(response: Response, message: str) -> ApiEnvelope:
    response.headers["Cache-Control"] = "no-store"
    return ApiEnvelope(success=True, data=None, code="OK", error=None, user_message=message, hint=None)


@router.post("/login", response_model=LoginEnvelope)
def login_route(payload: LoginRequest, response: Response,
                session: Annotated[Session, Depends(get_session)]) -> LoginEnvelope:
    with session.begin():
        result = login(session, username=payload.username, password=payload.password.get_secret_value())
    if result is None:
        raise HTTPException(401, "invalid_credentials", headers={"WWW-Authenticate": "Bearer"})
    response.headers["Cache-Control"] = "no-store"
    return LoginEnvelope(success=True, data=LoginData(access_token=result.access_token,
        token_type="Bearer", expires_at=result.session.expires_at,
        subject_id=result.account.subject_id, organization_id=result.account.organization_id,
        store_id=result.account.store_id), code="OK", error=None,
        user_message="已登录。", hint=None)


@router.post("/logout", response_model=ApiEnvelope)
def logout_route(response: Response, token: Annotated[str, Depends(require_bearer_token)],
                 session: Annotated[Session, Depends(get_session)]) -> ApiEnvelope:
    with session.begin():
        logout(session, token)
    return _success(response, "已退出登录。")


@router.post("/logout-all", response_model=ApiEnvelope)
def logout_all_route(response: Response, actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                     session: Annotated[Session, Depends(get_session)]) -> ApiEnvelope:
    with session.begin():
        logout_all(session, actor)
    return _success(response, "所有登录会话已退出。")


@router.post("/password", response_model=ApiEnvelope)
def password_route(payload: PasswordChangeRequest, response: Response,
                   actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                   session: Annotated[Session, Depends(get_session)]) -> ApiEnvelope:
    with session.begin():
        changed = change_password(session, actor, current_password=payload.current_password.get_secret_value(),
                                  new_password=payload.new_password.get_secret_value())
    if not changed:
        raise HTTPException(401, "invalid_credentials", headers={"WWW-Authenticate": "Bearer"})
    return _success(response, "密码已更新，请重新登录。")
