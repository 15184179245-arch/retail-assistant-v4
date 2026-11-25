"""Global knowledge projection generation, its triggers, and PG0051 TRUNCATE gap.

The projection is derived state keyed by one monotonic ``knowledge_source_generation``
row.  Every ordinary write to one of the six knowledge source tables must bump that
generation, otherwise a published build could keep serving a stale window.  SQLite
fires row triggers for DELETE implicitly caused by ``INSERT OR REPLACE`` /
``UPDATE OR REPLACE`` only when ``recursive_triggers`` is on, so the four
knowledge tables bump on every BEFORE INSERT/UPDATE/DELETE: the NEW-row bump of the
replacement statement closes the old row even when its implicit DELETE is invisible.
The two filtered tables (``memory_commands``/``audit_events``) must not silently
drop a relevant old row through REPLACE, so their BEFORE INSERT/UPDATE predicates
also probe every PK and unique conflict axis on NEW for a relevant existing row
(including, for UPDATE, a relevant OLD row).  These statements bump the generation;
they never abort a REPLACE, so the caller's idempotency/conflict behaviour is
unchanged.  A statement that fails a CHECK/UNIQUE/FK constraint rolls its bump back
with the statement, and ``OR FAIL`` keeps the rows already bumped before it.

The singleton generation row rejects direct DELETE, REPLACE reseeding, reset, and
generation overflow.  ``audit_events`` and ``memory_commands`` bump only for the
rows that actually change knowledge (``target_type='knowledge_entry'`` and
``operation='promote'``), so ordinary login/agent audits and the rebuild's own
audit record do not invalidate the projection.

PostgreSQL: 0051 guarded audit/receipt UPDATE and DELETE but left TRUNCATE
uncovered; this migration adds fail-closed TRUNCATE triggers for those tables and
the knowledge source/projection tables.  Older migrations are not edited.
"""
from alembic import op
import sqlalchemy as sa

# The frozen file name stays 0077_knowledge_projection_generation.py, but the
# revision id must fit PG's alembic_version.version_num VARCHAR(32).
revision = '0077_knowledge_projection_gen'
down_revision = '0076_pi_canonical_tool_results'
branch_labels = None
depends_on = None

_SOURCE_TABLES = (
    'knowledge_entries',
    'knowledge_revisions',
    'knowledge_revision_access',
    'knowledge_commands',
    'memory_commands',
    'audit_events',
)

# Relevant-row filter for the two tables that also carry unrelated rows.
_FILTER_COLUMN = {
    'memory_commands': ('operation', 'promote'),
    'audit_events': ('target_type', 'knowledge_entry'),
}

# Exact PK/unique axes used to detect a REPLACE-doomed old row and to keep the
# filtered-table predicates complete.
_CONFLICT_AXES = {
    'knowledge_entries': (('id',), (('organization_id', 'store_id', 'id'),)),
    'knowledge_revisions': (
        ('id',),
        (('entry_id', 'version'), ('organization_id', 'store_id', 'id')),
    ),
    'knowledge_revision_access': (('revision_id',), ()),
    'knowledge_commands': (
        ('id',),
        (('organization_id', 'store_id', 'actor_id', 'operation', 'idempotency_key'),),
    ),
    'memory_commands': (
        ('id',),
        (('organization_id', 'store_id', 'actor_id', 'operation', 'idempotency_key'),),
    ),
    'audit_events': (
        ('id',),
        (('organization_id', 'store_id', 'idempotency_key'),),
    ),
}


def _conflict_axes(table):
    pk, uniques = _CONFLICT_AXES[table]
    return (pk,) + tuple(uniques)


def _doomed_match(table, *, update, self_expr='r.rowid'):
    """Rows that a REPLACE statement would delete implicitly.

    The NEW row can hit the primary key and several unique keys at once, so every
    axis is OR-ed; the caller must not stop at the first matching row.
    """
    parts = []
    for axis in _conflict_axes(table):
        parts.append('(' + ' AND '.join('r.' + col + ' = NEW.' + col for col in axis) + ')')
    clause = ' OR '.join(parts)
    if update:
        clause = '(' + clause + ') AND ' + self_expr + ' <> OLD.' + self_expr.split('.')[-1]
    return clause


