from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from backend.app.core.errors import ApiEnvelope


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=64)
    password: SecretStr = Field(min_length=1, max_length=128)


class LoginData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    token_type: Literal["Bearer"]
    expires_at: datetime
    subject_id: str
    organization_id: str
    store_id: str


class LoginEnvelope(ApiEnvelope):
    data: LoginData | None


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=12, max_length=128)
