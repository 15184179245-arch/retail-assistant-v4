"""Workspace's single optimistic-write and create-transaction boundary.

No commits, implicit retries, or authority decisions. Caller owns rollback.
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from backend.app.modules.workspace.models import WorkspaceArtifact, WorkspaceThreadBinding


class WriteConflict(Exception):
    def __init__(self, current_version=None):
        self.current_version = current_version


def compare_and_swap(session, row, *, expected_version, values):
    if type(row) not in (WorkspaceArtifact, WorkspaceThreadBinding):
        raise TypeError("workspace_cas_row_type")
    if type(expected_version) is not int or expected_version < 1:
        raise WriteConflict()
    if set(values) & {"id", "organization_id", "store_id", "owner_subject_id",
                      "subject_id", "version"}:
        raise ValueError("workspace_cas_identity_change")
    model = type(row)
    predicates = [model.id == row.id, model.organization_id == row.organization_id,
                  model.store_id == row.store_id]
    if model is WorkspaceArtifact:
        predicates += [model.owner_subject_id == row.owner_subject_id]
        live = [model.status == "draft"]
    else:
        predicates += [model.subject_id == row.subject_id]
        live = []
    with session.no_autoflush:
        changed = session.execute(update(model).where(
            *predicates, *live, model.version == expected_version
        ).values(**values, version=expected_version + 1)
          .execution_options(synchronize_session=False)).rowcount
        if changed != 1:
            current = session.scalar(select(model.version).where(*predicates))
            raise WriteConflict(current)
        session.expire(row)
        session.refresh(row)


def begin_create(session):
    """Ensure SAVEPOINT cannot commit SQLite work outside a physical root.

    Only the create command serializes its absent-key read on SQLite. An
    existing caller transaction is retained, never committed or replaced.
    """
    connection = session.connection()
    if connection.dialect.name != "sqlite":
        return
    raw = connection.connection.driver_connection
    if connection.in_nested_transaction():
        raise RuntimeError("workspace_create_nested_caller_unsupported")
    if not raw.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def is_write_contention(error):
    original = getattr(error, "orig", None)
    code = getattr(original, "sqlite_errorcode", None)
    return isinstance(error, OperationalError) and code is not None and (code & 255) in (5, 6)
