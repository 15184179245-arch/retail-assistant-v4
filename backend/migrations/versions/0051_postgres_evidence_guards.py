"""PostgreSQL audit append-only and knowledge receipt finalize-once guards.

SQLite retains its explicit historical service-API-only boundary. This does
not defend against a malicious PostgreSQL superuser disabling triggers.
"""
from alembic import op
import sqlalchemy as sa

revision='0051_postgres_evidence_guards'
down_revision='0050_office_workbench_review'
branch_labels=None
depends_on=None


def upgrade():
    if op.get_bind().dialect.name!='postgresql':
        return
    op.execute("""CREATE FUNCTION sx_ops001_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'audit evidence is immutable' USING ERRCODE='23000'; END; $$""")
    op.execute("""CREATE FUNCTION sx_ops001_knowledge_receipt_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='UPDATE' THEN
        IF OLD.response::jsonb='{}'::jsonb
           AND jsonb_typeof(NEW.response::jsonb)='object' AND NEW.response::jsonb<>'{}'::jsonb
           AND (to_jsonb(NEW)-'response')=(to_jsonb(OLD)-'response') THEN
          RETURN NEW;
        END IF;
      END IF;
      RAISE EXCEPTION 'knowledge command receipt is immutable' USING ERRCODE='23000';
    END; $$""")
    op.execute('CREATE TRIGGER trg_audit_events_ops001_immutable BEFORE UPDATE OR DELETE ON audit_events FOR EACH ROW EXECUTE FUNCTION sx_ops001_audit_immutable()')
    op.execute('CREATE TRIGGER trg_knowledge_commands_ops001_immutable BEFORE UPDATE OR DELETE ON knowledge_commands FOR EACH ROW EXECUTE FUNCTION sx_ops001_knowledge_receipt_immutable()')


def downgrade():
    connection=op.get_bind()
    if connection.dialect.name!='postgresql':
        return
    for table in ('audit_events','knowledge_commands'):
        if connection.execute(sa.text('SELECT 1 FROM '+table+' LIMIT 1')).first() is not None:
            raise RuntimeError('PostgreSQL evidence guard downgrade refused: data exists')
    op.execute('DROP TRIGGER trg_audit_events_ops001_immutable ON audit_events')
    op.execute('DROP TRIGGER trg_knowledge_commands_ops001_immutable ON knowledge_commands')
    op.execute('DROP FUNCTION sx_ops001_audit_immutable()')
    op.execute('DROP FUNCTION sx_ops001_knowledge_receipt_immutable()')
