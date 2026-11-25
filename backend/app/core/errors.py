from typing import Any

from pydantic import BaseModel, ConfigDict


class ApiEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    data: Any | None
    code: str
    error: str | None
    user_message: str
    hint: str | None
