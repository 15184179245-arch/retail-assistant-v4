"""Portable notes over Host memory, not another memory database or authority.

The frontmatter is a deliberately small YAML subset: fixed keys and JSON
scalars. No YAML loader, paths, link fetches, executable tags, or plugins.
Import edits an existing visible memory through the ordinary audited service.
"""
import json
from uuid import UUID

from backend.app.modules.memory import service

FORMAT = 'v4-personal-memory-1'
MAX_BYTES = 32768
KEYS = ('format', 'memory_id', 'organization_id', 'store_id', 'owner_id',
        'version', 'source', 'purpose', 'retain_until_withdrawn', 'expires_at', 'authority')


def _metadata(actor, item):
    return dict(format=FORMAT, memory_id=item['id'], organization_id=actor.organization_id,
                store_id=actor.store_id, owner_id=actor.subject_id, version=item['version'],
                source=item['source'], purpose=item['purpose'],
                retain_until_withdrawn=item['retain_until_withdrawn'],
                expires_at=item['expires_at'].isoformat() if item['expires_at'] else None,
                authority='reference_only')


def export_note(session, actor, memory_id, *, now=None):
    item = service.get_memory(session, actor, memory_id, now=now)
    # Host IDs are UUIDs, never use caller text or note titles as a path.
    filename = 'memory-' + str(UUID(item['id'])) + '.md'
    metadata = _metadata(actor, item)
    header = '\n'.join(key + ': ' + json.dumps(metadata[key], ensure_ascii=False) for key in KEYS)
    return dict(filename=filename, markdown='---\n' + header + '\n---\n\n' + item['content'] + '\n',
                memory_id=item['id'], version=item['version'], authority='reference_only')


def _parse(markdown):
    invalid = service.MemoryError('memory_markdown_invalid')
    if (not isinstance(markdown, str) or len(markdown) > MAX_BYTES
            or '\x00' in markdown or any(0xD800 <= ord(c) <= 0xDFFF for c in markdown)):
        raise invalid
    if len(markdown.encode('utf-8')) > MAX_BYTES:
        raise invalid
    markdown = markdown.replace('\r\n', '\n')
    if not markdown.startswith('---\n'):
        raise invalid
    header, separator, body = markdown[4:].partition('\n---\n')
    if not separator or len(header) > 4096 or not 1 <= len(body.strip()) <= 5000:
        raise invalid
    metadata = {}
    try:
        for line in header.splitlines():
            key, sep, value = line.partition(': ')
            if not sep or key not in KEYS or key in metadata:
                raise invalid
            metadata[key] = json.loads(value)
    except (json.JSONDecodeError, RecursionError) as error:
        raise invalid from error
    if set(metadata) != set(KEYS):
        raise invalid
    # Leave room for the next version in PostgreSQL's signed INTEGER as well.
    if (type(metadata['version']) is not int or not 1 <= metadata['version'] < 2**31 - 1
            or type(metadata['retain_until_withdrawn']) is not bool
            or metadata['expires_at'] is not None and type(metadata['expires_at']) is not str
            or any(type(metadata[key]) is not str for key in KEYS
                   if key not in ('version', 'retain_until_withdrawn', 'expires_at'))):
        raise invalid
    if metadata['format'] != FORMAT or metadata['authority'] != 'reference_only':
        raise invalid
    if any(isinstance(value, str) and ('\x00' in value or any(
            0xD800 <= ord(c) <= 0xDFFF for c in value)) for value in metadata.values()):
        raise invalid
    return metadata, body.strip()


def import_note(session, actor, *, markdown, idempotency_key, now=None):
    current = service._now(now)
    service._require(session, actor, service.READ, current)
    service._require(session, actor, service.WRITE, current)
    metadata, body = _parse(markdown)
    if (metadata['organization_id'], metadata['store_id'], metadata['owner_id']) != (
            actor.organization_id, actor.store_id, actor.subject_id):
        raise service.MemoryError('memory_not_found')
    item = service.get_memory(session, actor, metadata['memory_id'], now=current)
    expected = _metadata(actor, item)
    # Version is consumed by the service CAS/receipt, not a preflight CAS that
    # would break idempotent retries. All other header fields are immutable here.
    if any(metadata[key] != expected[key] for key in KEYS if key != 'version'):
        raise service.MemoryError('memory_markdown_metadata_conflict')
    return service.correct_memory(session, actor, item['id'], content=body,
                                  expected_version=metadata['version'],
                                  idempotency_key=idempotency_key, now=current)