def _bump_predicate(table, action, *, self_expr='r.rowid', probe_conflicts=True):
    """WHEN predicate for a BEFORE row trigger; ``None`` means every row.

    SQLite probes every PK/unique conflict axis because ``OR REPLACE`` can
    delete a relevant old row while its DELETE trigger stays invisible.
    PostgreSQL has no ``OR REPLACE``: ``ON CONFLICT DO UPDATE`` updates the
    existing row (OLD/NEW are visible to the UPDATE trigger) and ``DO NOTHING``
    may bump conservatively, so PG predicates stay subquery-free because
    trigger WHEN expressions forbid subqueries.
    """
    column, value = _FILTER_COLUMN.get(table, (None, None))
    if column is None:
        return None
    new_relevant = "NEW.%s = '%s'" % (column, value)
    old_relevant = "OLD.%s = '%s'" % (column, value)
    if not probe_conflicts:
        if action == 'insert':
            return new_relevant
        if action == 'update':
            return '(%s OR %s)' % (new_relevant, old_relevant)
        return old_relevant
    doomed = _doomed_match(table, update=(action == 'update'), self_expr=self_expr)
    relevant_conflict = (
        "EXISTS (SELECT 1 FROM %s r WHERE r.%s = '%s' AND (%s))"
        % (table, column, value, doomed)
    )
    if action == 'insert':
        return '(%s OR %s)' % (new_relevant, relevant_conflict)
    if action == 'update':
        return '(%s OR %s OR %s)' % (new_relevant, old_relevant, relevant_conflict)
    return old_relevant


def _now_column_type():
    return sa.DateTime(timezone=True)


