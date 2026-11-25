"""Explicit settlement command DDL. No ORM imports; runtime FK is never disabled."""
import re
from alembic import op
import sqlalchemy as sa

revision = '0049_erp_settlement_dispatch'
down_revision = '0048_erp_catalog_maintenance'
branch_labels = None
depends_on = None
TABLES = ('erp_settlement_commands', 'erp_settlement_attempts', 'erp_settlement_observations')
IMMUTABLE = ('erp_settlement_commands', 'erp_settlement_observations')
OLD_ACTIONS = (
    'catalog_field_proposal',
    'purchase_document_proposal',
    'sales_return_proposal',
    'sales_document_proposal',
    'goods_receipt_document_proposal',
    'catalog_maintenance_proposal',
)


def _canonical(value):
    parts = re.split(r"('(?:[^']|'')*')", value)
    return ''.join(part if index % 2 else re.sub(r'[\s;]', '', part.lower())
                   for index, part in enumerate(parts))


def _action_signature(value):
    parts = re.split(r"('(?:[^']|'')*')", value)

    def syntax(part):
        return re.sub(r'[\s()]', '', re.sub(r'::(?:character varying|text)(?:\[\])?', '', part.lower()))

    return ''.join(part if index % 2 else syntax(part) for index, part in enumerate(parts))


def _preflight(bind, include_settlement):
    checks = {c['name']: c['sqltext'] for c in sa.inspect(bind).get_check_constraints('erp_command_requests')}
    values = OLD_ACTIONS + (() if include_settlement else ('settlement_document_proposal',))
    expression = 'action IN (' + ','.join(repr(v) for v in values) + ')'
    normalize = _action_signature
    allowed = {normalize(expression), normalize('action = ANY (ARRAY[' + ','.join(repr(v) for v in values) + '])')}
    if normalize(checks.get('ck_erp_request_action', '')) not in allowed:
        raise RuntimeError('refuse 0049: unexpected ERP action CHECK')
    if bind.dialect.name == 'sqlite':
        if bind.exec_driver_sql('PRAGMA foreign_keys').scalar_one() != 0:
            raise RuntimeError('refuse 0049: isolated migration connection must already have FK OFF')
        if bind.exec_driver_sql('PRAGMA foreign_key_check').first():
            raise RuntimeError('refuse 0049: preexisting FK corruption; no automatic repair')
        if sa.inspect(bind).has_table('_erp0049_requests_copy'):
            raise RuntimeError('refuse 0049: leftover request copy table; preserve evidence')
        rows = bind.execute(sa.text("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='erp_command_requests'")).all()
        expected = {
            f'trg_erp_command_requests_no_{verb}':
            f'CREATE TRIGGER trg_erp_command_requests_no_{verb} BEFORE {verb.upper()} ON erp_command_requests '
            "BEGIN SELECT RAISE(ABORT, 'ERP command evidence immutable'); END" for verb in ('update', 'delete')
        }
        if ({name for name, _ in rows} != set(expected)
                or any(_canonical(sql) != _canonical(expected[name]) for name, sql in rows)):
            raise RuntimeError('refuse 0049: unknown, changed or missing ERP immutable triggers')
        return [sql for _, sql in rows]
    if bind.dialect.name != 'postgresql':
        raise RuntimeError('refuse 0049: unsupported migration dialect')
    return []


def _set_action(bind, include_settlement, triggers):
    values = OLD_ACTIONS + (('settlement_document_proposal',) if include_settlement else ())
    expression = 'action IN (' + ','.join(repr(v) for v in values) + ')'
    if bind.dialect.name == 'postgresql':
        op.drop_constraint('ck_erp_request_action', 'erp_command_requests', type_='check')
        op.create_check_constraint('ck_erp_request_action', 'erp_command_requests', expression)
        return
    table = sa.Table('erp_command_requests', sa.MetaData(), autoload_with=bind)
    old = next(c for c in table.constraints if isinstance(c, sa.CheckConstraint) and c.name == 'ck_erp_request_action')
    table.constraints.remove(old)
    table.append_constraint(sa.CheckConstraint(expression, name='ck_erp_request_action'))
    count = bind.execute(sa.text('SELECT count(*) FROM erp_command_requests')).scalar_one()
    temporary = '_erp0049_requests_copy'
    table.name = temporary
    bind.execute(sa.schema.CreateTable(table))
    names = ','.join(bind.dialect.identifier_preparer.quote(c.name) for c in table.columns)
    bind.exec_driver_sql(f'INSERT INTO {temporary} ({names}) SELECT {names} FROM erp_command_requests')
    if bind.execute(sa.text(f'SELECT count(*) FROM {temporary}')).scalar_one() != count:
        raise RuntimeError('0049 request copy row mismatch; original retained')
    bind.exec_driver_sql('DROP TABLE erp_command_requests')
    bind.exec_driver_sql(f'ALTER TABLE {temporary} RENAME TO erp_command_requests')
    table.name = 'erp_command_requests'
    for index in sorted(table.indexes, key=lambda i: i.name):
        bind.execute(sa.schema.CreateIndex(index))
    for sql in triggers:
        bind.exec_driver_sql(sql)
    if (bind.execute(sa.text('SELECT count(*) FROM erp_command_requests')).scalar_one() != count
            or bind.exec_driver_sql('PRAGMA foreign_key_check').first() is not None):
        raise RuntimeError('0049 request rebuild validation failed; preserve isolated snapshot')


