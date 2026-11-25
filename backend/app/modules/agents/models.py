from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from backend.app.db.base import Base
from backend.app.db.types import UTCDateTime as _UTCDateTime


class UTCDateTime(_UTCDateTime):
    """Store aware timestamps as UTC; normalize SQLite's naive result values."""

    cache_ok = True


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_key: Mapped[str] = mapped_column(String(120), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(120), nullable=False)
    toolset_version: Mapped[str] = mapped_column(String(120), nullable=False)
    input_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="created")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_model_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_agent_runs_scope_id"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_agent_runs_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "requester_id"], ["subjects.organization_id", "subjects.id"], name="fk_agent_runs_requester_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "task_id"], ["tasks.organization_id", "tasks.store_id", "tasks.id"], name="fk_agent_runs_task_scope", ondelete="RESTRICT"),
        CheckConstraint("state IN ('created', 'running', 'waiting_for_human', 'waiting_for_external', 'completed', 'failed', 'cancelled')", name="ck_agent_runs_state"),
        CheckConstraint("version > 0", name="ck_agent_runs_version_positive"),
        CheckConstraint("max_input_tokens > 0 AND max_output_tokens > 0 AND max_model_calls > 0 AND max_tool_calls > 0 AND max_steps > 0", name="ck_agent_runs_budgets_positive"),
        CheckConstraint("deadline_at > created_at", name="ck_agent_runs_deadline_after_created"),
        CheckConstraint("updated_at >= created_at", name="ck_agent_runs_updated_after_created"),
        CheckConstraint("length(agent_key) > 0 AND length(agent_version) > 0 AND length(provider_key) > 0 AND length(model) > 0 AND length(prompt_version) > 0 AND length(toolset_version) > 0", name="ck_agent_runs_metadata_nonempty"),
        Index("ix_agent_runs_scope", "organization_id", "store_id", "created_at"),
        Index("ix_agent_runs_state", "organization_id", "store_id", "state", "created_at"),
    )


class RunSkillBinding(Base):
    """One immutable, scoped SKL-003 skill pin per Run.

    Absence means the original ordinary Agent path. Skill release/rollback
    cannot change a previously chosen version, template or tool ceiling.
    """
    __tablename__ = "agent_run_skill_bindings"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(36), nullable=False)
    skill_key: Mapped[str] = mapped_column(String(120), nullable=False)
    release_tag: Mapped[str] = mapped_column(String(120), nullable=False)
    spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    skill_version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_key: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(120), nullable=False)
    toolset_version: Mapped[str] = mapped_column(String(120), nullable=False)
    role_key: Mapped[str] = mapped_column(String(40), nullable=False)
    specialty: Mapped[str] = mapped_column(String(40), nullable=False)
    role_spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_contract_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_tools: Mapped[list] = mapped_column(JSON, nullable=False)
    selection_reason: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "run_id"],
                             ["agent_runs.organization_id", "agent_runs.store_id",
                              "agent_runs.id"], name="fk_skill_run_binding_run",
                             ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "skill_id"],
                             ["agent_skill_versions.organization_id",
                              "agent_skill_versions.store_id", "agent_skill_versions.id"],
                             name="fk_skill_run_binding_skill", ondelete="RESTRICT"),
        CheckConstraint("skill_version > 0", name="ck_skill_run_binding_version"),
        CheckConstraint("length(spec_sha256) = 64 AND length(evaluation_digest) = 64 "
                        "AND length(role_spec_sha256) = 64 AND length(tool_contract_sha256) = 64",
                        name="ck_skill_run_binding_sha"),
        CheckConstraint("role_key = 'specialist' AND "
                        "specialty IN ('catalog','knowledge','tasks')",
                        name="ck_skill_run_binding_role"),
    )


