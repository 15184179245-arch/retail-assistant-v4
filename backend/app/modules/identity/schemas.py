from datetime import datetime

from pydantic import BaseModel, ConfigDict

from backend.app.core.errors import ApiEnvelope


class IdentityContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_id: str
    organization_id: str
    store_id: str
    auth_expires_at: datetime
    capabilities: list[str] = []
    capability_version: str = "v1"


class IdentityContextEnvelope(ApiEnvelope):
    data: IdentityContext | None
