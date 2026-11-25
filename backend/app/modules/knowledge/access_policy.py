"""MEM-003: one fresh scoped policy for content, history, receipts and evidence.

Unverified is an absent row, not an irreversible classification. Publication is
checked by callers separately. Human preview is a router-owned capability and
is never inferred from an actor's otherwise valid employee/worker binding.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from backend.app.modules.audit.models import AuditEvent
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.identity.models import PermissionGrant, RoleAssignment
from backend.app.modules.knowledge.models import KnowledgeEntry, KnowledgeRevision, KnowledgeRevisionAccess, KnowledgeCommand
from backend.app.modules.memory.models import MemoryCommand
from backend.app.modules.memory.policy import classify_content

RANK = {'public': 0, 'restricted': 1, 'hold': 2}
ACCESS_FIELDS = ('revision_id', 'organization_id', 'store_id', 'origin_kind',
                 'origin_memory_id', 'origin_memory_version', 'classification', 'policy_version', 'access_epoch')


def _record(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


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


def inspect_entry(session, entry):
    scope = (entry.organization_id, entry.store_id)
    revisions = list(session.scalars(select(KnowledgeRevision).where(KnowledgeRevision.entry_id == entry.id)
                                   .execution_options(populate_existing=True)))
    policies = list(session.scalars(select(KnowledgeRevisionAccess).where(
        KnowledgeRevisionAccess.revision_id.in_([r.id for r in revisions]))
        .execution_options(populate_existing=True)))
    commands = list(session.scalars(select(KnowledgeCommand).where(
        KnowledgeCommand.organization_id == scope[0], KnowledgeCommand.store_id == scope[1],
        KnowledgeCommand.response["data"]["id"].as_string() == entry.id)))
    promotions = list(session.scalars(select(MemoryCommand).where(
        MemoryCommand.organization_id == scope[0], MemoryCommand.store_id == scope[1], MemoryCommand.operation == 'promote',
        or_(MemoryCommand.response['id'].as_string() == entry.id,
            MemoryCommand.response['knowledge_entry_id'].as_string() == entry.id,
            MemoryCommand.response['current_revision_id'].as_string().in_([r.id for r in revisions])))))
    audits = list(session.scalars(select(AuditEvent).where(AuditEvent.organization_id == scope[0],
        AuditEvent.store_id == scope[1], AuditEvent.target_type == 'knowledge_entry', AuditEvent.target_id == entry.id)))
    facts, conflict = resolve_records(_record(entry), [_record(r) for r in revisions],
        [_record(c) for c in commands], [_record(c) for c in promotions], [_record(a) for a in audits], [_record(p) for p in policies])
    return revisions, policies, facts, conflict


def has_restricted(session, actor, now=None):
    # Refresh the exact existing authorization rows before the established
    # authorize_scope evaluator. An already-loaded allow must not survive a
    # concurrent granted=False update in the same long-lived worker Session.
    # These are scoped reads, not a second permission authority or ACL cache.
    session.flush()
    roles = list(session.scalars(select(RoleAssignment).where(
        RoleAssignment.organization_id == actor.organization_id,
        RoleAssignment.store_id == actor.store_id, RoleAssignment.subject_id == actor.subject_id)
        .execution_options(populate_existing=True)))
    role_keys = [row.role_key for row in roles if row.active]
    list(session.scalars(select(PermissionGrant).where(
        PermissionGrant.organization_id == actor.organization_id,
        PermissionGrant.store_id == actor.store_id,
        PermissionGrant.permission_key.in_(('memory.restricted.read', 'pricing.cost.read')),
        or_(PermissionGrant.subject_id == actor.subject_id, PermissionGrant.role_key.in_(role_keys)))
        .execution_options(populate_existing=True)))
    for key in ('memory.restricted.read', 'pricing.cost.read'):
        try:
            authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id, permission=key, now=now)
            return True
        except HTTPException as error:
            if error.status_code != 403 or error.detail != 'permission_denied':
                raise
    return False


def can_read_reference(session, actor, reference, *, now=None):
    """Current freeform metadata is independent of historical content access."""
    return _content_class(reference) == 'public' or has_restricted(session, actor, now)


def can_read(session, actor, entry, revision, *, now=None, human_preview=None):
    if (revision is None or entry.organization_id != actor.organization_id or entry.store_id != actor.store_id
            or revision.organization_id != entry.organization_id or revision.store_id != entry.store_id or revision.entry_id != entry.id):
        return False
    # Refresh the entry as well as revisions/metadata; a long-lived Session
    # must not preserve a stale current/version/scope after another transaction.
    session.refresh(entry)
    _, policies, facts, conflict = inspect_entry(session, entry)
    if conflict:
        return False
    # A resolvable but absent row is still unverified until migration or explicit
    # verify_source commits the missing trusted facts. No implicit GET writes.
    ancestors = [r for r in session.scalars(select(KnowledgeRevision).where(
        KnowledgeRevision.entry_id == entry.id, KnowledgeRevision.version <= revision.version))]
    def preview():
        from backend.app.modules.knowledge import router
        return router._valid_human_preview(human_preview, actor)
    return _revision_read_decision(_record(revision), [_record(row) for row in ancestors],
        [_record(row) for row in policies], facts, conflict,
        lambda: has_restricted(session, actor, now), preview)


def establish_new(session, entry, revision, *, origin_kind='ordinary', memory_id=None, memory_version=None):
    classification = _content_class(revision.title, revision.body, revision.source_ref, entry.scope_ref)
    if revision.version > 1:
        _, _, facts, conflict = inspect_entry(session, entry)
        previous = [fact for key, fact in facts.items() if key != revision.id]
        if conflict or any(f is None for f in previous) or not previous:
            return None
        parent = previous[-1]
        origin_kind, memory_id, memory_version = parent['origin_kind'], parent['origin_memory_id'], parent['origin_memory_version']
        classification = max(classification, *(f['classification'] for f in previous), key=RANK.get)
    elif origin_kind != 'memory' and str(revision.source_ref or '').startswith('memory:'):
        classification = 'hold'  # a client marker cannot certify a promotion
    row = KnowledgeRevisionAccess(revision_id=revision.id, organization_id=entry.organization_id,
        store_id=entry.store_id, origin_kind=origin_kind, origin_memory_id=memory_id,
        origin_memory_version=memory_version, classification=classification, policy_version=1, access_epoch=1)
    session.add(row)
    session.flush()
    return row


def verify_source(session, actor, entry, *, now):
    revisions, policies, facts, conflict = inspect_entry(session, entry)
    if conflict:
        return False
    existing = {p.revision_id for p in policies}
    for revision in revisions:
        fact = facts.get(revision.id)
        if revision.id in existing or fact is None:
            continue
        session.add(KnowledgeRevisionAccess(**fact))
        record_event(session, actor_id=actor.subject_id, organization_id=entry.organization_id, store_id=entry.store_id,
            action='knowledge.source_verified', target_type='knowledge_entry', target_id=entry.id,
            idempotency_key=f'knowledge-access:{revision.id}:established',
            details={'revision_id': revision.id, 'origin_kind': fact['origin_kind'], 'classification': fact['classification'],
                     'policy_version': 1}, occurred_at=now)
    session.flush()
    return bool(facts) and all(f is not None for f in facts.values())


def tighten_revision_access(session, actor, revision_id, *, expected_epoch, classification, reason, now=None):
    now = now or datetime.now(timezone.utc)
    authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                    permission='knowledge.review', now=now)
    row = session.scalar(select(KnowledgeRevisionAccess).where(KnowledgeRevisionAccess.revision_id == revision_id,
        KnowledgeRevisionAccess.organization_id == actor.organization_id, KnowledgeRevisionAccess.store_id == actor.store_id)
        .execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(404, 'knowledge_not_found')
    if classification not in RANK or RANK[classification] <= RANK[row.classification] or not str(reason).strip():
        raise HTTPException(400, 'knowledge_access_tighten_required')
    old = row.classification
    # Explicit BEGIN protects SQLite nested rollback from legacy SAVEPOINT commits.
    connection = session.connection()
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN')
    with session.begin_nested():
        changed = session.execute(update(KnowledgeRevisionAccess).where(
            KnowledgeRevisionAccess.revision_id == revision_id, KnowledgeRevisionAccess.organization_id == actor.organization_id,
            KnowledgeRevisionAccess.store_id == actor.store_id, KnowledgeRevisionAccess.access_epoch == expected_epoch,
            KnowledgeRevisionAccess.classification == old).values(classification=classification, access_epoch=expected_epoch + 1)
            .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise HTTPException(409, 'knowledge_access_epoch_conflict')
        record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id, store_id=actor.store_id,
            action='knowledge.access_tightened', target_type='knowledge_entry',
            target_id=session.get(KnowledgeRevision, revision_id).entry_id,
            idempotency_key=f'knowledge-access:{revision_id}:epoch:{expected_epoch + 1}',
            details={'revision_id': revision_id, 'old': old, 'new': classification, 'epoch': expected_epoch + 1, 'reason': reason},
            occurred_at=now)
        session.flush()
    session.expire_all()


def project_response(session, actor, response, *, now=None):
    result = deepcopy(response)
    data = result.get('data') if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return result
    entry = session.scalar(select(KnowledgeEntry).where(KnowledgeEntry.id == data.get('id'),
        KnowledgeEntry.organization_id == actor.organization_id, KnowledgeEntry.store_id == actor.store_id)
        .execution_options(populate_existing=True))
    snapshot = data.get('revision')
    revision = session.get(KnowledgeRevision, snapshot.get('id'), populate_existing=True) if isinstance(snapshot, dict) else None
    allowed = (entry is not None and revision is not None and _snapshot_matches(snapshot, _record(revision))
               and can_read(session, actor, entry, revision, now=now))
    if not allowed:
        data['revision'] = None
        data['scope_ref'] = None
    # Freeform historical scope_ref is independently classified even if a legacy
    # current policy only knows a later reference.
    elif _content_class(data.get('scope_ref')) == 'restricted' and not has_restricted(session, actor, now):
        data['scope_ref'] = None
    return result


def _chunks(values, size=200):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def inspect_entries(session, entries):
    """Batch the SAME provenance predicates as inspect_entry.

    Revisions/policies intentionally have NO added scope filter: corrupt foreign
    history is evidence of conflict, never a row to hide. OR-target promotions
    are attached to every matching entry, deduplicated per original row.
    """
    entries = list(entries)
    if not entries:
        return {}
    by_id = {entry.id: entry for entry in entries}
    revisions = {key: [] for key in by_id}
    policies = {key: [] for key in by_id}
    commands = {key: {} for key in by_id}
    promotions = {key: {} for key in by_id}
    audits = {key: {} for key in by_id}
    revision_entry = {}
    for part in _chunks(list(by_id)):
        for row in session.scalars(select(KnowledgeRevision).where(KnowledgeRevision.entry_id.in_(part))
                                   .execution_options(populate_existing=True)):
            revisions[row.entry_id].append(row)
            revision_entry[row.id] = row.entry_id
    for part in _chunks(list(revision_entry)):
        for row in session.scalars(select(KnowledgeRevisionAccess).where(
                KnowledgeRevisionAccess.revision_id.in_(part)).execution_options(populate_existing=True)):
            policies[revision_entry[row.revision_id]].append(row)
    scopes = {}
    for entry in entries:
        scopes.setdefault((entry.organization_id, entry.store_id), []).append(entry.id)
    for scope, identities in scopes.items():
        identity_set = set(identities)
        scope_revisions = [key for key, entry_id in revision_entry.items() if entry_id in identity_set]
        for part in _chunks(identities):
            for row in session.scalars(select(KnowledgeCommand).where(
                    KnowledgeCommand.organization_id == scope[0], KnowledgeCommand.store_id == scope[1],
                    KnowledgeCommand.response['data']['id'].as_string().in_(part))):
                response = row.response
                data = response.get('data') if isinstance(response, dict) else None
                target = data.get('id') if isinstance(data, dict) else None
                if target in identity_set:
                    commands[target][row.id] = row
            for row in session.scalars(select(AuditEvent).where(AuditEvent.organization_id == scope[0],
                    AuditEvent.store_id == scope[1], AuditEvent.target_type == 'knowledge_entry',
                    AuditEvent.target_id.in_(part))):
                audits[row.target_id][row.id] = row
        # Query every original OR arm. Separate bounded IN chunks avoid both
        # SQLite parameter overflow and a cartesian entry/revision batch loop.
        clauses = []
        for part in _chunks(identities):
            clauses.append(or_(MemoryCommand.response['id'].as_string().in_(part),
                               MemoryCommand.response['knowledge_entry_id'].as_string().in_(part)))
        clauses.extend(MemoryCommand.response['current_revision_id'].as_string().in_(part)
                       for part in _chunks(scope_revisions))
        for clause in clauses:
            for row in session.scalars(select(MemoryCommand).where(
                    MemoryCommand.organization_id == scope[0], MemoryCommand.store_id == scope[1],
                    MemoryCommand.operation == 'promote', clause)):
                response = row.response
                if not isinstance(response, dict):
                    continue
                targets = {value for value in (response.get('id'), response.get('knowledge_entry_id'))
                           if isinstance(value, str) and value in identity_set}
                revision_id = response.get('current_revision_id')
                if isinstance(revision_id, str) and revision_entry.get(revision_id) in identity_set:
                    targets.add(revision_entry[revision_id])
                for target in targets:
                    promotions[target][row.id] = row
    result = {}
    for entry in entries:
        rs, ps = revisions[entry.id], policies[entry.id]
        facts, conflict = resolve_records(_record(entry), [_record(row) for row in rs],
            [_record(row) for row in commands[entry.id].values()],
            [_record(row) for row in promotions[entry.id].values()],
            [_record(row) for row in audits[entry.id].values()], [_record(row) for row in ps])
        result[entry.id] = (rs, ps, facts, conflict)
    return result


def _revision_read_decision(revision, ancestors, policies, facts, conflict, restricted, preview):
    """One pure policy decision shared by the single-row and batch paths."""
    if conflict:
        return False
    ancestor_ids = {row['id'] for row in ancestors}
    if any(row['revision_id'] in ancestor_ids and row['classification'] == 'hold' for row in policies):
        return False
    policy_ids = {row['revision_id'] for row in policies}
    fact = facts.get(revision['id']) if ancestor_ids.issubset(policy_ids) else None
    if fact is None:
        return preview() and restricted()
    return (fact['classification'] == 'public'
            or fact['classification'] == 'restricted' and restricted())


def can_read_prepared(actor, entry, revision, inspection, *, restricted, human_preview=None):
    """Pure provenance decision over Host-loaded facts, not a permission grant."""
    if (revision is None or entry.organization_id != actor.organization_id or entry.store_id != actor.store_id
            or revision.organization_id != entry.organization_id or revision.store_id != entry.store_id
            or revision.entry_id != entry.id):
        return False
    revisions, policies, facts, conflict = inspection
    def preview():
        from backend.app.modules.knowledge import router
        return router._valid_human_preview(human_preview, actor)
    return _revision_read_decision(_record(revision),
        [_record(row) for row in revisions if row.version <= revision.version],
        [_record(row) for row in policies], facts, conflict, restricted, preview)


def projection_entry_state(entry, revision, revisions, policies, facts, conflict=False):
    """Pure neutral read class for one entry, shared by build and read paths.

    This is the projection-safe projection of ``_revision_read_decision``: it
    never consults a principal, so the result can be stored for any caller and
    later combined with live permission facts.  A missing current revision or an
    unresolved fact chain is explicitly ``missing``/``unverified`` instead of
    being folded into ``public``.
    """
    scope_reference_class = _content_class(entry.get('scope_ref'))
    if revision is None:
        return {
            'read_class': 'missing', 'cause': 'current_revision_missing',
            'policy_version': 1, 'access_epoch': 1,
            'scope_reference_class': scope_reference_class,
            'ancestor_hold_ids': [], 'ancestor_fact_complete': False,
        }
    ancestors = [row for row in revisions if row['version'] <= revision['version']]
    ancestor_ids = {row['id'] for row in ancestors}
    hold_ids = sorted(row['revision_id'] for row in policies
                      if row['revision_id'] in ancestor_ids and row['classification'] == 'hold')
    policy_ids = {row['revision_id'] for row in policies}
    covered = ancestor_ids.issubset(policy_ids)
    fact = facts.get(revision['id']) if covered else None
    if fact is not None and fact.get('classification') == 'hold' and not hold_ids:
        hold_ids = [revision['id']]
    if conflict:
        read_class, cause = 'conflict', 'provenance_conflict'
    elif hold_ids:
        read_class, cause = 'hold', 'ancestor_hold'
    elif fact is None:
        read_class, cause = 'unverified', 'ancestor_unresolved'
    else:
        read_class = fact['classification']
        cause = 'resolved' if read_class != 'hold' else 'resolved_hold'
    current_policy = next((row for row in policies if row['revision_id'] == revision['id']), None)
    return {
        'read_class': read_class,
        'cause': cause,
        'policy_version': fact['policy_version'] if fact else (current_policy or {}).get('policy_version', 1),
        'access_epoch': fact['access_epoch'] if fact else (current_policy or {}).get('access_epoch', 1),
        'scope_reference_class': scope_reference_class,
        'ancestor_hold_ids': hold_ids,
        'ancestor_fact_complete': covered and fact is not None,
    }


def projection_row_visible(row, *, restricted: bool) -> bool:
    """Shared pure read-side visibility for one projection row.

    The stored class is target-local and principal-free; the only live input is
    whether the authenticated principal currently holds the restricted grant.
    """
    read_class = row['read_class'] if isinstance(row, dict) else row.read_class
    reference_class = (row['scope_reference_class'] if isinstance(row, dict)
                       else row.scope_reference_class)
    if read_class not in ('public', 'restricted'):
        return False
    return ((read_class == 'public' or restricted)
            and (reference_class == 'public' or restricted))