class AgentExecution(Base):
    """Durable worker control. Absent row means legacy inline_v1, never claimable.

    This table does not confer business authorization: worker dispatch additionally
    needs a real employee grant and the AGT-020 security-fence rollout gate.
    """
    __tablename__ = "agent_run_executions"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="worker_v2")
    phase: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    owner_machine_id: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account_version: Mapped[int] = mapped_column(Integer, nullable=False)
    authorization_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    # AGT-026 B1 only reserves these columns. A NULL authorization_epoch is a
    # legacy execution row that never recorded a scope epoch: it must not be
    # backfilled or treated as an original authorization, and the later worker
    # revalidation has to fail shut on it. approval_grant is reserved and is
    # never consumed or trusted by B1.
    authorization_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approval_grant: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    origin_message_id: Mapped[str] = mapped_column(String(36), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    frozen_context: Mapped[dict] = mapped_column(JSON, nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_requested_by: Mapped[str | None] = mapped_column(String(36))
    cancel_resolution: Mapped[str] = mapped_column(String(16), nullable=False, default="not_requested")
    effect_status: Mapped[str] = mapped_column(String(24), nullable=False, default="none")
    dispatch_state: Mapped[str] = mapped_column(String(16), nullable=False, default="not_dispatched")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "run_id"],
                             ["agent_runs.organization_id", "agent_runs.store_id", "agent_runs.id"],
                             name="fk_agent_execution_run_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "owner_machine_id"],
                             ["machine_identities.organization_id", "machine_identities.store_id",
                              "machine_identities.id"], name="fk_agent_execution_machine_scope",
                             ondelete="RESTRICT"),
        ForeignKeyConstraint(["session_id"], ["auth_sessions.id"],
                             name="fk_agent_execution_session", ondelete="RESTRICT"),
        ForeignKeyConstraint(["account_id"], ["employee_accounts.id"],
                             name="fk_agent_execution_account", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "origin_message_id"],
                             ["agent_messages.organization_id", "agent_messages.store_id",
                              "agent_messages.id"], name="fk_agent_execution_origin_scope",
                             ondelete="RESTRICT"),
        CheckConstraint("mode = 'worker_v2'", name="ck_agent_execution_mode"),
        CheckConstraint("phase IN ('queued','claimed','unknown','finished','cancelled')",
                        name="ck_agent_execution_phase"),
        CheckConstraint("version > 0 AND epoch >= 0 AND account_version > 0",
                        name="ck_agent_execution_versions"),
        CheckConstraint("authorization_epoch IS NULL OR authorization_epoch > 0",
                        name="ck_agent_execution_authorization_epoch"),
        CheckConstraint("(phase = 'claimed' AND owner_machine_id IS NOT NULL AND lease_until IS NOT NULL) "
                        "OR (phase != 'claimed' AND owner_machine_id IS NULL AND lease_until IS NULL)",
                        name="ck_agent_execution_lease_pair"),
        CheckConstraint("cancel_resolution IN ('not_requested','requested','cancelled','too_late','unknown')",
                        name="ck_agent_execution_cancel_resolution"),
        CheckConstraint("effect_status IN ('none','local_task_committed','external_unknown')",
                        name="ck_agent_execution_effect_status"),
        CheckConstraint("dispatch_state IN ('not_dispatched','dispatched','unknown','settled')",
                        name="ck_agent_execution_dispatch"),
        CheckConstraint("length(input_digest) = 64", name="ck_agent_execution_digest"),
        Index("ix_agent_execution_claim", "organization_id", "store_id",
              "phase", "lease_until", "created_at"),
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    output_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "run_id"], ["agent_runs.organization_id", "agent_runs.store_id", "agent_runs.id"], name="fk_agent_steps_run_scope", ondelete="RESTRICT"),
        UniqueConstraint("organization_id", "store_id", "run_id", "sequence_no", name="uq_agent_steps_run_sequence"),
        UniqueConstraint("organization_id", "store_id", "run_id", "id", "kind", name="uq_agent_steps_run_id_kind"),
        CheckConstraint("kind IN ('model', 'tool', 'checkpoint')", name="ck_agent_steps_kind"),
        CheckConstraint("state IN ('pending', 'running', 'succeeded', 'failed', 'unknown', 'cancelled')", name="ck_agent_steps_state"),
        CheckConstraint("sequence_no > 0 AND version > 0", name="ck_agent_steps_sequence_version_positive"),
        CheckConstraint("(input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR output_tokens >= 0)", name="ck_agent_steps_usage_nonnegative"),
        CheckConstraint("kind = 'model' OR (input_tokens IS NULL AND output_tokens IS NULL)", name="ck_agent_steps_nonmodel_usage_null"),
        CheckConstraint("updated_at >= created_at", name="ck_agent_steps_updated_after_created"),
        Index("ix_agent_steps_run", "organization_id", "store_id", "run_id", "sequence_no"),
        Index("uq_agent_steps_confirmation_scope", "organization_id", "store_id", "run_id", "id", unique=True),
    )


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="tool", server_default="tool")
    tool_key: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "run_id", "step_id", "step_kind"], ["agent_steps.organization_id", "agent_steps.store_id", "agent_steps.run_id", "agent_steps.id", "agent_steps.kind"], name="fk_agent_tool_calls_tool_step", ondelete="RESTRICT"),
        UniqueConstraint("organization_id", "store_id", "step_id", name="uq_agent_tool_calls_step"),
        UniqueConstraint("organization_id", "store_id", "run_id", "idempotency_key", name="uq_agent_tool_calls_run_idempotency"),
        CheckConstraint("step_kind = 'tool'", name="ck_agent_tool_calls_step_kind_tool"),
        CheckConstraint("length(tool_key) > 0 AND length(tool_version) > 0 AND length(idempotency_key) > 0", name="ck_agent_tool_calls_metadata_nonempty"),
        CheckConstraint("length(input_digest) = 64", name="ck_agent_tool_calls_digest_sha256"),
    )


