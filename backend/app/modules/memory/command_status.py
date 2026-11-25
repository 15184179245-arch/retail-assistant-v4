"""Read committed operation metadata without replaying writes or old content."""
from sqlalchemy import select

from backend.app.modules.memory import service
from backend.app.modules.memory.models import MemoryCommand


def read_status(session, actor, *, operation, key, now=None):
    current = service._now(now)
    service._require(session, actor, service.READ, current)
    service._require(session, actor, service.WRITE, current)
    if operation not in ('save', 'correct', 'withdraw') or not isinstance(key, str) or not 1 <= len(key) <= 200 or key != key.strip():
        raise service.MemoryError('memory_command_query_invalid')
    command = session.scalar(select(MemoryCommand).where(
        MemoryCommand.organization_id == actor.organization_id,
        MemoryCommand.store_id == actor.store_id,
        MemoryCommand.actor_id == actor.subject_id,
        MemoryCommand.operation == operation,
        MemoryCommand.idempotency_key == key))
    result = dict(operation=operation, key=key, status='unobserved',
                  memory_id=None, current_version=None)
    if command is None:
        # An in-flight transaction may still commit. Absence is not a retry permit.
        return result
    result['status'] = 'committed'
    # Historical response content must never be returned, even to its author.
    identity = command.response.get('id')
    if not isinstance(identity, str):
        raise service.MemoryError('memory_command_receipt_invalid')
    try:
        row = service._row(session, actor, identity, current)
    except service.MemoryError as error:
        if error.code != 'memory_not_found':
            raise
    else:
        result.update(memory_id=row.id, current_version=row.version)
    return result
