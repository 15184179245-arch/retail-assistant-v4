from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.core.errors import ApiEnvelope


class MemoryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    content: str
    purpose: str
    source: str
    expires_at: datetime | None
    retain_until_withdrawn: bool = False
    version: int
    active: bool
    created_at: datetime
    updated_at: datetime


class MemoryPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[MemoryOut]


class MemoryEnvelope(ApiEnvelope):
    data: MemoryOut | None


class MemoryListEnvelope(ApiEnvelope):
    data: MemoryPage | None


class MemorySearchPage(MemoryPage):
    match_mode: Literal['all_terms', 'any_terms', 'none']
    authority: Literal['reference_only']


class MemorySearchEnvelope(ApiEnvelope):
    data: MemorySearchPage | None


class MemoryNoteImport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    markdown: str = Field(min_length=1, max_length=32768)


class MemoryNote(BaseModel):
    model_config = ConfigDict(extra='forbid')
    filename: str
    markdown: str
    memory_id: str
    version: int
    authority: Literal['reference_only']


class MemoryNoteEnvelope(ApiEnvelope):
    data: MemoryNote | None


class MemoryCorrect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str
    expected_version: int
    purpose: str | None = None
    expires_at: str | None = None


class MemoryCommandStatus(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['save', 'correct', 'withdraw']
    key: str
    status: Literal['unobserved', 'committed']
    memory_id: str | None
    current_version: int | None


class MemoryCommandStatusEnvelope(ApiEnvelope):
    data: MemoryCommandStatus | None
