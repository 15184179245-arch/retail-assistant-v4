"""Frozen MEM-003 provenance backfill and monotonic confidentiality guards.

No current model/policy imports: this version's evidence parser and classifier
are copied intentionally and tested against the runtime matrix. This migration
never changes old knowledge, command receipt, or audit rows.
"""
from datetime import datetime, timezone
import re
from alembic import op
import sqlalchemy as sa

revision = '0053_knowledge_revision_access'
down_revision = '0052_agent_answer_own_evidence'
branch_labels = None
depends_on = None

RANK = {'public': 0, 'restricted': 1, 'hold': 2}
RESTRICTED_KEYWORDS = frozenset(['供货商返点', '内部折扣', '协议价', '回扣', '底价', '底薪', '成本价', '批发特价', '提成', '毛利', '薪资', '谈判扣点', '返点', '进价', '采购成本'])

def classify_content(content):
    return {"is_restricted": any(k in content for k in RESTRICTED_KEYWORDS)}



def _time(value):
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None

def _snapshot_matches(snapshot, revision):
    if not isinstance(snapshot, dict):
        return False
    fields = ('id', 'version', 'title', 'body', 'source_type', 'source_ref', 'submitted_by')
    return (all(type(snapshot.get(k)) is type(revision.get(k)) and snapshot.get(k) == revision.get(k) for k in fields)
            and _time(snapshot.get('submitted_at')) is not None
            and _time(snapshot.get('submitted_at')) == _time(revision.get('submitted_at')))

def _content_class(*values):
    return 'restricted' if classify_content('\n'.join(str(v or '') for v in values))['is_restricted'] else 'public'

