"""Canonical readonly tool-result facts; no backfill or execution permission."""
from alembic import op
import sqlalchemy as sa

revision='0076_pi_canonical_tool_results'
down_revision='0075_pi_readonly_stream_profile'
branch_labels=depends_on=None


def upgrade():
    op.create_table('agent_pi_tool_results',
        sa.Column('id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('record_version', sa.Integer(), nullable=False),
        sa.Column('result_abi', sa.String(32), nullable=False),
        sa.Column('organization_id', sa.String(36), nullable=False),
        sa.Column('store_id', sa.String(36), nullable=False),
        sa.Column('requester_id', sa.String(36), nullable=False),
        sa.Column('agent_id', sa.String(36), nullable=False),
        sa.Column('run_id', sa.String(36), nullable=False),
        sa.Column('model_step_id', sa.String(36), nullable=False),
        sa.Column('tool_step_id', sa.String(36), nullable=False),
        sa.Column('model_kind', sa.String(16), nullable=False),
        sa.Column('tool_kind', sa.String(16), nullable=False),
        sa.Column('pi_receipt_id', sa.String(36), nullable=False),
        sa.Column('native_call_id', sa.String(100), nullable=False),
        sa.Column('tool_key', sa.String(120), nullable=False),
        sa.Column('tool_name', sa.String(120), nullable=False),
        sa.Column('tool_version', sa.String(120), nullable=False),
        sa.Column('toolset_version', sa.String(200), nullable=False),
        sa.Column('input_digest', sa.String(64), nullable=False),
        sa.Column('execution_epoch', sa.BigInteger(), nullable=False),
        sa.Column('runtime_record_sha256', sa.String(64), nullable=False),
        sa.Column('result_kind', sa.String(16), nullable=False),
        sa.Column('output_ref', sa.String(71), nullable=False),
        sa.Column('payload_sha256', sa.String(64), nullable=False),
        sa.Column('source_refs_sha256', sa.String(64), nullable=False),
        sa.Column('canonical_ref', sa.String(71), nullable=False),
        sa.Column('canonical_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('length(native_call_id) BETWEEN 1 AND 100 AND length(canonical_json) BETWEEN 1 AND 262144', name='ck_pi_result_body'),
        sa.CheckConstraint("record_version = 1 AND result_abi = 'pi-tool-result/1'", name='ck_pi_result_contract'),
        sa.CheckConstraint("length(input_digest) = 64 AND length(runtime_record_sha256) = 64 AND length(payload_sha256) = 64 AND length(source_refs_sha256) = 64 AND length(toolset_version) = 71 AND substr(toolset_version,1,7) = 'sha256:' AND output_ref = 'sha256:' || payload_sha256 AND length(canonical_ref) = 71 AND substr(canonical_ref,1,7) = 'sha256:'", name='ck_pi_result_digests'),
        sa.CheckConstraint("model_kind = 'model' AND tool_kind = 'tool' AND execution_epoch > 0", name='ck_pi_result_identity'),
        sa.CheckConstraint("result_kind IN ('success','tool_error') AND (result_kind <> 'tool_error' OR tool_key = 'memory.search')", name='ck_pi_result_kind'),
        sa.CheckConstraint("tool_version = '1' AND ((tool_key = 'tasks.list_mine' AND tool_name = 'v4_tasks_list_mine_v1') OR (tool_key = 'knowledge.search' AND tool_name = 'v4_knowledge_search_v1') OR (tool_key = 'catalog.search' AND tool_name = 'v4_catalog_search_v1') OR (tool_key = 'memory.search' AND tool_name = 'v4_memory_search_v1'))", name='ck_pi_result_tool'),
        sa.ForeignKeyConstraint(['organization_id', 'agent_id'], ['subjects.organization_id', 'subjects.id'], name='fk_pi_result_agent', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'run_id', 'model_step_id', 'model_kind'], ['agent_steps.organization_id', 'agent_steps.store_id', 'agent_steps.run_id', 'agent_steps.id', 'agent_steps.kind'], name='fk_pi_result_model_step', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'run_id', 'native_call_id'], ['agent_pi_tool_receipts.organization_id', 'agent_pi_tool_receipts.store_id', 'agent_pi_tool_receipts.run_id', 'agent_pi_tool_receipts.native_call_id'], name='fk_pi_result_native_scope', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['pi_receipt_id'], ['agent_pi_tool_receipts.id'], name='fk_pi_result_receipt_id', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'requester_id'], ['subjects.organization_id', 'subjects.id'], name='fk_pi_result_requester', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'run_id'], ['agent_runs.organization_id', 'agent_runs.store_id', 'agent_runs.id'], name='fk_pi_result_run_scope', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'run_id', 'tool_step_id', 'tool_kind'], ['agent_steps.organization_id', 'agent_steps.store_id', 'agent_steps.run_id', 'agent_steps.id', 'agent_steps.kind'], name='fk_pi_result_tool_step', ondelete="RESTRICT"),
        sa.UniqueConstraint('organization_id', 'store_id', 'run_id', 'native_call_id', name='uq_pi_result_native'),
        sa.UniqueConstraint('pi_receipt_id', name='uq_pi_result_receipt'),
        sa.UniqueConstraint('organization_id', 'store_id', 'run_id', 'tool_step_id', name='uq_pi_result_tool_step'),
    )
    op.create_index('ix_pi_result_run','agent_pi_tool_results',['organization_id','store_id','run_id','created_at'])
    if op.get_bind().dialect.name=='sqlite':
        # REPLACE may delete without row DELETE triggers when recursive_triggers
        # is off. Reject every existing PK/unique axis BEFORE conflict handling.
        op.execute("""CREATE TRIGGER trg_pi_tool_result_no_insert_conflict
            BEFORE INSERT ON agent_pi_tool_results
            WHEN EXISTS (SELECT 1 FROM agent_pi_tool_results r WHERE
                r.id = NEW.id OR r.pi_receipt_id = NEW.pi_receipt_id OR
                (r.organization_id = NEW.organization_id AND r.store_id = NEW.store_id
                 AND r.run_id = NEW.run_id AND r.native_call_id = NEW.native_call_id) OR
                (r.organization_id = NEW.organization_id AND r.store_id = NEW.store_id
                 AND r.run_id = NEW.run_id AND r.tool_step_id = NEW.tool_step_id))
            BEGIN SELECT RAISE(ABORT, 'pi_tool_result_conflict'); END""")
        for action in ('UPDATE','DELETE'):
            op.execute("CREATE TRIGGER trg_pi_tool_result_no_"+action.lower()+
                " BEFORE "+action+" ON agent_pi_tool_results BEGIN SELECT RAISE(ABORT, 'pi_tool_result_immutable'); END")
    elif op.get_bind().dialect.name=='postgresql':
        op.execute("CREATE FUNCTION deny_pi_tool_result_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'pi_tool_result_immutable'; END; $$")
        op.execute("CREATE TRIGGER trg_pi_tool_result_immutable BEFORE UPDATE OR DELETE ON agent_pi_tool_results FOR EACH ROW EXECUTE FUNCTION deny_pi_tool_result_mutation()")
        op.execute("CREATE TRIGGER trg_pi_tool_result_no_truncate BEFORE TRUNCATE ON agent_pi_tool_results FOR EACH STATEMENT EXECUTE FUNCTION deny_pi_tool_result_mutation()")


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('pi_canonical_downgrade_requires_data_check')
    if op.get_bind().execute(sa.text('SELECT 1 FROM agent_pi_tool_results LIMIT 1')).first()is not None:
        raise RuntimeError('pi_canonical_facts_require_safe_forward_recovery')
    if op.get_bind().dialect.name=='sqlite':
        op.execute('DROP TRIGGER trg_pi_tool_result_no_insert_conflict')
        op.execute('DROP TRIGGER trg_pi_tool_result_no_update')
        op.execute('DROP TRIGGER trg_pi_tool_result_no_delete')
    elif op.get_bind().dialect.name=='postgresql':
        op.execute('DROP TRIGGER trg_pi_tool_result_no_truncate ON agent_pi_tool_results')
        op.execute('DROP TRIGGER trg_pi_tool_result_immutable ON agent_pi_tool_results')
        op.execute('DROP FUNCTION deny_pi_tool_result_mutation()')
    op.drop_index('ix_pi_result_run',table_name='agent_pi_tool_results')
    op.drop_table('agent_pi_tool_results')
