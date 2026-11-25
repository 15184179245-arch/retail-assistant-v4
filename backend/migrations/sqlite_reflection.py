"""Narrow SQLite inline-FK reflection repair, never a schema-diff filter.

SQLite's PRAGMA catalog is authoritative for action values omitted by the
installed SQLAlchemy inspector for the two 0069 ADD COLUMN references.
Only this connection's dialect instance is wrapped, then restored on every exit.
"""
from contextlib import contextmanager
from copy import deepcopy

_TARGETS = {"assistant_turns", "assistant_turn_authorities"}
_ACTIONS = {"NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"}


@contextmanager
def preserve_inline_fk_actions(connection):
    if connection.dialect.name != "sqlite":
        yield
        return
    dialect = connection.dialect
    original = dialect.get_foreign_keys
    had_override = "get_foreign_keys" in dialect.__dict__
    previous_override = dialect.__dict__.get("get_foreign_keys")

    def get_foreign_keys(current, table_name, schema=None, **kw):
        reflected = original(current, table_name, schema=schema, **kw)
        if (current.engine is not connection.engine or table_name not in _TARGETS
                or schema not in (None, "main")):
            return reflected
        # Identifiers are restricted above, not supplied by callers or models.
        groups = {}
        for row in current.exec_driver_sql(
                f'PRAGMA main.foreign_key_list("{table_name}")').mappings():
            groups.setdefault(row["id"], []).append(dict(row))
        result = deepcopy(reflected)
        for fk in result:
            if (fk["constrained_columns"] != ["source_inbox_id"]
                    or fk["referred_table"] != "assistant_inbox"
                    or fk["referred_columns"] != ["id"]
                    or fk.get("referred_schema") not in (None, "main")):
                continue
            matches = [rows for rows in groups.values()
                       if len(rows) == 1 and rows[0]["seq"] == 0
                       and rows[0]["from"] == "source_inbox_id"
                       and rows[0]["table"] == "assistant_inbox"
                       and rows[0]["to"] == "id"]
            if len(matches) != 1:
                raise RuntimeError("sqlite_inline_fk_reflection_ambiguous")
            row = matches[0][0]
            for option, field in (("ondelete", "on_delete"), ("onupdate", "on_update")):
                action = str(row[field]).upper()
                if action not in _ACTIONS:
                    raise RuntimeError("sqlite_inline_fk_reflection_unknown_action")
                # Copy actual actions, including CASCADE/NO ACTION. Never invent RESTRICT.
                fk.setdefault("options", {})[option] = action
        return result

    dialect.get_foreign_keys = get_foreign_keys
    try:
        yield
    finally:
        if had_override:
            dialect.get_foreign_keys = previous_override
        else:
            del dialect.get_foreign_keys