def resolve_records(entry, revisions, commands, promotions, audits, policies):
    """Pure version-1 resolver. Historical migration freezes an independent copy.

    Returns (facts by revision, structural/conflicting-origin failure). None
    facts are genuinely unverified and must not be persisted on ordinary reads.
    """
    revisions = sorted(revisions, key=lambda r: r['version'])
    if (not revisions or [r['version'] for r in revisions] != list(range(1, len(revisions) + 1))
            or entry.get('current_revision_id') != revisions[-1]['id']
            or entry.get('version', 0) < revisions[-1]['version']):
        return {}, True
    scope = (entry['organization_id'], entry['store_id'])
    if any((r['organization_id'], r['store_id']) != scope or r['entry_id'] != entry['id']
           or _time(r['submitted_at']) is None or not r.get('title', '').strip()
           or not r.get('body', '').strip() for r in revisions):
        return {}, True
    by_id = {r['id']: r for r in revisions}
    if len(by_id) != len(revisions):
        return {}, True
    existing = {p['revision_id']: p for p in policies}
    origins = set()
    for p in policies:
        if (p['organization_id'], p['store_id']) != scope or p['revision_id'] not in by_id:
            return {}, True
        if p['origin_kind'] != 'unknown':
            origins.add((p['origin_kind'], p['origin_memory_id'], p['origin_memory_version']))
    if len(origins) > 1:
        return {}, True
    origin = next(iter(origins)) if origins else None
    memory_ids = set()
    for command in promotions:
        response = command.get('response')
        if not isinstance(response, dict):
            continue
        targeted = (response.get('id') == entry['id'] or response.get('knowledge_entry_id') == entry['id']
                    or response.get('current_revision_id') in by_id)
        if not targeted:
            continue
        first = revisions[0]
        if (command.get('operation') != 'promote'
                or (command.get('organization_id'), command.get('store_id')) != scope
                or command.get('actor_id') != first['submitted_by']
                or response.get('id') != entry['id'] or response.get('knowledge_entry_id') != entry['id']
                or response.get('current_revision_id') != first['id']
                or response.get('submitted_by') != first['submitted_by']
                or not isinstance(response.get('memory_id'), str) or not response['memory_id']
                or not re.fullmatch('[0-9a-f]{64}', str(command.get('payload_digest', '')))):
            return {}, True
        memory_ids.add(response['memory_id'])
    if len(memory_ids) > 1:
        return {}, True
    if memory_ids:
        memory_id = next(iter(memory_ids))
        if origin and (origin[0] != 'memory' or origin[1] != memory_id):
            return {}, True
        origin = origin or ('memory', memory_id, None)
    snapshots = {r['id']: [] for r in revisions}
    ordinary_submit = False
    matched_commands = set()
    for command in commands:
        response = command.get('response')
        data = response.get('data') if isinstance(response, dict) else None
        if not isinstance(data, dict) or data.get('id') != entry['id']:
            continue
        snapshot = data.get('revision')
        # Old protected receipts may legitimately have no raw revision. They
        # cannot establish provenance; they also never override stronger proof.
        if snapshot is None:
            continue
        revision = by_id.get(snapshot.get('id')) if isinstance(snapshot, dict) else None
        if (revision is None or not _snapshot_matches(snapshot, revision)
                or (command.get('organization_id'), command.get('store_id')) != scope
                or (data.get('organization_id'), data.get('store_id')) != scope
                or not re.fullmatch('[0-9a-f]{64}', str(command.get('payload_digest', '')))):
            return {}, True
        snapshots[revision['id']].append(data)
        op = command['operation']
        if op in ('submit', 'revise'):
            if command.get('actor_id') != revision['submitted_by']:
                return {}, True
            matched_commands.add((op, revision['id']))
        if op == 'submit':
            if revision['version'] != 1 or revision['submitted_by'] != entry['submitted_by']:
                return {}, True
            ordinary_submit = True
    if ordinary_submit and origin and origin[0] == 'memory':
        return {}, True
    if ordinary_submit and origin is None:
        origin = ('ordinary', None, None)
    suspicious_marker = any(str(r.get('source_ref') or '').startswith('memory:') for r in revisions)
    if suspicious_marker and (origin is None or origin[0] != 'memory'):
        return {r['id']: None for r in revisions}, False

    def audit_matches(revision):
        action = 'knowledge.submitted' if revision['version'] == 1 else 'knowledge.revised'
        key = f"knowledge:{entry['id']}:{action}:{revision['id']}"
        for audit in audits:
            if ((audit.get('organization_id'), audit.get('store_id')) != scope
                    or audit.get('target_id') != entry['id'] or audit.get('target_type') != 'knowledge_entry'
                    or audit.get('actor_id') != revision['submitted_by']
                    or _time(audit.get('occurred_at')) != _time(revision['submitted_at'])):
                continue
            details = audit.get('details')
            if (audit.get('action') == action and audit.get('idempotency_key') == key
                    and isinstance(details, dict) and type(details.get('version')) is int
                    and details['version'] >= revision['version'] and details.get('status') == 'pending_review'):
                return True
            if revision['version'] == 1 and revision['source_type'] == 'import' and audit.get('action') == 'knowledge.imported':
                ref = details.get('source_ref') if isinstance(details, dict) else None
                if (isinstance(ref, str) and ref and ref[:500] == revision.get('source_ref')
                        and audit.get('idempotency_key') == 'knowledge-import:' + ref[:150]):
                    return True
        return False

    if origin is None and audit_matches(revisions[0]):
        origin = ('legacy_record', None, None)
    facts = {}
    unresolved = False
    strongest = 'public'
    for revision in revisions:
        old = existing.get(revision['id'])
        if old:
            classification = old['classification']
            if old['origin_kind'] == 'unknown':
                classification = 'hold'
        elif not origin or (revision['version'] > 1 and not audit_matches(revision)):
            classification = None
        else:
            classification = _content_class(revision['title'], revision['body'], revision.get('source_ref'))
            for snapshot in snapshots[revision['id']]:
                classification = max(classification, _content_class(snapshot.get('scope_ref')), key=RANK.get)
            # Only current scope_ref is available without an immutable receipt;
            # conservatively tighten all descendants instead of inventing old refs.
            classification = max(classification, _content_class(entry.get('scope_ref')), key=RANK.get)
        if classification is None:
            unresolved = True
            facts[revision['id']] = None
            continue
        strongest = max(strongest, classification, key=RANK.get)
        if unresolved:
            facts[revision['id']] = None
        else:
            o = (old['origin_kind'], old['origin_memory_id'], old['origin_memory_version']) if old else origin
            facts[revision['id']] = dict(revision_id=revision['id'], organization_id=scope[0], store_id=scope[1],
                origin_kind=o[0], origin_memory_id=o[1], origin_memory_version=o[2],
                classification=strongest, policy_version=1, access_epoch=old['access_epoch'] if old else 1)
    return facts, False


def guard_sql(dialect):
    if dialect == 'sqlite':
        changed = ' OR '.join('NEW.' + field + ' IS NOT OLD.' + field for field in
            ('revision_id','organization_id','store_id','origin_kind','origin_memory_id','origin_memory_version','policy_version'))
        return [
            "CREATE TRIGGER trg_knowledge_access_delete BEFORE DELETE ON knowledge_revision_access BEGIN SELECT RAISE(ABORT, 'knowledge access is immutable'); END",
            "CREATE TRIGGER trg_knowledge_access_update BEFORE UPDATE ON knowledge_revision_access WHEN " + changed +
            " OR NEW.access_epoch <> OLD.access_epoch + 1 OR NOT ((OLD.classification = 'public' AND NEW.classification IN ('restricted','hold')) OR (OLD.classification = 'restricted' AND NEW.classification = 'hold')) BEGIN SELECT RAISE(ABORT, 'knowledge access must only tighten'); END",
        ]
    if dialect == 'postgresql':
        return ["""CREATE FUNCTION sx_mem003_access_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'knowledge access is immutable' USING ERRCODE='23000';
          END IF;
          IF (to_jsonb(NEW)-'classification'-'access_epoch') IS DISTINCT FROM (to_jsonb(OLD)-'classification'-'access_epoch')
             OR NEW.access_epoch <> OLD.access_epoch + 1
             OR NOT ((OLD.classification='public' AND NEW.classification IN ('restricted','hold'))
                     OR (OLD.classification='restricted' AND NEW.classification='hold')) THEN
            RAISE EXCEPTION 'knowledge access must only tighten' USING ERRCODE='23000';
          END IF;
          RETURN NEW;
        END; $$""",
        "CREATE TRIGGER trg_knowledge_access_guard BEFORE UPDATE OR DELETE ON knowledge_revision_access FOR EACH ROW EXECUTE FUNCTION sx_mem003_access_guard()"]
    raise RuntimeError('unsupported knowledge access dialect')