class AgentPiToolReceipt(Base):
    """Recovery-A facts, not a transcript, checkpoint or replay permission.

    The raw native ID is retained when its model result commits. Later writes
    bind the original tool Step and result in their respective Host roots.
    Kind-qualified foreign keys prevent a model Step being used as a tool Step.
    """

    __tablename__ = "agent_pi_tool_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    model_step_id: Mapped[str] = mapped_column(String(36), nullable=False)
    model_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="model")
    native_call_id: Mapped[str] = mapped_column(String(100), nullable=False)
    pi_name: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_key: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(120), nullable=False)
    toolset_version: Mapped[str] = mapped_column(String(120), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tool_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="tool")
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="accepted")
    output_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "store_id", "run_id"],
            ["agent_runs.organization_id", "agent_runs.store_id", "agent_runs.id"],
            name="fk_pi_receipt_run", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["organization_id", "store_id", "run_id", "model_step_id", "model_kind"],
            ["agent_steps.organization_id", "agent_steps.store_id", "agent_steps.run_id",
             "agent_steps.id", "agent_steps.kind"],
            name="fk_pi_receipt_model", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["organization_id", "store_id", "run_id", "tool_step_id", "tool_kind"],
            ["agent_steps.organization_id", "agent_steps.store_id", "agent_steps.run_id",
             "agent_steps.id", "agent_steps.kind"],
            name="fk_pi_receipt_tool", ondelete="RESTRICT"),
        UniqueConstraint("organization_id", "store_id", "run_id", "native_call_id",
                         name="uq_pi_receipt_native_call"),
        UniqueConstraint("organization_id", "store_id", "run_id", "tool_step_id",
                         name="uq_pi_receipt_tool_step"),
        CheckConstraint("contract_version = 1 AND model_kind = 'model' AND tool_kind = 'tool'",
                        name="ck_pi_receipt_contract_kinds"),
        CheckConstraint("length(native_call_id) BETWEEN 1 AND 100 AND length(pi_name) > 0 "
                        "AND length(tool_key) > 0 AND length(tool_version) > 0 "
                        "AND length(toolset_version) > 0 AND length(input_digest) = 64",
                        name="ck_pi_receipt_identity"),
        CheckConstraint("(state = 'accepted' AND tool_step_id IS NULL AND output_ref IS NULL AND error_code IS NULL) "
                        "OR (state = 'dispatch_intent' AND tool_step_id IS NOT NULL AND output_ref IS NULL AND error_code IS NULL) "
                        "OR (state = 'succeeded' AND tool_step_id IS NOT NULL AND output_ref IS NOT NULL AND error_code IS NULL) "
                        "OR (state IN ('failed', 'unknown') AND output_ref IS NULL AND error_code IS NOT NULL)",
                        name="ck_pi_receipt_state"),
        CheckConstraint("updated_at >= created_at", name="ck_pi_receipt_times"),
        Index("ix_pi_receipt_run", "organization_id", "store_id", "run_id", "created_at"),
    )


