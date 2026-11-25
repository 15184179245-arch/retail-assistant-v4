"""One DSH orchestration snapshot per existing business Task, never a second Task."""
from datetime import datetime
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime


class AssistantWork(Base):
    __tablename__ = 'assistant_work'
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    origin_account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    core_commit: Mapped[str] = mapped_column(String(40), nullable=False)
    ticket_id: Mapped[str] = mapped_column(String(32), nullable=False)
    assignment_id: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_context: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    outbox_event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(['organization_id', 'store_id', 'task_id'],
            ['tasks.organization_id', 'tasks.store_id', 'tasks.id'], name='fk_assistant_work_task', ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id', 'owner_id'],
            ['subjects.organization_id', 'subjects.id'], name='fk_assistant_work_owner', ondelete='RESTRICT'),
        ForeignKeyConstraint(['origin_account_id'], ['employee_accounts.id'],
            name='fk_assistant_work_account', ondelete='RESTRICT'),
        ForeignKeyConstraint(['outbox_event_id'], ['outbox_events.id'],
            name='fk_assistant_work_outbox', ondelete='RESTRICT'),
        UniqueConstraint('organization_id', 'store_id', 'owner_id', 'request_id', name='uq_assistant_work_request'),
        UniqueConstraint('outbox_event_id', name='uq_assistant_work_outbox'),
        CheckConstraint('version > 0', name='ck_assistant_work_version'),
        CheckConstraint('length(request_digest)=64 AND length(snapshot_sha256)=64 AND length(core_commit)=40 '
                        'AND length(ticket_id)=32 AND length(assignment_id)=32', name='ck_assistant_work_bindings'),
    )


class AssistantDispatch(Base):
    """One launch intent per work. Unknown intents are never queued again."""
    __tablename__ = 'assistant_dispatch'
    work_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_version: Mapped[int] = mapped_column(Integer, nullable=False)
    authorization_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    runtime_record: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    machine_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    reconcile_after: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(['work_id'], ['assistant_work.task_id'], name='fk_assistant_dispatch_work', ondelete='RESTRICT'),
        ForeignKeyConstraint(['session_id'], ['auth_sessions.id'], name='fk_assistant_dispatch_session', ondelete='RESTRICT'),
        CheckConstraint('account_version > 0', name='ck_assistant_dispatch_account_version'),
        CheckConstraint("state IN ('pending','launching','settled','denied','unknown')", name='ck_assistant_dispatch_state'),
    )


class AssistantWorkAuthority(Base):
    """No bearer credential: exact-work, exact-run delegated authority record."""
    __tablename__ = 'assistant_work_authorities'
    work_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    origin_session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_version: Mapped[int] = mapped_column(Integer, nullable=False)
    work_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    permissions: Mapped[list] = mapped_column(JSON, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_binding_sha256: Mapped[str | None] = mapped_column(String(64),
        CheckConstraint("workspace_binding_sha256 IS NULL OR length(workspace_binding_sha256) = 64",
                        name="ck_work_authority_workspace_pin"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(['work_id'], ['assistant_work.task_id'], name='fk_work_authority_work', ondelete='RESTRICT'),
        ForeignKeyConstraint(['account_id'], ['employee_accounts.id'], name='fk_work_authority_account', ondelete='RESTRICT'),
        ForeignKeyConstraint(['origin_session_id'], ['auth_sessions.id'], name='fk_work_authority_session', ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id', 'store_id'], ['stores.organization_id', 'stores.id'], name='fk_work_authority_store', ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id', 'owner_id'], ['subjects.organization_id', 'subjects.id'], name='fk_work_authority_owner', ondelete='RESTRICT'),
        CheckConstraint('account_version > 0 AND work_epoch > 0', name='ck_work_authority_versions'),
        CheckConstraint('expires_at > issued_at', name='ck_work_authority_expiry'),
        CheckConstraint('length(request_digest)=64 AND length(snapshot_sha256)=64 AND length(binding_sha256)=64', name='ck_work_authority_hashes'),
    )


class AssistantProgress(Base):
    """Immutable Host fact projection. Never an execution or business approval."""
    __tablename__ = 'assistant_progress'
    work_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    fact: Mapped[dict] = mapped_column(JSON, nullable=False)
    fact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    projection: Mapped[dict] = mapped_column(JSON, nullable=False)
    projection_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(['work_id'], ['assistant_work.task_id'], name='fk_assistant_progress_work', ondelete='RESTRICT'),
        UniqueConstraint('run_id', name='uq_assistant_progress_run'),
        CheckConstraint("state IN ('completed','failed','cancelled','blocked','needs_attention')", name='ck_assistant_progress_state'),
        CheckConstraint('length(fact_sha256)=64 AND length(projection_sha256)=64 AND length(manifest_sha256)=64', name='ck_assistant_progress_hashes'),
    )