def _create_tables():
    op.create_table(
        'knowledge_projection_builds',
        sa.Column('build_id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('source_generation', sa.BigInteger(), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('resolver_version', sa.Integer(), nullable=False),
        sa.Column('normalizer_version', sa.Integer(), nullable=False),
        sa.Column('projection_schema_version', sa.Integer(), nullable=False),
        sa.Column('window_limit', sa.Integer(), nullable=False),
        sa.Column('entry_count', sa.Integer(), nullable=False),
        sa.Column('input_digest', sa.String(64), nullable=False),
        sa.Column('projection_digest', sa.String(64), nullable=False),
        sa.Column('built_at', _now_column_type(), nullable=False),
        sa.Column('window_expires_at', _now_column_type(), nullable=True),
        sa.Column('published_at', _now_column_type(), nullable=True),
        sa.Column('publisher_id', sa.String(120), nullable=True),
        sa.Column('event_id', sa.String(36), nullable=True),
        sa.UniqueConstraint('event_id', name='uq_knowledge_projection_builds_event'),
        sa.CheckConstraint('source_generation >= 0', name='ck_knowledge_projection_builds_source_generation'),
        sa.CheckConstraint("state IN ('staging','published','retired')", name='ck_knowledge_projection_builds_state'),
        sa.CheckConstraint(
            'resolver_version = 1 AND normalizer_version = 1 AND projection_schema_version = 1',
            name='ck_knowledge_projection_builds_versions',
        ),
        sa.CheckConstraint('window_limit > 0 AND window_limit <= 10000', name='ck_knowledge_projection_builds_window'),
        sa.CheckConstraint('entry_count >= 0', name='ck_knowledge_projection_builds_entry_count'),
        sa.CheckConstraint(
            "(state <> 'published') OR (published_at IS NOT NULL AND publisher_id IS NOT NULL)",
            name='ck_knowledge_projection_builds_published_fields',
        ),
    )
    op.create_index('ix_knowledge_projection_builds_state', 'knowledge_projection_builds', ['state'])

    op.create_table(
        'knowledge_entry_projections',
        sa.Column('build_id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('entry_id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('organization_id', sa.String(36), nullable=False),
        sa.Column('store_id', sa.String(36), nullable=False),
        sa.Column('entry_version', sa.Integer(), nullable=False),
        sa.Column('current_revision_id', sa.String(36), nullable=True),
        sa.Column('revision_version', sa.Integer(), nullable=True),
        sa.Column('submitted_at', _now_column_type(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('expires_at', _now_column_type(), nullable=True),
        sa.Column('scope_type', sa.String(16), nullable=False),
        sa.Column('scope_ref', sa.String(200), nullable=True),
        sa.Column('approved_by', sa.String(36), nullable=True),
        sa.Column('approved_at', _now_column_type(), nullable=True),
        sa.Column('read_class', sa.String(16), nullable=False),
        sa.Column('policy_version', sa.Integer(), nullable=False),
        sa.Column('access_epoch', sa.BigInteger(), nullable=False),
        sa.Column('scope_reference_class', sa.String(16), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('body_excerpt', sa.String(400), nullable=False),
        sa.Column('source_type', sa.String(24), nullable=False),
        sa.Column('source_ref', sa.String(500), nullable=True),
        sa.Column('lower_search_bytes', sa.LargeBinary(), nullable=False),
        sa.Column('proof', sa.JSON(), nullable=False),
        sa.Column('source_digest', sa.String(64), nullable=False),
        sa.Column('row_digest', sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ['build_id'], ['knowledge_projection_builds.build_id'],
            name='fk_knowledge_entry_projections_build', ondelete='CASCADE',
        ),
        sa.CheckConstraint(
            "read_class IN ('public','restricted','conflict','hold','unverified','missing')",
            name='ck_knowledge_entry_projections_read_class',
        ),
        sa.CheckConstraint(
            "scope_reference_class IN ('public','restricted')",
            name='ck_knowledge_entry_projections_scope_class',
        ),
        sa.CheckConstraint(
            'entry_version > 0 AND (revision_version IS NULL OR revision_version > 0)',
            name='ck_knowledge_entry_projections_versions',
        ),
        sa.CheckConstraint('policy_version = 1 AND access_epoch > 0', name='ck_knowledge_entry_projections_epoch'),
    )
    op.create_index(
        'ix_knowledge_entry_projections_scope',
        'knowledge_entry_projections', ['build_id', 'organization_id', 'store_id'],
    )

    op.create_table(
        'knowledge_source_generation',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('generation', sa.BigInteger(), nullable=False),
        sa.Column('format_version', sa.Integer(), nullable=False),
        sa.Column('build_id', sa.String(36), nullable=True),
        sa.Column('entry_count', sa.Integer(), nullable=False),
        sa.Column('built_at', _now_column_type(), nullable=True),
        sa.Column('updated_at', _now_column_type(), nullable=False),
        sa.ForeignKeyConstraint(
            ['build_id'], ['knowledge_projection_builds.build_id'],
            name='fk_knowledge_source_generation_build', ondelete='RESTRICT',
        ),
        sa.CheckConstraint('id = 1', name='ck_knowledge_source_generation_singleton'),
        sa.CheckConstraint('generation > 0', name='ck_knowledge_source_generation_positive'),
        sa.CheckConstraint('generation <= 9223372036854775807', name='ck_knowledge_source_generation_max'),
        sa.CheckConstraint('format_version = 1', name='ck_knowledge_source_generation_format'),
        sa.CheckConstraint('entry_count >= 0', name='ck_knowledge_source_generation_entry_count'),
    )


def _seed_generation():
    op.execute(
        "INSERT INTO knowledge_source_generation "
        "(id, generation, format_version, build_id, entry_count, built_at, updated_at) "
        "SELECT 1, 1, 1, NULL, 0, NULL, CURRENT_TIMESTAMP "
        "WHERE NOT EXISTS (SELECT 1 FROM knowledge_source_generation WHERE id = 1)"
    )


def _sqlite_upgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'sqlite':
        return
    # BEFORE triggers: on INSERT OR REPLACE the implicit DELETE of the old row is
    # invisible when recursive_triggers is off, but the NEW-row bump still closes
    # it.  The filtered tables additionally probe a relevant old row that the
    # statement is about to replace through any PK/unique axis.
    for table in _SOURCE_TABLES:
        for action, suffix in (('insert', 'ins'), ('update', 'upd'), ('delete', 'del')):
            predicate = _bump_predicate(table, action)
            when = ('WHEN ' + predicate + ' ') if predicate else ''
            op.execute(
                "CREATE TRIGGER trg_kp_{t}_{s} BEFORE {A} ON {t} {w}BEGIN "
                "SELECT RAISE(ABORT, 'knowledge_projection_generation_missing') "
                "WHERE NOT EXISTS (SELECT 1 FROM knowledge_source_generation WHERE id = 1); "
                "UPDATE knowledge_source_generation SET generation = generation + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = 1; END".format(
                    t=table, s=suffix, A=action.upper(), w=when,
                )
            )
    op.execute(
        "CREATE TRIGGER trg_kp_gen_ins BEFORE INSERT ON knowledge_source_generation "
        "WHEN NEW.id <> 1 OR NEW.generation <> 1 OR NEW.format_version <> 1 "
        "OR typeof(NEW.generation) <> 'integer' "
        "OR NEW.build_id IS NOT NULL OR EXISTS (SELECT 1 FROM knowledge_source_generation) "
        "BEGIN SELECT RAISE(ABORT, 'knowledge_projection_generation_reseed_rejected'); END"
    )
    op.execute(
        # Same-generation updates are the publish path (build pointer and
        # counters only); a *reset* or a multi-step jump is still rejected.
        "CREATE TRIGGER trg_kp_gen_upd BEFORE UPDATE ON knowledge_source_generation "
        "WHEN NEW.id <> 1 OR NEW.id <> OLD.id "
        "OR typeof(NEW.generation) <> 'integer' "
        "OR NEW.generation < OLD.generation OR NEW.generation > OLD.generation + 1 "
        "OR NEW.format_version <> 1 OR NEW.entry_count < 0 "
        "OR (OLD.build_id IS NOT NULL AND NEW.build_id IS NULL) "
        "BEGIN SELECT RAISE(ABORT, 'knowledge_projection_generation_update_rejected'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_kp_gen_del BEFORE DELETE ON knowledge_source_generation "
        "BEGIN SELECT RAISE(ABORT, 'knowledge_projection_generation_delete_rejected'); END"
    )


_PG_BUMP = """
CREATE FUNCTION knowledge_projection_bump() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM knowledge_source_generation WHERE id = 1) THEN
    RAISE EXCEPTION 'knowledge projection generation missing' USING ERRCODE='23000';
  END IF;
  UPDATE knowledge_source_generation
     SET generation = generation + 1, updated_at = CURRENT_TIMESTAMP
   WHERE id = 1;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END; $$"""

_PG_GEN_GUARD = """
CREATE FUNCTION knowledge_projection_generation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'knowledge projection generation delete rejected' USING ERRCODE='23000';
  ELSIF TG_OP = 'INSERT' THEN
    IF NEW.id <> 1 OR NEW.generation <> 1 OR NEW.format_version <> 1 OR NEW.build_id IS NOT NULL THEN
      RAISE EXCEPTION 'knowledge projection generation reseed rejected' USING ERRCODE='23000';
    END IF;
  ELSE
    IF NEW.id <> OLD.id OR NEW.id <> 1 THEN
      RAISE EXCEPTION 'knowledge projection generation identity rejected' USING ERRCODE='23000';
    END IF;
    -- A same-generation update is the publish path (build pointer/counters);
    -- a reset or a multi-step jump is still rejected.
    IF NEW.generation < OLD.generation OR NEW.generation > OLD.generation + 1 THEN
      RAISE EXCEPTION 'knowledge projection generation self-assign, reset or overflow rejected' USING ERRCODE='23000';
    END IF;
    IF NEW.format_version <> OLD.format_version OR NEW.format_version <> 1 THEN
      RAISE EXCEPTION 'knowledge projection format version rejected' USING ERRCODE='23000';
    END IF;
    IF OLD.build_id IS NOT NULL AND NEW.build_id IS NULL THEN
      RAISE EXCEPTION 'knowledge projection generation head cleared' USING ERRCODE='23000';
    END IF;
  END IF;
  RETURN NEW;
END; $$"""

_PG_DENY = """
CREATE FUNCTION knowledge_projection_deny_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'knowledge projection truncate rejected' USING ERRCODE='23000';
END; $$"""


_PG_TRUNCATE_BUMP = """
CREATE FUNCTION knowledge_projection_truncate_bump() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'memory_commands' THEN
    IF EXISTS (SELECT 1 FROM memory_commands WHERE operation = 'promote') THEN
      UPDATE knowledge_source_generation
         SET generation = generation + 1, updated_at = CURRENT_TIMESTAMP
       WHERE id = 1;
    END IF;
  ELSE
    UPDATE knowledge_source_generation
       SET generation = generation + 1, updated_at = CURRENT_TIMESTAMP
     WHERE id = 1;
  END IF;
  RETURN NULL;
END; $$"""


_TRUNCATE_BUMP_TABLES = frozenset({
    'knowledge_entries',
    'knowledge_revisions',
    'knowledge_revision_access',
    'memory_commands',
})


def _pg_upgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(_PG_BUMP)
    op.execute(_PG_GEN_GUARD)
    op.execute(_PG_DENY)
    op.execute(_PG_TRUNCATE_BUMP)
    # PostgreSQL has no INSERT OR REPLACE; plain constraint violations abort and
    # ON CONFLICT DO NOTHING may bump conservatively.  No conflict-rejecting guard
    # is installed, matching the SQLite contract.
    for table in _SOURCE_TABLES:
        for action, suffix in (('insert', 'ins'), ('update', 'upd'), ('delete', 'del')):
            predicate = _bump_predicate(table, action, probe_conflicts=False)
            when = ('WHEN (' + predicate + ') ') if predicate else ''
            op.execute(
                "CREATE TRIGGER trg_kp_{t}_{s} BEFORE {A} ON {t} FOR EACH ROW {w}"
                "EXECUTE FUNCTION knowledge_projection_bump()".format(
                    t=table, s=suffix, A=action.upper(), w=when,
                )
            )
        if table in _TRUNCATE_BUMP_TABLES:
            op.execute(
                "CREATE TRIGGER trg_kp_notrunc_{t} BEFORE TRUNCATE ON {t} FOR EACH STATEMENT "
                "EXECUTE FUNCTION knowledge_projection_truncate_bump()".format(t=table)
            )
        else:
            op.execute(
                "CREATE TRIGGER trg_kp_notrunc_{t} BEFORE TRUNCATE ON {t} FOR EACH STATEMENT "
                "EXECUTE FUNCTION knowledge_projection_deny_truncate()".format(t=table)
            )
    op.execute(
        "CREATE TRIGGER trg_kp_gen_ins BEFORE INSERT ON knowledge_source_generation "
        "FOR EACH ROW EXECUTE FUNCTION knowledge_projection_generation_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_kp_gen_upd BEFORE UPDATE ON knowledge_source_generation "
        "FOR EACH ROW EXECUTE FUNCTION knowledge_projection_generation_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_kp_gen_del BEFORE DELETE ON knowledge_source_generation "
        "FOR EACH ROW EXECUTE FUNCTION knowledge_projection_generation_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_kp_gen_notrunc BEFORE TRUNCATE ON knowledge_source_generation "
        "FOR EACH STATEMENT EXECUTE FUNCTION knowledge_projection_deny_truncate()"
    )
    for table in ('knowledge_projection_builds', 'knowledge_entry_projections'):
        op.execute(
            "CREATE TRIGGER trg_kp_notrunc_{t} BEFORE TRUNCATE ON {t} FOR EACH STATEMENT "
            "EXECUTE FUNCTION knowledge_projection_deny_truncate()".format(t=table)
        )


def upgrade():
    _create_tables()
    _seed_generation()
    _sqlite_upgrade()
    _pg_upgrade()


def _drop_triggers():
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        for table in _SOURCE_TABLES:
            for name in (
                'trg_kp_{t}_ins', 'trg_kp_{t}_upd', 'trg_kp_{t}_del',
            ):
                op.execute('DROP TRIGGER IF EXISTS ' + name.format(t=table))
        for name in ('trg_kp_gen_ins', 'trg_kp_gen_upd', 'trg_kp_gen_del'):
            op.execute('DROP TRIGGER IF EXISTS ' + name)
    elif bind.dialect.name == 'postgresql':
        for table in _SOURCE_TABLES + ('knowledge_projection_builds', 'knowledge_entry_projections'):
            for name in (
                'trg_kp_{t}_ins', 'trg_kp_{t}_upd', 'trg_kp_{t}_del',
                'trg_kp_notrunc_{t}',
            ):
                op.execute('DROP TRIGGER IF EXISTS ' + name.format(t=table) + ' ON ' + table)
        for name in ('trg_kp_gen_ins', 'trg_kp_gen_upd', 'trg_kp_gen_del', 'trg_kp_gen_notrunc'):
            op.execute('DROP TRIGGER IF EXISTS ' + name + ' ON knowledge_source_generation')
        for function in (
            'knowledge_projection_bump()',
            'knowledge_projection_generation_guard()',
            'knowledge_projection_deny_truncate()',
            'knowledge_projection_truncate_bump()',
        ):
            op.execute('DROP FUNCTION IF EXISTS ' + function)


def downgrade():
    # Removing the triggers silently turns a published projection into stale
    # state, so downgrade runs only as an explicit operator action on a clean
    # tree and never while a head build exists.
    import os
    if op.get_context().as_sql:
        raise RuntimeError('knowledge_projection_downgrade_requires_online_check')
    if os.environ.get('AGT045_PROJECTION_DOWNGRADE_ALLOW') != '1':
        raise RuntimeError('knowledge_projection_downgrade_requires_explicit_allow')
    bind = op.get_bind()
    build_count = bind.execute(sa.text('SELECT COUNT(*) FROM knowledge_projection_builds')).scalar_one()
    head = bind.execute(
        sa.text('SELECT generation, build_id FROM knowledge_source_generation WHERE id = 1')
    ).first()
    if build_count or head is None or head[0] != 1 or head[1] is not None:
        raise RuntimeError('knowledge_projection_downgrade_requires_clean_generation')
    _drop_triggers()
    op.drop_table('knowledge_source_generation')
    op.drop_table('knowledge_entry_projections')
    op.drop_table('knowledge_projection_builds')
