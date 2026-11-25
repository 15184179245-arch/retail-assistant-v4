"""FIN-001 workpaper contracts. This first adapter accepts synthetic input only."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from backend.app.core.errors import ApiEnvelope

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class SourceEntry(Strict):
    source_id: str = Field(min_length=1, max_length=120)
    source_version: str = Field(min_length=1, max_length=80)
    date: str = Field(min_length=10, max_length=10)
    evidence_locator: str = Field(min_length=1, max_length=200)
    direction: Literal["debit", "credit"]
    amount: str | None = Field(default=None, max_length=32)

class WorkpaperCreate(Strict):
    workpaper_key: str = Field(min_length=1, max_length=120)
    expected_version: int = Field(ge=0)
    evidence_id: str = Field(min_length=1, max_length=36)
    evidence_sha256: str = Field(min_length=64, max_length=64)
    source_kind: Literal["synthetic"]
    source_snapshot_id: str = Field(min_length=1, max_length=120)
    source_version: str = Field(min_length=1, max_length=80)
    observed_at: str = Field(min_length=20, max_length=40)
    period_start: str = Field(min_length=10, max_length=10)
    period_end: str = Field(min_length=10, max_length=10)
    party_type: Literal["customer", "supplier"]
    party_ref: str = Field(min_length=1, max_length=120)
    currency: Literal["CNY", "USD"]
    opening_balance: str | None = Field(default=None, max_length=32)
    closing_balance: str | None = Field(default=None, max_length=32)
    complete: bool
    balance_convention: Literal["debit_increases", "credit_increases"] | None = None
    entries: list[SourceEntry] = Field(max_length=500)

class WorkpaperEnvelope(ApiEnvelope):
    data: dict | None = None