class AgentPiToolResult(Base):
    """Immutable canonical facts, not an execution or current disclosure grant.

    The two receipt FKs deliberately do not certify that ID and scoped native
    key identify the same row. The B writer/reader must re-prove that binding.
    """
    __tablename__ = 'agent_pi_tool_results'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    record_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_abi: Mapped[str] = mapped_column(String(32), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    model_step_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tool_step_id: Mapped[str] = mapped_column(String(36), nullable=False)
    model_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    tool_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    pi_receipt_id: Mapped[str] = mapped_column(String(36), nullable=False)
    native_call_id: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_key: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(120), nullable=False)
    toolset_version: Mapped[str] = mapped_column(String(200), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_record_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    output_ref: Mapped[str] = mapped_column(String(71), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_refs_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_ref: Mapped[str] = mapped_column(String(71), nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(['organization_id','store_id','run_id'],['agent_runs.organization_id','agent_runs.store_id','agent_runs.id'],name='fk_pi_result_run_scope',ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id','store_id','run_id','native_call_id'],['agent_pi_tool_receipts.organization_id','agent_pi_tool_receipts.store_id','agent_pi_tool_receipts.run_id','agent_pi_tool_receipts.native_call_id'],name='fk_pi_result_native_scope',ondelete='RESTRICT'),
        ForeignKeyConstraint(['pi_receipt_id'],['agent_pi_tool_receipts.id'],name='fk_pi_result_receipt_id',ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id','store_id','run_id','model_step_id','model_kind'],['agent_steps.organization_id','agent_steps.store_id','agent_steps.run_id','agent_steps.id','agent_steps.kind'],name='fk_pi_result_model_step',ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id','store_id','run_id','tool_step_id','tool_kind'],['agent_steps.organization_id','agent_steps.store_id','agent_steps.run_id','agent_steps.id','agent_steps.kind'],name='fk_pi_result_tool_step',ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id','requester_id'],['subjects.organization_id','subjects.id'],name='fk_pi_result_requester',ondelete='RESTRICT'),
        ForeignKeyConstraint(['organization_id','agent_id'],['subjects.organization_id','subjects.id'],name='fk_pi_result_agent',ondelete='RESTRICT'),
        UniqueConstraint('organization_id','store_id','run_id','native_call_id',name='uq_pi_result_native'),
        UniqueConstraint('organization_id','store_id','run_id','tool_step_id',name='uq_pi_result_tool_step'),
        UniqueConstraint('pi_receipt_id',name='uq_pi_result_receipt'),
        CheckConstraint("record_version = 1 AND result_abi = 'pi-tool-result/1'",name='ck_pi_result_contract'),
        CheckConstraint("model_kind = 'model' AND tool_kind = 'tool' AND execution_epoch > 0",name='ck_pi_result_identity'),
        CheckConstraint("result_kind IN ('success','tool_error') AND (result_kind <> 'tool_error' OR tool_key IN ('memory.search','knowledge.search'))",name='ck_pi_result_kind'),
        CheckConstraint("tool_version = '1' AND ((tool_key = 'tasks.list_mine' AND tool_name = 'v4_tasks_list_mine_v1') OR (tool_key = 'knowledge.search' AND tool_name = 'v4_knowledge_search_v1') OR (tool_key = 'catalog.search' AND tool_name = 'v4_catalog_search_v1') OR (tool_key = 'memory.search' AND tool_name = 'v4_memory_search_v1'))",name='ck_pi_result_tool'),
        CheckConstraint("length(input_digest) = 64 AND length(runtime_record_sha256) = 64 AND length(payload_sha256) = 64 AND length(source_refs_sha256) = 64 AND length(toolset_version) = 71 AND substr(toolset_version,1,7) = 'sha256:' AND output_ref = 'sha256:' || payload_sha256 AND length(canonical_ref) = 71 AND substr(canonical_ref,1,7) = 'sha256:'",name='ck_pi_result_digests'),
        CheckConstraint('length(native_call_id) BETWEEN 1 AND 100 AND length(canonical_json) BETWEEN 1 AND 262144',name='ck_pi_result_body'),
        Index('ix_pi_result_run','organization_id','store_id','run_id','created_at'),
    )


class PiUsagePresence(TypeDecorator):
    """Only original-field presence; never a place to persist provider data."""
    impl = JSON(none_as_null=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and (type(value) is not dict or set(value) != {
                'prompt_tokens', 'completion_tokens', 'cached_tokens', 'cache_write_tokens'}
                or any(type(item) is not bool for item in value.values())):
            raise ValueError('pi_usage_presence_invalid')
        return value


class PiGoUsageBasis(TypeDecorator):
    """Whitelisted Host-only accounting evidence; never body or credentials."""
    impl = JSON(none_as_null=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            from .pi_online_response import validate_go_usage_basis
            try:
                validate_go_usage_basis(value)
            except Exception:
                raise ValueError('pi_go_usage_basis_invalid') from None
        return value


def _pi_message_digest_check():
    remaining = 'message_digest'
    for digit in '0123456789abcdef':
        remaining = f"replace({remaining}, '{digit}', '')"
    return f"message_digest IS NULL OR (length(message_digest) = 64 AND {remaining} = '')"


class AgentPiProviderReceipt(Base):
    """One durable physical-send eligibility, never a retry/recovery grant.

    Bodies, prompts, credentials and upstream error strings are never stored.
    A committed send_intent with no result is itself an ambiguous call fact.
    """
    __tablename__ = "agent_pi_provider_receipts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    model_step_id: Mapped[str] = mapped_column(String(36), nullable=False)
    model_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="model")
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    permit_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    toolset_version: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    wire_protocol: Mapped[str] = mapped_column(String(40), nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    send_started: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    usage_presence: Mapped[dict | None] = mapped_column(PiUsagePresence(), nullable=True)
    usage_basis: Mapped[dict | None] = mapped_column(PiGoUsageBasis(), nullable=True)
    usage_input: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    usage_output: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_read: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_write: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["organization_id","store_id","run_id","model_step_id","model_kind"],
            ["agent_steps.organization_id","agent_steps.store_id","agent_steps.run_id","agent_steps.id","agent_steps.kind"],
            name="fk_pi_provider_model", ondelete="RESTRICT"),
        UniqueConstraint("organization_id","store_id","run_id","model_step_id", name="uq_pi_provider_step"),
        UniqueConstraint("permit_digest", name="uq_pi_provider_permit"),
        UniqueConstraint("organization_id","store_id","run_id","request_id", name="uq_pi_provider_request"),
        CheckConstraint("model_kind = 'model' AND epoch > 0 AND model = 'deepseek-v4.1-flash' "
            "AND wire_protocol IN ('v4.pi-readonly/2', 'v4.pi-memory/2', 'v4.pi-discovery/2', 'v4.pi-source/2', 'v4.pi-readonly/3', 'v4.pi-workbench/2')", name="ck_pi_provider_identity"),
        CheckConstraint("length(permit_digest) = 64 AND length(input_digest) = 64 AND length(request_digest) = 64 "
            "AND length(manifest_digest) = 64 AND length(request_id) > 0 AND length(toolset_version) > 0 "
            "AND (response_digest IS NULL OR length(response_digest) = 64)", name="ck_pi_provider_digests"),
        CheckConstraint("(state IN ('permitted','send_intent') AND response_digest IS NULL AND error_code IS NULL "
            "AND usage_input IS NULL AND usage_output IS NULL AND cache_read IS NULL AND cache_write IS NULL) "
            "OR (state = 'responded' AND response_digest IS NOT NULL AND error_code IS NULL AND send_started = true "
            "AND usage_input IS NOT NULL AND usage_output IS NOT NULL AND cache_read IS NOT NULL AND cache_write = 0) "
            "OR (state IN ('unknown','not_sent') AND error_code IS NOT NULL)", name="ck_pi_provider_state"),
        CheckConstraint("(usage_input IS NULL OR usage_input >= 0) AND (usage_output IS NULL OR usage_output >= 0) "
            "AND (cache_read IS NULL OR cache_read >= 0) AND (cache_write IS NULL OR cache_write >= 0)", name="ck_pi_provider_usage"),
        CheckConstraint(_pi_message_digest_check(),
                        name="ck_pi_provider_message_digest"),
        CheckConstraint("updated_at >= created_at", name="ck_pi_provider_times"),
    )

    @property
    def usage(self):
        if self.usage_input is None:
            return None
        return {"input": self.usage_input, "output": self.usage_output,
                "cacheRead": self.cache_read, "cacheWrite": self.cache_write}


class AgentConfirmation(Base):
    __tablename__ = "agent_confirmations"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_id: Mapped[str] = mapped_column(String(36), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tool_key: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(120), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    decision_reason: Mapped[str | None] = mapped_column(String(500))
    result_task_id: Mapped[str | None] = mapped_column(String(36))

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "run_id"],
                             ["agent_runs.organization_id", "agent_runs.store_id", "agent_runs.id"],
                             name="fk_agent_confirmations_run_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "run_id", "step_id"],
                             ["agent_steps.organization_id", "agent_steps.store_id", "agent_steps.run_id", "agent_steps.id"],
                             name="fk_agent_confirmations_step_scope", ondelete="RESTRICT"),
        CheckConstraint("state IN ('pending', 'executing', 'approved', 'rejected', 'expired', 'failed', 'unknown')",
                        name="ck_agent_confirmations_state"),
        CheckConstraint("version > 0 AND length(input_digest) = 64", name="ck_agent_confirmations_version_digest"),
        CheckConstraint("expires_at > created_at", name="ck_agent_confirmations_expiry"),
        Index("ix_agent_confirmations_scope_state", "organization_id", "store_id", "state", "expires_at"),
    )


class AgentConversation(Base):
    __tablename__ = "agent_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    summary_text: Mapped[str | None] = mapped_column(String(2000))
    summary_source: Mapped[str | None] = mapped_column(String(120))
    summary_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_agent_conversations_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "subject_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_agent_conversations_subject_scope", ondelete="RESTRICT"),
        UniqueConstraint("organization_id", "store_id", "id",
                         name="uq_agent_conversations_scope_id"),
        CheckConstraint("status IN ('active', 'closed')", name="ck_agent_conversations_status"),
        CheckConstraint("summary_version >= 0 AND version > 0",
                        name="ck_agent_conversations_versions"),
        Index("ix_agent_conversations_subject", "organization_id", "store_id",
              "subject_id", "status"),
    )


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # 0063 upgrades PostgreSQL to TEXT; SQLite deliberately retains VARCHAR affinity.
    content: Mapped[str] = mapped_column(Text().with_variant(String(4000), "sqlite"), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    source_ref: Mapped[str | None] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "conversation_id"],
                             ["agent_conversations.organization_id", "agent_conversations.store_id",
                              "agent_conversations.id"],
                             name="fk_agent_messages_conversation_scope", ondelete="RESTRICT"),
        UniqueConstraint("organization_id", "store_id", "id", name="uq_agent_messages_scope_id"),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_agent_messages_role"),
        CheckConstraint("state IN ('active', 'revoked')", name="ck_agent_messages_state"),
        CheckConstraint("version > 0 AND length(content) > 0", name="ck_agent_messages_content"),
        Index("ix_agent_messages_conversation", "organization_id", "store_id",
              "conversation_id", "created_at"),
    )


