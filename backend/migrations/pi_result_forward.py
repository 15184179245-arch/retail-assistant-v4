"""Narrow SQLite result-kind forward boundary, not an application capability grant.

Expected DDL below is compiled only from frozen original migration 0076
(sha256 878be99574897564b5c00ac35c0e01a92b0437d593ff20a062af53934dec4c76).
Never learn expected constraints from the database being migrated.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import re
import struct

import alembic

PREDECESSOR = "0078_merge_workspace_pi"
REVISION = "0079_pi_knowledge_tool_error"
TABLE = "agent_pi_tool_results"
SHADOW = "agent_pi_tool_results_0079_shadow"
WORKSPACE = "0054_thread_workspace_artifacts"
PROJECTION = "0077_knowledge_projection_gen"
OLD_KIND = "result_kind IN ('success','tool_error') AND (result_kind <> 'tool_error' OR tool_key = 'memory.search')"
NEW_KIND = "result_kind IN ('success','tool_error') AND (result_kind <> 'tool_error' OR tool_key IN ('memory.search','knowledge.search'))"

OLD_CREATE_SQL = "CREATE TABLE agent_pi_tool_results (\n    id VARCHAR(36) NOT NULL, \n    record_version INTEGER NOT NULL, \n    result_abi VARCHAR(32) NOT NULL, \n    organization_id VARCHAR(36) NOT NULL, \n    store_id VARCHAR(36) NOT NULL, \n    requester_id VARCHAR(36) NOT NULL, \n    agent_id VARCHAR(36) NOT NULL, \n    run_id VARCHAR(36) NOT NULL, \n    model_step_id VARCHAR(36) NOT NULL, \n    tool_step_id VARCHAR(36) NOT NULL, \n    model_kind VARCHAR(16) NOT NULL, \n    tool_kind VARCHAR(16) NOT NULL, \n    pi_receipt_id VARCHAR(36) NOT NULL, \n    native_call_id VARCHAR(100) NOT NULL, \n    tool_key VARCHAR(120) NOT NULL, \n    tool_name VARCHAR(120) NOT NULL, \n    tool_version VARCHAR(120) NOT NULL, \n    toolset_version VARCHAR(200) NOT NULL, \n    input_digest VARCHAR(64) NOT NULL, \n    execution_epoch BIGINT NOT NULL, \n    runtime_record_sha256 VARCHAR(64) NOT NULL, \n    result_kind VARCHAR(16) NOT NULL, \n    output_ref VARCHAR(71) NOT NULL, \n    payload_sha256 VARCHAR(64) NOT NULL, \n    source_refs_sha256 VARCHAR(64) NOT NULL, \n    canonical_ref VARCHAR(71) NOT NULL, \n    canonical_json TEXT NOT NULL, \n    created_at DATETIME NOT NULL, \n    PRIMARY KEY (id), \n    CONSTRAINT ck_pi_result_body CHECK (length(native_call_id) BETWEEN 1 AND 100 AND length(canonical_json) BETWEEN 1 AND 262144), \n    CONSTRAINT ck_pi_result_contract CHECK (record_version = 1 AND result_abi = 'pi-tool-result/1'), \n    CONSTRAINT ck_pi_result_digests CHECK (length(input_digest) = 64 AND length(runtime_record_sha256) = 64 AND length(payload_sha256) = 64 AND length(source_refs_sha256) = 64 AND length(toolset_version) = 71 AND substr(toolset_version,1,7) = 'sha256:' AND output_ref = 'sha256:' || payload_sha256 AND length(canonical_ref) = 71 AND substr(canonical_ref,1,7) = 'sha256:'), \n    CONSTRAINT ck_pi_result_identity CHECK (model_kind = 'model' AND tool_kind = 'tool' AND execution_epoch > 0), \n    CONSTRAINT ck_pi_result_kind CHECK (result_kind IN ('success','tool_error') AND (result_kind <> 'tool_error' OR tool_key = 'memory.search')), \n    CONSTRAINT ck_pi_result_tool CHECK (tool_version = '1' AND ((tool_key = 'tasks.list_mine' AND tool_name = 'v4_tasks_list_mine_v1') OR (tool_key = 'knowledge.search' AND tool_name = 'v4_knowledge_search_v1') OR (tool_key = 'catalog.search' AND tool_name = 'v4_catalog_search_v1') OR (tool_key = 'memory.search' AND tool_name = 'v4_memory_search_v1'))), \n    CONSTRAINT fk_pi_result_agent FOREIGN KEY(organization_id, agent_id) REFERENCES subjects (organization_id, id) ON DELETE RESTRICT, \n    CONSTRAINT fk_pi_result_model_step FOREIGN KEY(organization_id, store_id, run_id, model_step_id, model_kind) REFERENCES agent_steps (organization_id, store_id, run_id, id, kind) ON DELETE RESTRICT, \n    CONSTRAINT fk_pi_result_native_scope FOREIGN KEY(organization_id, store_id, run_id, native_call_id) REFERENCES agent_pi_tool_receipts (organization_id, store_id, run_id, native_call_id) ON DELETE RESTRICT, \n    CONSTRAINT fk_pi_result_receipt_id FOREIGN KEY(pi_receipt_id) REFERENCES agent_pi_tool_receipts (id) ON DELETE RESTRICT, \n    CONSTRAINT fk_pi_result_requester FOREIGN KEY(organization_id, requester_id) REFERENCES subjects (organization_id, id) ON DELETE RESTRICT, \n    CONSTRAINT fk_pi_result_run_scope FOREIGN KEY(organization_id, store_id, run_id) REFERENCES agent_runs (organization_id, store_id, id) ON DELETE RESTRICT, \n    CONSTRAINT fk_pi_result_tool_step FOREIGN KEY(organization_id, store_id, run_id, tool_step_id, tool_kind) REFERENCES agent_steps (organization_id, store_id, run_id, id, kind) ON DELETE RESTRICT, \n    CONSTRAINT uq_pi_result_native UNIQUE (organization_id, store_id, run_id, native_call_id), \n    CONSTRAINT uq_pi_result_receipt UNIQUE (pi_receipt_id), \n    CONSTRAINT uq_pi_result_tool_step UNIQUE (organization_id, store_id, run_id, tool_step_id)\n)"
INDEX_SQL = 'CREATE INDEX ix_pi_result_run ON agent_pi_tool_results (organization_id, store_id, run_id, created_at)'
TRIGGER_SQL = {'trg_pi_tool_result_no_insert_conflict': "CREATE TRIGGER trg_pi_tool_result_no_insert_conflict\n            BEFORE INSERT ON agent_pi_tool_results\n            WHEN EXISTS (SELECT 1 FROM agent_pi_tool_results r WHERE\n                r.id = NEW.id OR r.pi_receipt_id = NEW.pi_receipt_id OR\n                (r.organization_id = NEW.organization_id AND r.store_id = NEW.store_id\n                 AND r.run_id = NEW.run_id AND r.native_call_id = NEW.native_call_id) OR\n                (r.organization_id = NEW.organization_id AND r.store_id = NEW.store_id\n                 AND r.run_id = NEW.run_id AND r.tool_step_id = NEW.tool_step_id))\n            BEGIN SELECT RAISE(ABORT, 'pi_tool_result_conflict'); END", 'trg_pi_tool_result_no_update': "CREATE TRIGGER trg_pi_tool_result_no_update BEFORE UPDATE ON agent_pi_tool_results BEGIN SELECT RAISE(ABORT, 'pi_tool_result_immutable'); END", 'trg_pi_tool_result_no_delete': "CREATE TRIGGER trg_pi_tool_result_no_delete BEFORE DELETE ON agent_pi_tool_results BEGIN SELECT RAISE(ABORT, 'pi_tool_result_immutable'); END"}
COLUMN_TYPES = [('id', 'VARCHAR(36)'), ('record_version', 'INTEGER'), ('result_abi', 'VARCHAR(32)'), ('organization_id', 'VARCHAR(36)'), ('store_id', 'VARCHAR(36)'), ('requester_id', 'VARCHAR(36)'), ('agent_id', 'VARCHAR(36)'), ('run_id', 'VARCHAR(36)'), ('model_step_id', 'VARCHAR(36)'), ('tool_step_id', 'VARCHAR(36)'), ('model_kind', 'VARCHAR(16)'), ('tool_kind', 'VARCHAR(16)'), ('pi_receipt_id', 'VARCHAR(36)'), ('native_call_id', 'VARCHAR(100)'), ('tool_key', 'VARCHAR(120)'), ('tool_name', 'VARCHAR(120)'), ('tool_version', 'VARCHAR(120)'), ('toolset_version', 'VARCHAR(200)'), ('input_digest', 'VARCHAR(64)'), ('execution_epoch', 'BIGINT'), ('runtime_record_sha256', 'VARCHAR(64)'), ('result_kind', 'VARCHAR(16)'), ('output_ref', 'VARCHAR(71)'), ('payload_sha256', 'VARCHAR(64)'), ('source_refs_sha256', 'VARCHAR(64)'), ('canonical_ref', 'VARCHAR(71)'), ('canonical_json', 'TEXT'), ('created_at', 'DATETIME')]
EXPECTED_FKS = [('subjects', ('organization_id', 'agent_id'), ('organization_id', 'id'), 'NO ACTION', 'RESTRICT', 'NONE'), ('agent_steps', ('organization_id', 'store_id', 'run_id', 'model_step_id', 'model_kind'), ('organization_id', 'store_id', 'run_id', 'id', 'kind'), 'NO ACTION', 'RESTRICT', 'NONE'), ('agent_pi_tool_receipts', ('organization_id', 'store_id', 'run_id', 'native_call_id'), ('organization_id', 'store_id', 'run_id', 'native_call_id'), 'NO ACTION', 'RESTRICT', 'NONE'), ('agent_pi_tool_receipts', ('pi_receipt_id',), ('id',), 'NO ACTION', 'RESTRICT', 'NONE'), ('subjects', ('organization_id', 'requester_id'), ('organization_id', 'id'), 'NO ACTION', 'RESTRICT', 'NONE'), ('agent_runs', ('organization_id', 'store_id', 'run_id'), ('organization_id', 'store_id', 'id'), 'NO ACTION', 'RESTRICT', 'NONE'), ('agent_steps', ('organization_id', 'store_id', 'run_id', 'tool_step_id', 'tool_kind'), ('organization_id', 'store_id', 'run_id', 'id', 'kind'), 'NO ACTION', 'RESTRICT', 'NONE')]
EXPECTED_INDEXES = [('pk', 1, 'pk', 0, ('id',)), ('u', 1, 'u', 0, ('organization_id', 'store_id', 'run_id', 'native_call_id')), ('u', 1, 'u', 0, ('pi_receipt_id',)), ('u', 1, 'u', 0, ('organization_id', 'store_id', 'run_id', 'tool_step_id')), ('ix_pi_result_run', 0, 'c', 0, ('organization_id', 'store_id', 'run_id', 'created_at'))]
VERSION_CREATE_SQL = 'CREATE TABLE alembic_version (\n\tversion_num VARCHAR(32) NOT NULL, \n\tCONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)\n)'
assert OLD_CREATE_SQL.count(OLD_KIND) == 1
NEW_CREATE_SQL = OLD_CREATE_SQL.replace(OLD_KIND, NEW_KIND)


_TOKEN = re.compile(r"""'(?:''|[^'])*'|"(?:\"\"|[^"])*"|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|<>|<=|>=|!=|\|\||[(),=<>;+*./-]""")


