"""Exact-turn provenance, additive even with SQLite FK enforcement enabled.

No table recreation, legacy backfill, or change to existing authority seals.
"""
from alembic import op
import sqlalchemy as sa
revision='0069_assistant_inbox_execution'
down_revision='0068_assistant_inbox'
branch_labels=depends_on=None
TABLES=(('assistant_turns','turn'),('assistant_turn_authorities','turn_authority'))
CHECK=('(source_inbox_id IS NULL AND source_inbox_sha256 IS NULL) OR '
       '(source_inbox_id IS NOT NULL AND source_inbox_sha256 IS NOT NULL AND length(source_inbox_sha256)=64)')


def upgrade():
    for table, name in TABLES:
        if op.get_context().dialect.name == 'sqlite':
            # SQLite permits nullable ADD COLUMN REFERENCES. Rebuilding a parent
            # table would break its existing child FKs, so never use batch mode.
            op.execute(f'ALTER TABLE {table} ADD COLUMN source_inbox_id VARCHAR(36) '
                       f'CONSTRAINT fk_{name}_inbox REFERENCES assistant_inbox(id) ON DELETE RESTRICT')
            op.execute(f'ALTER TABLE {table} ADD COLUMN source_inbox_sha256 VARCHAR(64) '
                       f'CONSTRAINT ck_{name}_inbox CHECK ({CHECK})')
        else:
            op.add_column(table,sa.Column('source_inbox_id',sa.String(36),nullable=True))
            op.add_column(table,sa.Column('source_inbox_sha256',sa.String(64),nullable=True))
            op.create_foreign_key('fk_'+name+'_inbox',table,'assistant_inbox',['source_inbox_id'],['id'],ondelete='RESTRICT')
            op.create_check_constraint('ck_'+name+'_inbox',table,CHECK)
        op.create_index('uq_'+name+'_inbox',table,['source_inbox_id'],unique=True)


def downgrade():
    if op.get_context().as_sql:raise RuntimeError('assistant_inbox_execution_downgrade_requires_data_check')
    for table,_ in TABLES:
        if op.get_bind().execute(sa.text('SELECT 1 FROM '+table+
            ' WHERE source_inbox_id IS NOT NULL OR source_inbox_sha256 IS NOT NULL LIMIT 1')).first():
            raise RuntimeError('assistant_inbox_execution_requires_safe_forward_recovery')
    for table,name in reversed(TABLES):
        op.drop_index('uq_'+name+'_inbox',table_name=table)
        if op.get_context().dialect.name != 'sqlite':
            op.drop_constraint('ck_'+name+'_inbox',table,type_='check')
            op.drop_constraint('fk_'+name+'_inbox',table,type_='foreignkey')
        # SQLite drops these inline column constraints with their own columns.
        op.drop_column(table,'source_inbox_sha256');op.drop_column(table,'source_inbox_id')
