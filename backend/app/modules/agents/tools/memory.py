"""Shared-owner memory writes; no agent-local database or model-selected identity."""
from hashlib import sha256
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.modules.audit.service import record_event
from backend.app.modules.memory import service


# Bounded credential-format rejection, not a promise to detect every secret.
_CREDENTIAL = re.compile(
    r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|'
    r'\b(?:sk-|st_|sx[12]_|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{16,}|'
    r'\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*|'
    r'(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|密钥|密码|Cookie)\s*[:=：]\s*\S+', re.I)


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @field_validator('content', 'purpose', check_fields=False)
    @classmethod
    def text_is_bounded_reference(cls, value):
        if not value.strip() or '\x00' in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError('memory_content_invalid')
        if _CREDENTIAL.search(value):
            raise ValueError('memory_credential_content_rejected')
        return value.strip()

    @field_validator('memory_id', check_fields=False)
    @classmethod
    def canonical_identity(cls, value):
        if str(UUID(value)) != value:
            raise ValueError('memory_identity_invalid')
        return value


class SaveInput(Input):
    content: str = Field(min_length=1, max_length=5000)
    purpose: str = Field(min_length=1, max_length=200)


class CorrectInput(Input):
    memory_id: str = Field(min_length=36, max_length=36)
    content: str = Field(min_length=1, max_length=5000)
    expected_version: int = Field(ge=1, lt=2**31-1)


class WithdrawInput(Input):
    memory_id: str = Field(min_length=36, max_length=36)
    expected_version: int = Field(ge=1, lt=2**31-1)


class WriteReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    memory_id: str
    version: int
    active: bool
    operation: Literal['save', 'correct', 'withdraw']
    authority: Literal['reference_only'] = 'reference_only'


def execute(session, requester, agent, *, operation, payload, command_key):
    """Called only after registry dual authorization, in its write savepoint."""
    service._require(session, requester, service.READ, service._now())
    if operation == 'save':
        result = service.save_memory(session, requester, content=payload.content,
            purpose=payload.purpose, source='agent:' + agent.subject_id, persist=True,
            retain_until_withdrawn=True, idempotency_key=command_key)
    elif operation == 'correct':
        result = service.correct_memory(session, requester, payload.memory_id,
            content=payload.content, expected_version=payload.expected_version,
            idempotency_key=command_key)
    elif operation == 'withdraw':
        result = service.withdraw_memory(session, requester, payload.memory_id,
            expected_version=payload.expected_version, idempotency_key=command_key)
    else:
        raise service.MemoryError('memory_operation_invalid')
    # Original command metadata stays stable on replay, including after a later
    # correction. Never substitute the current row for the immutable receipt.
    record_event(session, actor_id=requester.subject_id,
        organization_id=requester.organization_id, store_id=requester.store_id,
        action='memory.agent_' + operation, target_type='personal_memory', target_id=result['id'],
        idempotency_key='memory-agent:' + sha256(command_key.encode()).hexdigest(),
        details={'agent_id': agent.subject_id, 'operation': operation,
                 'memory_version': result['version'], 'operation_key_sha256': sha256(command_key.encode()).hexdigest()})
    return WriteReceipt(memory_id=result['id'], version=result['version'], active=result['active'], operation=operation)