def _reject(code):
    raise RuntimeError("pi_knowledge_forward_" + code)


def _tokens(sql):
    if not isinstance(sql, str):
        _reject("sql_missing")
    result, offset = [], 0
    for match in _TOKEN.finditer(sql):
        if sql[offset:match.start()].strip():
            _reject("sql_unknown_token")
        token = match.group()
        if token.startswith("'"):
            result.append(("string", token))
        elif token.startswith('"'):
            result.append(("quoted", token[1:-1].replace('""', '"')))
        elif token[0].isalpha() or token[0] == "_":
            result.append(("word", token.lower()))
        else:
            result.append(("symbol", token))
        offset = match.end()
    if sql[offset:].strip():
        _reject("sql_unknown_token")
    if result and result[-1] == ("symbol", ";"):
        result.pop()
    # ALTER TABLE RENAME quotes the new table identifier. Only that known
    # syntactic slot is normalized; never normalize arbitrary double-quoted
    # CHECK literals/identifiers (SQLite has double-quoted-string compatibility).
    if (len(result) > 2 and result[:2] == [("word", "create"), ("word", "table")]
            and result[2] == ("quoted", TABLE)):
        result[2] = ("word", TABLE)
    return tuple(result)


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def _all(raw, sql, parameters=()):
    cursor = raw.execute(sql, parameters)
    try:
        return cursor.fetchall()
    finally:
        cursor.close()


