from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from backend.app.core.errors import ApiEnvelope


def _display_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


SearchDisplayText = Annotated[str | None, BeforeValidator(_display_text)]


class SearchMatchOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str
    external_code: str
    match_field: Literal["code", "barcode", "alias", "name", "derived"]
    match_type: Literal["exact_code", "exact_barcode", "normalized_barcode",
                        "alias_exact", "fuzzy_candidate"]
    original_value: str
    normalized_value: str | None
    name: SearchDisplayText
    spec: SearchDisplayText
    unit: SearchDisplayText
    barcode: str | None
    source_system: str
    source_version: str
    source_updated_at: datetime | None
    captured_at: datetime
    search_evidence: list[dict] = Field(default_factory=list)


class SearchResultOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    exact: list[SearchMatchOut]
    candidates: list[SearchMatchOut]
    ambiguous: bool
    message: str
    exact_total: int = 0
    candidate_total: int = 0
    has_more: bool = False
    scan_truncated: bool = False
    truncated: bool = False


class SearchEnvelope(ApiEnvelope):
    data: SearchResultOut | None