class AgentAnswer(Base):
    """One final-answer receipt; text remains in the existing personal message."""
    __tablename__ = "agent_answers"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    message_id: Mapped[str] = mapped_column(String(36), nullable=False)
    evidence: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    own_evidence: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "run_id"],
                             ["agent_runs.organization_id", "agent_runs.store_id", "agent_runs.id"],
                             name="fk_agent_answers_run_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "message_id"],
                             ["agent_messages.organization_id", "agent_messages.store_id", "agent_messages.id"],
                             name="fk_agent_answers_message_scope", ondelete="RESTRICT"),
        UniqueConstraint("message_id", name="uq_agent_answers_message"),
    )


class BudgetPolicy(Base):
    """Explicit, scoped monetary policy for one price version.

    Amounts are stored as strict decimal strings so SQLite and PostgreSQL keep
    the same exact value. Either ceiling may explicitly be ``unlimited``;
    this disables only that monetary ceiling, never usage accounting.
    ``version`` also serializes concurrent reservations:
    a reserving transaction conditionally bumps it before recounting spend.
    """
    __tablename__ = "agent_budget_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider_key: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    price_version: Mapped[str] = mapped_column(String(120), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    limit_amount: Mapped[str] = mapped_column(String(40), nullable=False)
    per_call_limit: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    effective_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_agent_budget_policies_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "subject_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_agent_budget_policies_subject_scope", ondelete="RESTRICT"),
        UniqueConstraint("organization_id", "store_id", "id",
                         name="uq_agent_budget_policies_scope_id"),
        Index("uq_agent_budget_policies_employee_window", "organization_id", "store_id",
              "subject_id", "effective_from", unique=True,
              sqlite_where=text("scope_type = 'employee'"),
              postgresql_where=text("scope_type = 'employee'")),
        Index("uq_agent_budget_policies_store_window", "organization_id", "store_id",
              "effective_from", unique=True,
              sqlite_where=text("scope_type = 'store'"),
              postgresql_where=text("scope_type = 'store'")),
        CheckConstraint("scope_type IN ('employee', 'store')",
                        name="ck_agent_budget_policies_scope_type"),
        CheckConstraint("(scope_type = 'employee' AND subject_id IS NOT NULL) OR "
                        "(scope_type = 'store' AND subject_id IS NULL)",
                        name="ck_agent_budget_policies_scope_subject"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)",
                        name="ck_agent_budget_policies_currency"),
        CheckConstraint("length(limit_amount) BETWEEN 1 AND 40 AND length(per_call_limit) BETWEEN 1 AND 40",
                        name="ck_agent_budget_policies_limit_length"),
        CheckConstraint("version > 0", name="ck_agent_budget_policies_version_positive"),
        CheckConstraint("effective_until IS NULL OR effective_until > effective_from",
                        name="ck_agent_budget_policies_window"),
        CheckConstraint("length(provider_key) > 0 AND length(model) > 0 AND length(price_version) > 0",
                        name="ck_agent_budget_policies_binding_nonempty"),
        CheckConstraint("updated_at >= created_at", name="ck_agent_budget_policies_updated_after_created"),
        Index("ix_agent_budget_policies_active",
              "organization_id", "store_id", "scope_type", "active", "effective_from"),
        Index("ix_agent_budget_policies_subject",
              "organization_id", "store_id", "subject_id", "active"),
    )