def _one(raw, sql, parameters=()):
    rows = _all(raw, sql, parameters)
    if len(rows) != 1:
        _reject("single_row_expected")
    return rows[0]


def raw_connection(connection):
    raw = connection.connection.driver_connection
    if raw is None:
        _reject("connection_missing")
    return raw


def _require_dedicated_connection(raw):
    databases = _all(raw, "PRAGMA database_list")
    if any(row[1] not in ("main", "temp") for row in databases):
        _reject("attached_database")
    if _all(raw, "SELECT name FROM sqlite_temp_master"):
        _reject("temporary_object")


def _read_versions(raw):
    _require_dedicated_connection(raw)
    names = _all(raw, "SELECT type,name,sql FROM main.sqlite_master WHERE name='alembic_version'")
    if not names:
        # An unstamped nonempty database is not an empty fresh database.
        if _all(raw, "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"):
            _reject("unstamped_nonempty")
        return frozenset()
    if len(names) != 1 or names[0][0] != "table" or _tokens(names[0][2]) != _tokens(VERSION_CREATE_SQL):
        _reject("version_shape")
    indexes = _all(raw, 'PRAGMA main.index_list("alembic_version")')
    if len(indexes) != 1 or tuple(indexes[0][2:]) != (1, "pk", 0):
        _reject("version_indexes")
    keys = [r for r in _all(raw, "PRAGMA main.index_xinfo(" + _q(indexes[0][1]) + ")") if r[5]]
    if len(keys) != 1 or tuple(keys[0][1:]) != (0, "version_num", 0, "BINARY", 1):
        _reject("version_indexes")
    if _all(raw, 'PRAGMA main.foreign_key_list("alembic_version")'):
        _reject("version_dependencies")
    if _all(raw, "SELECT name FROM main.sqlite_master WHERE type='trigger' AND tbl_name='alembic_version'"):
        _reject("version_dependencies")
    for table_name, in _all(raw, "SELECT name FROM main.sqlite_master WHERE type='table'"):
        if any(row[2].lower() == "alembic_version" for row in _all(raw, "PRAGMA main.foreign_key_list(" + _q(table_name) + ")")):
            _reject("version_dependencies")
    for owner, sql in _all(raw, "SELECT tbl_name,sql FROM main.sqlite_master WHERE type IN ('view','trigger','index') AND sql IS NOT NULL"):
        if owner != "alembic_version" and "alembic_version" in sql.lower():
            _reject("version_dependencies")
    cols = _all(raw, 'PRAGMA main.table_xinfo("alembic_version")')
    if len(cols) != 1 or tuple(cols[0][1:]) != ("version_num", "VARCHAR(32)", 1, None, 1, 0):
        _reject("version_shape")
    values = _all(raw, "SELECT version_num FROM main.alembic_version")
    if any(len(row) != 1 or not isinstance(row[0], str) or not row[0] for row in values):
        _reject("version_value")
    versions = frozenset(row[0] for row in values)
    if len(values) != len(versions):
        _reject("version_duplicate")
    if not versions and _all(raw, "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name<>'alembic_version'"):
        _reject("unstamped_nonempty")
    return versions