class AssistantTurn(Base):
    """A followup on the original Task. Received is not executable dispatch."""
    __tablename__ = 'assistant_turns'

    @property
    def task_id(self):
        # Execution identity for the shared dispatch/authority protocol. The
        # sole business Task is root_work_id, never this identifier.
        return self.id

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    root_work_id: Mapped[str] = mapped_column(String(36), nullable=False)
    previous_turn_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    origin_account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    ticket_id: Mapped[str] = mapped_column(String(32), nullable=False)
    assignment_id: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_fact: Mapped[dict] = mapped_column(JSON, nullable=False)
    parent_fact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    history: Mapped[list] = mapped_column(JSON, nullable=False)
    history_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default='received')
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source_inbox_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_inbox_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(['source_inbox_id'], ['assistant_inbox.id'], name='fk_turn_inbox', ondelete='RESTRICT'),
        Index('uq_turn_inbox', 'source_inbox_id', unique=True),
        CheckConstraint('(source_inbox_id IS NULL AND source_inbox_sha256 IS NULL) OR '
            '(source_inbox_id IS NOT NULL AND source_inbox_sha256 IS NOT NULL AND length(source_inbox_sha256)=64)', name='ck_turn_inbox'),
        ForeignKeyConstraint(['root_work_id'], ['assistant_work.task_id'], name='fk_assistant_turn_root', ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id','store_id','root_work_id'], ['tasks.organization_id','tasks.store_id','tasks.id'], name='fk_assistant_turn_task_scope', ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id','owner_id'], ['subjects.organization_id','subjects.id'], name='fk_assistant_turn_owner', ondelete='RESTRICT'),
        ForeignKeyConstraint(['origin_account_id'], ['employee_accounts.id'], name='fk_assistant_turn_account', ondelete='RESTRICT'),
        UniqueConstraint('root_work_id','sequence',name='uq_assistant_turn_sequence'),
        UniqueConstraint('organization_id','store_id','owner_id','request_id',name='uq_assistant_turn_request'),
        UniqueConstraint('run_id',name='uq_assistant_turn_run'),
        CheckConstraint('sequence > 1',name='ck_assistant_turn_sequence'),
        CheckConstraint("state = 'received'",name='ck_assistant_turn_state'),
        CheckConstraint('length(request_digest)=64 AND length(parent_fact_sha256)=64 AND length(history_sha256)=64 AND length(snapshot_sha256)=64 AND length(manifest_sha256)=64',name='ck_assistant_turn_hashes'),
        CheckConstraint('length(ticket_id)=32 AND length(assignment_id)=32',name='ck_assistant_turn_core_ids'),
    )


class AssistantTurnDispatch(Base):
    """One launch intent per immutable followup turn. Unknown intents are never queued again."""
    __tablename__ = 'assistant_turn_dispatch'
    work_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_version: Mapped[int] = mapped_column(Integer, nullable=False)
    authorization_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    runtime_record: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    machine_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    reconcile_after: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    outbox_event_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    result_record: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(['outbox_event_id'], ['outbox_events.id'], name='fk_turn_dispatch_outbox', ondelete='RESTRICT'),
        ForeignKeyConstraint(['work_id'], ['assistant_turns.id'], name='fk_assistant_turn_dispatch_work', ondelete='RESTRICT'),
        ForeignKeyConstraint(['session_id'], ['auth_sessions.id'], name='fk_assistant_turn_dispatch_session', ondelete='RESTRICT'),
        CheckConstraint('account_version > 0', name='ck_assistant_turn_dispatch_account_version'),
        CheckConstraint("state IN ('pending','launching','settled','denied','unknown')", name='ck_assistant_turn_dispatch_state'),
    )