def upgrade():
    op.create_table('knowledge_revision_access',
        sa.Column('revision_id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('organization_id', sa.String(36), nullable=False),
        sa.Column('store_id', sa.String(36), nullable=False),
        sa.Column('origin_kind', sa.String(16), nullable=False),
        sa.Column('origin_memory_id', sa.String(36), nullable=True),
        sa.Column('origin_memory_version', sa.Integer(), nullable=True),
        sa.Column('classification', sa.String(16), nullable=False),
        sa.Column('policy_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('access_epoch', sa.Integer(), nullable=False, server_default='1'),
        sa.ForeignKeyConstraint(['organization_id','store_id','revision_id'],
            ['knowledge_revisions.organization_id','knowledge_revisions.store_id','knowledge_revisions.id'],
            name='fk_knowledge_access_revision_scope', ondelete='RESTRICT'),
        sa.CheckConstraint("origin_kind IN ('ordinary','memory','legacy_record','unknown')", name='ck_knowledge_access_origin'),
        sa.CheckConstraint("classification IN ('public','restricted','hold')", name='ck_knowledge_access_class'),
        sa.CheckConstraint('policy_version = 1 AND access_epoch > 0', name='ck_knowledge_access_epoch'),
        sa.CheckConstraint('origin_memory_version IS NULL OR origin_memory_version > 0', name='ck_knowledge_access_memory_version'),
        sa.CheckConstraint("(origin_kind = 'memory' AND origin_memory_id IS NOT NULL AND length(trim(origin_memory_id)) > 0) OR (origin_kind <> 'memory' AND origin_memory_id IS NULL AND origin_memory_version IS NULL)", name='ck_knowledge_access_memory_origin'),
        sa.CheckConstraint("origin_kind <> 'unknown' OR classification = 'hold'", name='ck_knowledge_access_unknown_hold'))
    connection = op.get_bind()
    metadata = sa.MetaData()
    names = ('knowledge_entries','knowledge_revisions','knowledge_commands','memory_commands','audit_events','knowledge_revision_access')
    tables = {name: sa.Table(name, metadata, autoload_with=connection) for name in names}
    rows = {name: [dict(row) for row in connection.execute(sa.select(table)).mappings()]
            for name, table in tables.items() if name != 'knowledge_revision_access'}
    for entry in sorted(rows['knowledge_entries'], key=lambda r: (r['organization_id'],r['store_id'],r['id'])):
        revisions = [r for r in rows['knowledge_revisions'] if r['entry_id'] == entry['id']]
        scope = (entry['organization_id'],entry['store_id'])
        scoped = lambda records: [r for r in records if (r['organization_id'],r['store_id']) == scope]
        facts, conflict = resolve_records(entry, revisions, scoped(rows['knowledge_commands']),
            scoped(rows['memory_commands']), scoped(rows['audit_events']), [])
        for revision_row in sorted(revisions, key=lambda r: r['version']):
            fact = facts.get(revision_row['id'])
            if conflict:
                fact = dict(revision_id=revision_row['id'], organization_id=revision_row['organization_id'],
                    store_id=revision_row['store_id'], origin_kind='unknown', origin_memory_id=None,
                    origin_memory_version=None, classification='hold', policy_version=1, access_epoch=1)
            if fact is not None:
                connection.execute(tables['knowledge_revision_access'].insert().values(**fact))
    for statement in guard_sql(connection.dialect.name):
        op.execute(statement)


def downgrade():
    connection = op.get_bind()
    # Even a metadata-empty deployment must not silently lose protections while
    # original unverified knowledge exists. Nonempty data is forward-only.
    for table in ('knowledge_entries','knowledge_revisions','knowledge_revision_access'):
        if connection.execute(sa.text('SELECT 1 FROM ' + table + ' LIMIT 1')).first() is not None:
            raise RuntimeError('knowledge access downgrade refused: original knowledge or metadata exists')
    if connection.dialect.name == 'postgresql':
        op.execute('DROP TRIGGER trg_knowledge_access_guard ON knowledge_revision_access')
        op.execute('DROP FUNCTION sx_mem003_access_guard()')
    elif connection.dialect.name == 'sqlite':
        op.execute('DROP TRIGGER trg_knowledge_access_update')
        op.execute('DROP TRIGGER trg_knowledge_access_delete')
    op.drop_table('knowledge_revision_access')