def _allowed_versions(versions, parents):
    if not versions:
        return True
    if len(versions) == 1:
        return next(iter(versions)) in parents
    if len(versions) != 2 or WORKSPACE not in versions:
        return False

    def ancestors(revision):
        result, todo = set(), [revision]
        while todo:
            item = todo.pop()
            if item in result:
                continue
            result.add(item)
            todo.extend(parents.get(item, ()))
        return result

    pi_branch = ancestors(PROJECTION) - ancestors(WORKSPACE)
    return (versions - {WORKSPACE}).issubset(pi_branch)


_STATE_KEY = "pi_knowledge_forward_transaction_owner"


def _invalidate_original(connection, raw, error):
    try:
        connection.invalidate()
    except BaseException as failure:
        error.add_note("pi_knowledge_forward_invalidate_failed:" + type(failure).__name__)
    # Pool invalidation may suppress a driver's close exception. Retain the
    # ORIGINAL raw object and explicitly close it even after invalidate returns.
    try:
        raw.close()
    except BaseException as closing:
        error.add_note("pi_knowledge_forward_physical_close_unknown:" + type(closing).__name__)


def _cleanup_failed_transaction(connection, raw, error):
    try:
        if raw.in_transaction:
            raw.rollback()
    except BaseException as cleanup:
        error.add_note("pi_knowledge_forward_rollback_failed:" + type(cleanup).__name__)
        _invalidate_original(connection, raw, error)