class AssistantTurnAuthority(Base):
    """No bearer credential: exact-work, exact-run delegated authority record."""
    __tablename__ = 'assistant_turn_authorities'
    work_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    origin_session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_version: Mapped[int] = mapped_column(Integer, nullable=False)
    work_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    permissions: Mapped[list] = mapped_column(JSON, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_binding_sha256: Mapped[str | None] = mapped_column(String(64),
        CheckConstraint("workspace_binding_sha256 IS NULL OR length(workspace_binding_sha256) = 64",
                        name="ck_turn_authority_workspace_pin"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    source_inbox_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_inbox_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(['source_inbox_id'], ['assistant_inbox.id'], name='fk_turn_authority_inbox', ondelete='RESTRICT'),
        Index('uq_turn_authority_inbox', 'source_inbox_id', unique=True),
        CheckConstraint('(source_inbox_id IS NULL AND source_inbox_sha256 IS NULL) OR '
            '(source_inbox_id IS NOT NULL AND source_inbox_sha256 IS NOT NULL AND length(source_inbox_sha256)=64)', name='ck_turn_authority_inbox'),
        ForeignKeyConstraint(['work_id'], ['assistant_turns.id'], name='fk_turn_authority_work', ondelete='RESTRICT'),
        ForeignKeyConstraint(['account_id'], ['employee_accounts.id'], name='fk_turn_authority_account', ondelete='RESTRICT'),
        ForeignKeyConstraint(['origin_session_id'], ['auth_sessions.id'], name='fk_turn_authority_session', ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id', 'store_id'], ['stores.organization_id', 'stores.id'], name='fk_turn_authority_store', ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id', 'owner_id'], ['subjects.organization_id', 'subjects.id'], name='fk_turn_authority_owner', ondelete='RESTRICT'),
        CheckConstraint('account_version > 0 AND work_epoch > 0', name='ck_turn_authority_versions'),
        CheckConstraint('expires_at > issued_at', name='ck_turn_authority_expiry'),
        CheckConstraint('length(request_digest)=64 AND length(snapshot_sha256)=64 AND length(binding_sha256)=64', name='ck_turn_authority_hashes'),
    )



class AssistantInbox(Base):
    """Immutable supplemental instruction and finite consent; not a Pi Run."""
    __tablename__ = 'assistant_inbox'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    root_work_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer(), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    origin_session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    work_epoch: Mapped[int] = mapped_column(Integer(), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text(), nullable=False)
    permissions: Mapped[list] = mapped_column(JSON(), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(['root_work_id'], ['assistant_work.task_id'], name='fk_inbox_root', ondelete='RESTRICT'),
        ForeignKeyConstraint(['previous_message_id'], ['assistant_inbox.id'], name='fk_inbox_previous', ondelete='RESTRICT'),
        ForeignKeyConstraint(['account_id'], ['employee_accounts.id'], name='fk_inbox_account', ondelete='RESTRICT'),
        ForeignKeyConstraint(['origin_session_id'], ['auth_sessions.id'], name='fk_inbox_session', ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id', 'store_id'], ['stores.organization_id', 'stores.id'], name='fk_inbox_store', ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id', 'owner_id'], ['subjects.organization_id', 'subjects.id'], name='fk_inbox_owner', ondelete='RESTRICT'),
        UniqueConstraint('organization_id', 'store_id', 'owner_id', 'request_id', name='uq_inbox_request'),
        UniqueConstraint('root_work_id', 'sequence', name='uq_inbox_sequence'),
        CheckConstraint('sequence > 0 AND account_version > 0 AND work_epoch > 0', name='ck_inbox_versions'),
        CheckConstraint('expires_at > issued_at', name='ck_inbox_expiry'),
        CheckConstraint('length(request_digest)=64 AND length(binding_sha256)=64 AND length(parent_source_sha256)=64', name='ck_inbox_hashes'),
    )