class CostLedger(Base):
    """One persisted model-call reservation and its settlement/reconciliation.

    A row is uniquely identified by its run, step and call key. Price and usage
    snapshots preserve exactly what the estimate used; prompts, credentials and
    raw provider payloads are never stored here.
    """
    __tablename__ = "agent_cost_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_id: Mapped[str] = mapped_column(String(36), nullable=False)
    call_key: Mapped[str] = mapped_column(String(200), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(36), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    price_version: Mapped[str] = mapped_column(String(120), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reserved_amount: Mapped[str] = mapped_column(String(40), nullable=False)
    estimated_amount: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    reserved_usage: Mapped[dict] = mapped_column(JSON, nullable=False)
    usage_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    price_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    employee_budget_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    store_budget_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "run_id"],
                             ["agent_runs.organization_id", "agent_runs.store_id", "agent_runs.id"],
                             name="fk_agent_cost_ledger_run_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "run_id", "step_id"],
                             ["agent_steps.organization_id", "agent_steps.store_id",
                              "agent_steps.run_id", "agent_steps.id"],
                             name="fk_agent_cost_ledger_step_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "requester_id"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_agent_cost_ledger_requester_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "employee_budget_id"],
                             ["agent_budget_policies.organization_id",
                              "agent_budget_policies.store_id", "agent_budget_policies.id"],
                             name="fk_agent_cost_ledger_employee_budget", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "store_budget_id"],
                             ["agent_budget_policies.organization_id",
                              "agent_budget_policies.store_id", "agent_budget_policies.id"],
                             name="fk_agent_cost_ledger_store_budget", ondelete="RESTRICT"),
        UniqueConstraint("organization_id", "store_id", "run_id", "step_id", "call_key",
                         name="uq_agent_cost_ledger_call"),
        CheckConstraint("status IN ('reserved', 'settled', 'unknown', 'released', "
                        "'reconciliation_required')", name="ck_agent_cost_ledger_status"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)",
                        name="ck_agent_cost_ledger_currency"),
        CheckConstraint("length(reserved_amount) BETWEEN 1 AND 40",
                        name="ck_agent_cost_ledger_reserved_amount"),
        CheckConstraint("estimated_amount IS NULL OR length(estimated_amount) BETWEEN 1 AND 40",
                        name="ck_agent_cost_ledger_estimated_amount"),
        CheckConstraint("status NOT IN ('settled', 'reconciliation_required') "
                        "OR estimated_amount IS NOT NULL",
                        name="ck_agent_cost_ledger_terminal_amount"),
        CheckConstraint("length(call_key) > 0 AND length(provider_key) > 0 "
                        "AND length(model) > 0 AND length(price_version) > 0",
                        name="ck_agent_cost_ledger_identity_nonempty"),
        CheckConstraint("updated_at >= created_at",
                        name="ck_agent_cost_ledger_updated_after_created"),
        Index("ix_agent_cost_ledger_scope_time",
              "organization_id", "store_id", "created_at", "id"),
        Index("ix_agent_cost_ledger_requester_time",
              "organization_id", "store_id", "requester_id", "created_at", "id"),
        Index("ix_agent_cost_ledger_run", "organization_id", "store_id", "run_id"),
        Index("ix_agent_cost_ledger_status", "organization_id", "store_id", "status"),
    )