@contextmanager
def retained_step_errors(connection):
    """Share the existing owner/primary-error cleanup, not0079's started flag."""
    state = connection.info.get(_STATE_KEY)
    if type(state) is not dict:
        _reject("unowned_connection")
    state["retained_step_started"] = True
    try:
        yield
    except BaseException as error:
        state["step_error"] = error
        raise


def preflight_sqlite_connection(connection, revision_parents, retained_history=None):
    """Raw reads only: do not take a writer lock or cause SQLA autobegin."""
    raw = raw_connection(connection)
    if connection.in_transaction() or connection.in_nested_transaction() or raw.in_transaction:
        _reject("external_transaction")
    versions = _read_versions(raw)
    if not _allowed_versions(versions, revision_parents):
        if retained_history is None or not retained_history.allows_heads(versions):
            _reject("version_unknown")
    if retained_history is not None:
        retained_history.preflight(raw, versions)
    return raw


@contextmanager
def checked_migration_connection(engine, revision_parents, retained_history=None):
    """Own this env's connection and preserve primary errors during cleanup.

    Retain the original raw object and state dict; consulting connection.info
    after invalidation could reconnect, so cleanup never does that.
    """
    connection = raw = primary = None
    state = {"forward_started": False}
    try:
        connection = engine.connect()
        if engine.dialect.name == "sqlite":
            raw = raw_connection(connection)
            connection.info[_STATE_KEY] = state
            if retained_history is not None:
                from backend.migrations.retained_sales_history import CONNECTION_KEY
                connection.info[CONNECTION_KEY] = retained_history
            preflight_sqlite_connection(connection, revision_parents, retained_history)
        yield connection
        if (state["forward_started"] or state.get("retained_step_started")) and raw.in_transaction:
            _reject("transaction_left_open")
    except BaseException as error:
        remembered = state.get("step_error")
        primary = remembered if isinstance(remembered, BaseException) else error
        if primary is not error:
            primary.add_note("pi_knowledge_forward_proxy_unwind_failed:" + type(error).__name__)
        if (state["forward_started"] or state.get("retained_step_started")) and raw is not None:
            _cleanup_failed_transaction(connection, raw, primary)
        if primary is not error:
            raise primary from error
        raise
    finally:
        cleanup_error = None
        if connection is not None:
            try:
                connection.close()
            except BaseException as closing:
                target = primary or closing
                if primary is not None:
                    primary.add_note("pi_knowledge_forward_connection_close_failed:" + type(closing).__name__)
                if raw is not None:
                    _invalidate_original(connection, raw, target)
                if primary is None:
                    cleanup_error = closing
        try:
            engine.dispose()
        except BaseException as disposal:
            target = primary or cleanup_error or disposal
            if target is not disposal:
                target.add_note("pi_knowledge_forward_engine_dispose_failed:" + type(disposal).__name__)
            if connection is not None and raw is not None:
                _invalidate_original(connection, raw, target)
            if primary is None and cleanup_error is None:
                cleanup_error = disposal
        if primary is None and cleanup_error is not None:
            raise cleanup_error


