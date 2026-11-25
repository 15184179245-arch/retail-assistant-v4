"""One owned reentry boundary for two immutable, historically equivalent Sales nodes.

No version writes, stamps, retries, backfill or target-learned expectations.
SQLite only. Alembic owns the physical step transaction through version update.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path

from alembic import op

LEGACY = "0055_merge_sales_quote"
CURRENT = "0054_sales_quote_identity_v2"
PREDECESSOR = "0078_merge_workspace_pi"
MERGE = "0081_merge_retained_sales"
TABLES = ("sales_draft_lines", "pricing_source_currency_policies_v2",
          "pricing_quotes_v2", "sales_line_commands_v2")
SOURCE_SHA256 = {
    MERGE: "7b8f3d5d0dbc682d9ea074d1cfc5dad9e78d1797f29d9b3086274250c4b2e52a",
    LEGACY: "c6e0dfd55450c74b8ec09ee4166866d6bf8149727a8fd1f18147ba2c7424ca34",
    CURRENT: "d5647f64f5f8d7241a830544deb86b63edc0494579960017abf6b50e1d78508f",
}
SCHEMA_SHA256 = "7174c04c612aae53f8d4915803db5866d31ee8d1c1821a05ccc41da867217c93"
SCHEMA_RESOURCE = Path(__file__).with_name("frozen_sales_schema.json")
CONNECTION_KEY = "retained_sales_history_owner"
CANONICAL_PARENTS = {
    "0054_sales_quote_identity_v2": ("0053_knowledge_revision_access",),
    "0055_pi_native_tool_receipts": ("0054_sales_quote_identity_v2",),
    "0056_pi_provider_receipts": ("0055_pi_native_tool_receipts",),
    "0057_pi_provider_content_digest": ("0056_pi_provider_receipts",),
    "0058_pi_go_usage_basis": ("0057_pi_provider_content_digest",),
    "0059_memory_durable_retention": ("0058_pi_go_usage_basis",),
    "0060_pi_memory_provider_profile": ("0059_memory_durable_retention",),
    "0061_assistant_work_intake": ("0060_pi_memory_provider_profile",),
    "0062_assistant_pi_dispatch": ("0061_assistant_work_intake",),
    "0063_agent_message_text": ("0062_assistant_pi_dispatch",),
    "0064_assistant_work_authority": ("0063_agent_message_text",),
    "0065_assistant_progress": ("0064_assistant_work_authority",),
    "0066_assistant_turns": ("0065_assistant_progress",),
    "0067_assistant_turn_execution": ("0066_assistant_turns",),
    "0068_assistant_inbox": ("0067_assistant_turn_execution",),
    "0069_assistant_inbox_execution": ("0068_assistant_inbox",),
    "0070_pi_discovery_profile": ("0069_assistant_inbox_execution",),
    "0071_guanjia_observation": ("0070_pi_discovery_profile",),
    "0072_pi_source_profile": ("0071_guanjia_observation",),
    "0073_guanjia_category_receipts": ("0072_pi_source_profile",),
    "0074_skill_resource_manifests": ("0073_guanjia_category_receipts",),
    "0075_pi_readonly_stream_profile": ("0074_skill_resource_manifests",),
    "0076_pi_canonical_tool_results": ("0075_pi_readonly_stream_profile",),
    "0077_knowledge_projection_gen": ("0076_pi_canonical_tool_results",),
    "0078_merge_workspace_pi": ("0054_thread_workspace_artifacts", "0077_knowledge_projection_gen"),
    "0079_pi_knowledge_tool_error": ("0078_merge_workspace_pi",),
    "0080_workspace_concurrency": ("0079_pi_knowledge_tool_error",),
}


def _reject(reason):
    raise RuntimeError("retained_sales_" + reason)


def ancestors(heads, parents):
    result, todo = set(), list(heads)
    while todo:
        revision = todo.pop()
        if revision not in parents:
            _reject("unknown_revision")
        if revision not in result:
            result.add(revision)
            todo.extend(parents[revision])
    return result


def _parents(scripts):
    return {r.revision: (() if r.down_revision is None else r.down_revision
            if isinstance(r.down_revision, tuple) else (r.down_revision,))
            for r in scripts.walk_revisions()}


def reject_offline_reentry(scripts, targets, start=None):
    # Refuse before EnvironmentContext emits ANY SQL. Earlier unrelated
    # historical targets remain usable offline.
    heads = tuple(r.revision for r in scripts.get_revisions(targets))
    parents = _parents(scripts)
    interval = ancestors(heads, parents)
    if start is not None:
        # A start:end range compiles only the revisions between the two
        # endpoints, not every ancestor of the destination. The direction
        # is not yet known here, so the exact executed set is one side of
        # the symmetric difference; Sales nodes on either side refuse.
        origins = tuple(r.revision for r in scripts.get_revisions(start))
        prior = ancestors(origins, parents)
        interval = interval ^ prior
    if {LEGACY, CURRENT} & interval:
        _reject("live_schema_proof_required")


def schema_shape(raw):
    # Share the existing strict tokenizer, not a second SQL parser.
    from backend.migrations.pi_result_forward import _tokens
    result = []
    for kind, name, table, sql in raw.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE tbl_name IN (?,?,?,?) ORDER BY type,name", TABLES
    ).fetchall():
        value = None
        if sql is not None:
            tokens = _tokens(sql)
            if kind == "table":
                start = tokens.index(("symbol", "("))
                chunks, chunk, depth = [], [], 0
                for token in tokens[start + 1:-1]:
                    if token == ("symbol", ",") and depth == 0:
                        chunks.append(tuple(chunk))
                        chunk = []
                        continue
                    chunk.append(token)
                    if token == ("symbol", "("):
                        depth += 1
                    elif token == ("symbol", ")"):
                        depth -= 1
                chunks.append(tuple(chunk))
                # Only constraint declaration order is irrelevant; xinfo below
                # independently preserves column order/type/null/default/PK.
                value = (tokens[:start], tuple(sorted(chunks)))
            else:
                value = tokens
        result.append((kind, name, table, value))
    for table in TABLES:
        result.append(("column_xinfo", table, raw.execute(
            'PRAGMA table_xinfo("' + table + '")').fetchall()))
    return json.loads(json.dumps(result))


def _sales_present(shape):
    return any(row[0] == "table" and row[2] in TABLES[1:] for row in shape) or any(
        row[0] == "column_xinfo" and row[1] == TABLES[0] and any(
            col[1] in ("catalog_product_id", "unit", "quote_v2_id") for col in row[2]
        ) for row in shape
    )


def _before_lock(connection, revision):
    """No-op deterministic test seam; never changes authority or commits."""


class RetainedSalesHistory:
    def __init__(self, scripts):
        self.parents = _parents(scripts)
        if (self.parents.get(LEGACY) != ("0054_thread_workspace_artifacts",)
                or self.parents.get(CURRENT) != ("0053_knowledge_revision_access",)):
            _reject("frozen_parent_drift")
        if any(self.parents.get(identity) != down for identity, down in CANONICAL_PARENTS.items()):
            _reject("canonical_parent_drift")
        if self.parents.get(MERGE) != ("0080_workspace_concurrency", LEGACY):
            _reject("merge_parent_drift")
        for identity, expected in SOURCE_SHA256.items():
            if sha256(Path(scripts.get_revision(identity).path).read_bytes()).hexdigest() != expected:
                _reject("frozen_source_drift")
        data = SCHEMA_RESOURCE.read_bytes()
        if sha256(data).hexdigest() != SCHEMA_SHA256:
            _reject("frozen_schema_drift")
        self.expected = json.loads(data)

    def require_shape(self, raw):
        if schema_shape(raw) != self.expected:
            _reject("schema_mismatch")

    def allows_heads(self, heads):
        if len(heads) != 2 or LEGACY not in heads:
            return False
        other = next(iter(heads - {LEGACY}))
        if other not in CANONICAL_PARENTS:
            return False
        history = ancestors((other,), self.parents)
        return CURRENT in history and LEGACY not in history

    def preflight(self, raw, heads):
        # Advisory fail-closed before ANY DDL. Repeat relevant proofs after
        # the physical writer lock; this read alone never authorizes reentry.
        history = ancestors(heads, self.parents)
        if MERGE in history:
            # Both reentries and0079 have completed through the pinned merge.
            # Future legitimate Sales schema evolution is not frozen forever.
            return
        shape = schema_shape(raw)
        proven = bool({LEGACY, CURRENT} & history)
        if _sales_present(shape) or proven:
            if not proven:
                _reject("unproven_existing_schema")
            if shape != self.expected:
                _reject("schema_mismatch_before_ddl")

    def forward_heads(self, heads):
        return heads == frozenset((PREDECESSOR, LEGACY))

    def wrap(self, original, identity, other, connection):
        def upgrade():
            context = op.get_context()
            bind = op.get_bind()
            if bind is not connection or bind.dialect.name != "sqlite":
                _reject("sqlite_owned_connection_required")
            raw = bind.connection.driver_connection
            root = bind.get_transaction()
            if (context.connection is not bind or context._in_external_transaction
                    or context.impl.transactional_ddl is not False
                    or root is None or not root.is_active
                    or context._transaction is not root or bind.in_nested_transaction()
                    or raw.in_transaction):
                _reject("owned_proxy_required")
            from backend.migrations.pi_result_forward import retained_step_errors
            with retained_step_errors(bind):
                _before_lock(bind, identity)
                raw.execute("BEGIN IMMEDIATE").close()
                # Both decision AND standard Alembic version change are inside
                # the same owned physical transaction. Never release it here.
                heads = context.get_current_heads()
                history = ancestors(heads, self.parents)
                if identity in history:
                    _reject("concurrent_revision_already_applied")
                shape = schema_shape(raw)
                if other in history:
                    if shape != self.expected:
                        _reject("schema_mismatch")
                    return
                if _sales_present(shape):
                    _reject("unproven_existing_schema")
                original()
                self.require_shape(raw)
        upgrade._retained_sales_owner = self
        return upgrade


@contextmanager
def retained_sales_steps(scripts, connection, history):
    """Use EnvironmentContext's exact script; restore callables on every exit."""
    installed, primary = [], None
    try:
        for identity, other in ((LEGACY, CURRENT), (CURRENT, LEGACY)):
            module = scripts.get_revision(identity).module
            original = module.upgrade
            if getattr(original, "_retained_sales_owner", None) is not None:
                _reject("script_already_owned")
            wrapper = history.wrap(original, identity, other, connection)
            module.upgrade = wrapper
            installed.append((module, original, wrapper))
        yield history
    except BaseException as error:
        primary = error
        raise
    finally:
        drift = False
        for module, original, wrapper in reversed(installed):
            if module.upgrade is wrapper:
                module.upgrade = original
            else:
                drift = True
        if drift:
            if primary is not None:
                primary.add_note("retained_sales_wrapper_ownership_changed")
            else:
                _reject("wrapper_ownership_changed")