class SkillVersion(Base):
    """Immutable version binding; workflow status and evidence are CAS-updated."""
    __tablename__ = "agent_skill_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    skill_key: Mapped[str] = mapped_column(String(120), nullable=False)
    release_tag: Mapped[str] = mapped_column(String(120), nullable=False)
    spec_kind: Mapped[str] = mapped_column(String(32), nullable=False,
        default="legacy-digest", server_default="legacy-digest")
    spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(120), nullable=False)
    toolset_version: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    evaluated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    activated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    evaluation_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluation_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "store_id", "id", name="uq_agent_skill_scope_id"),
        UniqueConstraint("organization_id", "store_id", "skill_key", "release_tag",
                         name="uq_agent_skill_release"),
        ForeignKeyConstraint(["organization_id", "store_id"],
                             ["stores.organization_id", "stores.id"],
                             name="fk_agent_skill_store", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "created_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_agent_skill_creator", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "evaluated_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_agent_skill_evaluator", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "approved_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_agent_skill_approver", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "activated_by"],
                             ["subjects.organization_id", "subjects.id"],
                             name="fk_agent_skill_activator", ondelete="RESTRICT"),
        CheckConstraint("state IN ('draft','evaluated','approved','active','rolled_back')",
                        name="ck_agent_skill_state"),
        CheckConstraint("version >= 1", name="ck_agent_skill_version"),
        CheckConstraint("(spec_kind = 'legacy-digest' AND substr(skill_key,1,12) <> 'pi-resource.') OR "
                        "(spec_kind = 'pi-skill-resource/1' AND substr(skill_key,1,12) = 'pi-resource.')",
                        name="ck_agent_skill_spec_kind"),
        CheckConstraint("length(spec_sha256) = 64",
                        name="ck_agent_skill_spec_sha256"),
        CheckConstraint("evaluation_digest IS NULL OR length(evaluation_digest) = 64",
                        name="ck_agent_skill_evaluation_sha256"),
        CheckConstraint("length(skill_key) > 0 AND length(release_tag) > 0 AND "
                        "length(provider_key) > 0 AND length(model) > 0 AND "
                        "length(prompt_version) > 0 AND length(toolset_version) > 0",
                        name="ck_agent_skill_binding_nonempty"),
        CheckConstraint("updated_at >= created_at", name="ck_agent_skill_updated_after_created"),
        Index("ix_agent_skill_scope", "organization_id", "store_id", "skill_key", "state"),
        Index("uq_agent_skill_one_active", "organization_id", "store_id", "skill_key",
              unique=True, sqlite_where=text("state = 'active'"),
              postgresql_where=text("state = 'active'")),
    )


class SkillResourceManifest(Base):
    """P1-A immutable approved-source manifest; NOT an executable skill or Run pin."""
    __tablename__ = "agent_skill_resource_manifests"

    skill_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    knowledge_entry_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    canonical_manifest: Mapped[str] = mapped_column(Text(), nullable=False)
    spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id", "skill_id"],
            ["agent_skill_versions.organization_id", "agent_skill_versions.store_id", "agent_skill_versions.id"],
            name="fk_skill_resource_version", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "knowledge_entry_id"],
            ["knowledge_entries.organization_id", "knowledge_entries.store_id", "knowledge_entries.id"],
            name="fk_skill_resource_entry", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "store_id", "revision_id"],
            ["knowledge_revisions.organization_id", "knowledge_revisions.store_id", "knowledge_revisions.id"],
            name="fk_skill_resource_revision", ondelete="RESTRICT"),
        CheckConstraint("length(spec_sha256) = 64", name="ck_skill_resource_sha"),
        CheckConstraint("length(canonical_manifest) BETWEEN 1 AND 16384", name="ck_skill_resource_manifest_size"),
    )