def begin_forward_step(connection, migration_context):
    """Called only inside real0079 after Alembic's Proxy acquired RootTransaction."""
    if connection.dialect.name != "sqlite":
        _reject("postgresql_not_implemented")
    if alembic.__version__ != "1.18.4":
        _reject("alembic_version_unsupported")
    root = connection.get_transaction()
    if (migration_context.connection is not connection
            or getattr(migration_context, "_in_external_transaction", True)
            or migration_context.impl.transactional_ddl is not False
            or root is None or not root.is_active
            or getattr(migration_context, "_transaction", None) is not root
            or connection.in_nested_transaction()):
        _reject("proxy_transaction_required")
    raw = raw_connection(connection)
    state = connection.info.get(_STATE_KEY)
    if type(state) is not dict or state.get("forward_started") or raw.in_transaction:
        _reject("external_transaction")
    versions = _read_versions(raw)
    from backend.migrations.retained_sales_history import CONNECTION_KEY
    history = connection.info.get(CONNECTION_KEY)
    if versions != frozenset({PREDECESSOR}):
        if history is None or not history.forward_heads(versions):
            _reject("predecessor_required")
        history.require_shape(raw)
    raw.execute("PRAGMA foreign_keys=ON").close()
    if _one(raw, "PRAGMA foreign_keys")[0] != 1:
        _reject("fk_disabled")
    # Already-owned Proxy handles ordinary rollback including version UPDATE.
    # env also cleans the original raw object if driver commit/rollback fails.
    state["forward_started"] = True
    raw.execute("BEGIN IMMEDIATE").close()
    if not raw.in_transaction:
        _reject("physical_transaction_missing")
    if _read_versions(raw) != versions:
        _reject("version_changed_under_lock")
    if versions != frozenset({PREDECESSOR}):
        history.require_shape(raw)
    state["predecessor_versions"] = versions
    state["predecessor"] = PREDECESSOR


def require_boundary(connection):
    raw = raw_connection(connection)
    state = connection.info.get(_STATE_KEY)
    if (type(state) is not dict or state.get("predecessor") != PREDECESSOR
            or not state.get("forward_started") or not raw.in_transaction
            or _one(raw, "PRAGMA foreign_keys")[0] != 1):
        _reject("physical_fk_transaction_required")
    if _read_versions(raw) != state.get("predecessor_versions", frozenset({PREDECESSOR})):
        _reject("predecessor_required")
    return raw