def _guards(bind):
    for table in IMMUTABLE:
        if bind.dialect.name == 'sqlite':
            for verb in ('update', 'delete'):
                bind.exec_driver_sql(f"CREATE TRIGGER trg_{table}_{verb}_deny BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'erp settlement evidence is immutable'); END")
        else:
            bind.exec_driver_sql(f"CREATE FUNCTION deny_{table}_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'erp settlement evidence is immutable'; END; $$")
            bind.exec_driver_sql(f'CREATE TRIGGER trg_{table}_deny BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION deny_{table}_mutation()')


def upgrade():
    bind = op.get_bind()
    triggers = _preflight(bind, True)
    _set_action(bind, True, triggers)

    op.create_table(
        'erp_settlement_commands',
        sa.Column('id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('organization_id', sa.String(36), nullable=False),
        sa.Column('store_id', sa.String(36), nullable=False),
        sa.Column('workpaper_id', sa.String(36), nullable=False),
        sa.Column('request_id', sa.String(36), nullable=False),
        sa.Column('route', sa.String(8), nullable=False),
        sa.Column('operation', sa.String(24), nullable=False),
        sa.Column('party_type', sa.String(16), nullable=False),
        sa.Column('party_ref', sa.String(120), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('settlement_amount', sa.String(32), nullable=False),
        sa.Column('source_version', sa.Integer(), nullable=False),
        sa.Column('source_digest', sa.String(64), nullable=False),
        sa.Column('source_snapshot', sa.JSON(), nullable=False),
        sa.Column('proposal_key', sa.String(200), nullable=False),
        sa.Column('proposal_digest', sa.String(64), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('length(source_digest)=64 AND length(proposal_digest)=64', name='ck_ersc_fin_digests'),
        sa.CheckConstraint('length(proposal_key) BETWEEN 1 AND 200', name='ck_ersc_fin_key'),
        sa.CheckConstraint("route = 'api'", name='ck_ersc_fin_route'),
        sa.CheckConstraint("operation IN ('receipt_allocation', 'payment_allocation')", name='ck_ersc_fin_op'),
        sa.CheckConstraint("party_type IN ('customer', 'supplier')", name='ck_ersc_fin_party'),
        sa.CheckConstraint("currency IN ('CNY', 'USD')", name='ck_ersc_fin_curr'),
        sa.CheckConstraint('source_version > 0', name='ck_ersc_fin_ver'),
        sa.ForeignKeyConstraint(['organization_id', 'created_by'], ['subjects.organization_id', 'subjects.id'], name='fk_ersc_fin_creator', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'workpaper_id'], ['finance_workpapers.organization_id', 'finance_workpapers.store_id', 'finance_workpapers.id'], name='fk_ersc_fin_workpaper', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'request_id'], ['erp_command_requests.organization_id', 'erp_command_requests.store_id', 'erp_command_requests.id'], name='fk_ersc_fin_request', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id'], ['stores.organization_id', 'stores.id'], name='fk_ersc_fin_store', ondelete="RESTRICT"),
        sa.UniqueConstraint('organization_id', 'store_id', 'workpaper_id', name='uq_ersc_fin_workpaper'),
        sa.UniqueConstraint('organization_id', 'store_id', 'proposal_key', name='uq_ersc_fin_proposal_key'),
        sa.UniqueConstraint('organization_id', 'store_id', 'request_id', name='uq_ersc_fin_request'),
        sa.UniqueConstraint('organization_id', 'store_id', 'id', name='uq_ersc_fin_scope'),
    )
    op.create_index('ix_ersc_fin_created', 'erp_settlement_commands', ['organization_id', 'store_id', 'created_at', 'id'])

    op.create_table(
        'erp_settlement_attempts',
        sa.Column('id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('organization_id', sa.String(36), nullable=False),
        sa.Column('store_id', sa.String(36), nullable=False),
        sa.Column('command_id', sa.String(36), nullable=False),
        sa.Column('intent_id', sa.String(36), nullable=False),
        sa.Column('queue_key', sa.String(200), nullable=False),
        sa.Column('queue_digest', sa.String(64), nullable=False),
        sa.Column('input_digest', sa.String(64), nullable=False),
        sa.Column('executor_id', sa.String(36), nullable=False),
        sa.Column('account_id', sa.String(36), nullable=False),
        sa.Column('session_id', sa.String(36), nullable=False),
        sa.Column('account_version', sa.Integer(), nullable=False),
        sa.Column('authorization_epoch', sa.Integer(), nullable=False),
        sa.Column('authorization_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('allocation_results', sa.JSON(), nullable=False),
        sa.Column('delivery_ref', sa.String(200), nullable=True),
        sa.Column('external_ref', sa.String(200), nullable=True),
        sa.Column('receipt_digest', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('length(input_digest)=64 AND length(queue_digest)=64', name='ck_ersa_fin_digests'),
        sa.CheckConstraint('length(queue_key) BETWEEN 1 AND 200', name='ck_ersa_fin_key'),
        sa.CheckConstraint("state IN ('staged', 'dispatched', 'accepted', 'rejected', 'unknown')", name='ck_ersa_fin_state'),
        sa.CheckConstraint('version > 0 AND account_version > 0 AND authorization_epoch > 0', name='ck_ersa_fin_versions'),
        sa.ForeignKeyConstraint(['account_id'], ['employee_accounts.id'], name='fk_ersa_fin_acc', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['session_id'], ['auth_sessions.id'], name='fk_ersa_fin_sess', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'executor_id'], ['subjects.organization_id', 'subjects.id'], name='fk_ersa_fin_exec', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'command_id'], ['erp_settlement_commands.organization_id', 'erp_settlement_commands.store_id', 'erp_settlement_commands.id'], name='fk_ersa_fin_cmd', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'intent_id'], ['erp_command_intents.organization_id', 'erp_command_intents.store_id', 'erp_command_intents.id'], name='fk_ersa_fin_intent', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id'], ['stores.organization_id', 'stores.id'], name='fk_ersa_fin_store', ondelete="RESTRICT"),
        sa.UniqueConstraint('organization_id', 'store_id', 'command_id', name='uq_ersa_fin_command'),
        sa.UniqueConstraint('organization_id', 'store_id', 'intent_id', name='uq_ersa_fin_intent'),
        sa.UniqueConstraint('organization_id', 'store_id', 'queue_key', name='uq_ersa_fin_queue_key'),
        sa.UniqueConstraint('organization_id', 'store_id', 'id', name='uq_ersa_fin_scope'),
    )
    op.create_index('ix_ersa_fin_state', 'erp_settlement_attempts', ['organization_id', 'store_id', 'state', 'updated_at'])

    op.create_table(
        'erp_settlement_observations',
        sa.Column('id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('organization_id', sa.String(36), nullable=False),
        sa.Column('store_id', sa.String(36), nullable=False),
        sa.Column('attempt_id', sa.String(36), nullable=False),
        sa.Column('command_id', sa.String(36), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('observed_state', sa.String(24), nullable=False),
        sa.Column('receipt_digest', sa.String(64), nullable=False),
        sa.Column('details', sa.JSON(), nullable=False),
        sa.Column('recorded_by', sa.String(36), nullable=False),
        sa.CheckConstraint('length(receipt_digest)=64', name='ck_erso_fin_digest'),
        sa.CheckConstraint("observed_state IN ('dispatched', 'accepted', 'rejected', 'unknown')", name='ck_erso_fin_state'),
        sa.ForeignKeyConstraint(['organization_id', 'recorded_by'], ['subjects.organization_id', 'subjects.id'], name='fk_erso_fin_rec', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'attempt_id'], ['erp_settlement_attempts.organization_id', 'erp_settlement_attempts.store_id', 'erp_settlement_attempts.id'], name='fk_erso_fin_att', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id', 'command_id'], ['erp_settlement_commands.organization_id', 'erp_settlement_commands.store_id', 'erp_settlement_commands.id'], name='fk_erso_fin_cmd', ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(['organization_id', 'store_id'], ['stores.organization_id', 'stores.id'], name='fk_erso_fin_store', ondelete="RESTRICT"),
        sa.UniqueConstraint('organization_id', 'store_id', 'id', name='uq_erso_fin_scope'),
    )
    op.create_index('ix_erso_fin_observed', 'erp_settlement_observations', ['organization_id', 'store_id', 'attempt_id', 'observed_at'])
    _guards(bind)


def downgrade():
    bind = op.get_bind()
    if any(bind.execute(sa.text(f'SELECT 1 FROM {table} LIMIT 1')).first() for table in TABLES):
        raise RuntimeError('0049 downgrade refused with settlement evidence; disable and forward-fix')
    if bind.execute(sa.text("SELECT 1 FROM erp_command_requests WHERE action='settlement_document_proposal' LIMIT 1")).first():
        raise RuntimeError('0049 downgrade refused with settlement authorization history')
    triggers = _preflight(bind, False)
    for table in reversed(TABLES):
        op.drop_table(table)
    if bind.dialect.name == 'postgresql':
        for table in IMMUTABLE:
            bind.exec_driver_sql(f'DROP FUNCTION deny_{table}_mutation()')
    _set_action(bind, False, triggers)