def _table_sql(raw):
    row = _all(raw, "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,))
    if len(row) != 1:
        _reject("table_missing")
    return row[0][0]


def _pragma_shape(raw):
    columns = _all(raw, "PRAGMA table_xinfo(" + _q(TABLE) + ")")
    observed = [(row[0], row[1], row[2].upper(), row[3], row[4], row[5], row[6]) for row in columns]
    expected = [(i, name, kind, 1, None, int(i == 0), 0) for i, (name, kind) in enumerate(COLUMN_TYPES)]
    if observed != expected:
        _reject("columns_changed")
    foreign = {}
    for row in _all(raw, "PRAGMA foreign_key_list(" + _q(TABLE) + ")"):
        foreign.setdefault(row[0], []).append(row)
    actual = []
    for rows in foreign.values():
        rows.sort(key=lambda r: r[1])
        actual.append((rows[0][2], tuple(row[3] for row in rows), tuple(row[4] for row in rows),
                       rows[0][5], rows[0][6], rows[0][7]))
    if sorted(actual) != sorted(EXPECTED_FKS):
        _reject("foreign_keys_changed")
    indexes = []
    for row in _all(raw, "PRAGMA index_list(" + _q(TABLE) + ")"):
        _, name, unique, origin, partial = row
        cols = _all(raw, "PRAGMA index_xinfo(" + _q(name) + ")")
        keys = [col for col in cols if col[5]]
        if any(col[1] < 0 or col[3] != 0 or col[4] != "BINARY" for col in keys):
            _reject("index_expression_or_collation")
        indexes.append((name if origin == "c" else origin, unique, origin, partial,
                        tuple(col[2] for col in keys)))
    if sorted(indexes) != sorted(EXPECTED_INDEXES):
        _reject("indexes_changed")


def inspect_shape(raw):
    """Full exact recognition before first mutating statement, under writer lock."""
    if _all(raw, "SELECT name FROM sqlite_master WHERE name=?", (SHADOW,)):
        _reject("shadow_exists")
    table = _tokens(_table_sql(raw))
    if table == _tokens(OLD_CREATE_SQL):
        kind = "old"
    elif table == _tokens(NEW_CREATE_SQL):
        kind = "expanded"
    else:
        _reject("table_shape_unknown")
    _pragma_shape(raw)
    own = _all(raw, "SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger')", (TABLE,))
    named = {name: (kind_, sql) for kind_, name, sql in own if sql is not None}
    expected = {"ix_pi_result_run": ("index", INDEX_SQL), **{name: ("trigger", sql) for name, sql in TRIGGER_SQL.items()}}
    if set(named) != set(expected):
        _reject("owned_objects_changed")
    for name, (kind_, sql) in named.items():
        if kind_ != expected[name][0] or _tokens(sql) != _tokens(expected[name][1]):
            _reject("owned_object_body_changed")
    for table_name, in _all(raw, "SELECT name FROM sqlite_master WHERE type='table'"):
        for fk in _all(raw, "PRAGMA foreign_key_list(" + _q(table_name) + ")"):
            if fk[2].lower() == TABLE:
                _reject("incoming_foreign_key")
    for kind_, name, owner, sql in _all(raw, "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('view','trigger','index') AND sql IS NOT NULL"):
        if owner == TABLE or TABLE not in sql.lower():
            continue
        # Conservative HOLD even for quoted/comment references; no external
        # object is automatically rewritten or dropped.
        _reject("external_dependency")
    if _all(raw, "PRAGMA foreign_key_check"):
        _reject("foreign_key_damage")
    return kind


def data_fingerprint(raw, table=TABLE):
    if table not in (TABLE, SHADOW):
        _reject("fingerprint_table")
    names = [name for name, _ in COLUMN_TYPES]
    query = "SELECT " + ",".join(_q(x) for x in names) + "," + ",".join("typeof(" + _q(x) + ")" for x in names)
    query += " FROM " + _q(table) + ' ORDER BY "id" COLLATE BINARY'
    cursor, digest, count = raw.execute(query), sha256(), 0
    try:
        for row in cursor:
            values = []
            for name, value, kind in zip(names, row[:len(names)], row[len(names):]):
                if isinstance(value, bytes):
                    value = {"bytes": value.hex()}
                elif isinstance(value, float):
                    value = {"ieee754": struct.pack(">d", value).hex()}
                values.append([name, kind, value])
            body = json.dumps(values, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            digest.update(len(body).to_bytes(8, "big")); digest.update(body); count += 1
    finally:
        cursor.close()
    return count, digest.hexdigest()


def _checkpoint(label, connection):
    """No-op phase boundary for precise anonymous fault injection tests."""


def expand_result_kind(connection):
    raw = require_boundary(connection)
    shape = inspect_shape(raw)
    original = data_fingerprint(raw)
    _checkpoint("recognized", connection)
    if shape == "expanded":
        _checkpoint("author_noop", connection)
        return
    create = NEW_CREATE_SQL.replace("CREATE TABLE " + TABLE, "CREATE TABLE " + SHADOW, 1)
    connection.exec_driver_sql(create)
    _checkpoint("created_shadow", connection)
    columns = ",".join(_q(name) for name, _ in COLUMN_TYPES)
    connection.exec_driver_sql("INSERT INTO " + _q(SHADOW) + " (" + columns + ") SELECT " + columns + " FROM " + _q(TABLE))
    _checkpoint("copied", connection)
    if data_fingerprint(raw, SHADOW) != original:
        _reject("copy_mismatch")
    connection.exec_driver_sql("DROP TABLE " + _q(TABLE))
    _checkpoint("dropped_old", connection)
    connection.exec_driver_sql("ALTER TABLE " + _q(SHADOW) + " RENAME TO " + _q(TABLE))
    _checkpoint("renamed", connection)
    connection.exec_driver_sql(INDEX_SQL)
    _checkpoint("indexed", connection)
    for number, sql in enumerate(TRIGGER_SQL.values(), 1):
        connection.exec_driver_sql(sql)
        _checkpoint("trigger_" + str(number), connection)
    if inspect_shape(raw) != "expanded" or data_fingerprint(raw) != original:
        _reject("final_mismatch")
    _checkpoint("verified", connection)


def run_forward_step(connection, migration_context):
    # Retain the migration body's primary exception before Proxy rollback can
    # itself fail. env reads this same dict without revalidating/reconnecting.
    state = connection.info.get(_STATE_KEY)
    if type(state) is not dict:
        _reject("unowned_connection")
    try:
        begin_forward_step(connection, migration_context)
        expand_result_kind(connection)
    except BaseException as error:
        state["step_error"] = error
        raise
