from alembic import context
from alembic.script import ScriptDirectory
from backend.migrations.postgresql_compat import configure_migration_dialect
from backend.migrations.sqlite_reflection import preserve_inline_fk_actions
from backend.migrations.pi_result_forward import checked_migration_connection
from sqlalchemy import engine_from_config, pool

from backend.app.core.config import database_url
from backend.app.db.base import Base
from backend.app.modules.identity import models as identity_models  # noqa: F401
from backend.app.modules.identity import machine_service as machine_models  # noqa: F401
from backend.app.modules.approvals import models as approval_models  # noqa: F401
from backend.app.modules.audit import models as audit_models  # noqa: F401
from backend.app.modules.tasks import models as task_models  # noqa: F401
from backend.app.modules.assistant import models as assistant_models  # noqa: F401
from backend.app.modules.workspace import models as workspace_models  # noqa: F401
from backend.app.modules.auth import models as auth_models  # noqa: F401
from backend.app.modules.agents import models as agent_models  # noqa: F401
# SkillVersion (SKL-001) lives in agent_models and is included in Base.metadata.
from backend.app.modules.knowledge import models as knowledge_models  # noqa: F401
from backend.app.modules.catalog import models as catalog_models  # noqa: F401
from backend.app.modules.integrations.guanjia import observation_models as guanjia_observation_models  # noqa: F401
from backend.app.modules.integrations.guanjia import category_receipt_models as guanjia_category_receipt_models  # noqa: F401
from backend.app.modules.media import models as media_models  # noqa: F401
from backend.app.modules.locations import models as locations_models  # noqa: F401
from backend.app.modules.memory import models as memory_models  # noqa: F401
from backend.app.modules.search import models as search_models  # noqa: F401
from backend.app.modules.pricing import models as pricing_models  # noqa: F401
from backend.app.modules.sales import models as sales_models  # noqa: F401
from backend.app.modules.purchasing import models as purchasing_models  # noqa: F401
from backend.app.modules.receiving import models as receiving_models  # noqa: F401
from backend.app.modules.stocktake import models as stocktake_models  # noqa: F401
from backend.app.modules.analytics import models as analytics_models  # noqa: F401
from backend.app.modules.notifications import models as notification_models  # noqa: F401
from backend.app.modules.office_intake import ofc_002_models as office_models  # noqa: F401
from backend.app.modules.office_extraction import ofc_003_models as office_extraction_models  # noqa: F401
from backend.app.modules.office_rules import ofc_004_models as office_rules_models  # noqa: F401
from backend.app.modules.office_documents import ofc_005_models as office_document_models  # noqa: F401
from backend.app.modules.office_finance import fin_001_models as finance_workpaper_models  # noqa: F401
from backend.app.modules.erp_commands import erp_002_models as erp_command_models  # noqa: F401
from backend.app.modules.erp_commands import erp_003_models as erp_sandbox_models  # noqa: F401
from backend.app.modules.office_workflows import ofc_006_models as office_workflow_models  # noqa: F401
from backend.app.modules.office_exceptions import ofc_007_models as office_exception_models  # noqa: F401
from backend.app.modules.office_scheduling import ofc_008_models as office_schedule_models  # noqa: F401
from backend.app.modules.erp_sales import erp_004_models as erp_sales_models  # noqa: F401
from backend.app.modules.erp_purchasing import erp_005_models as erp_receipt_models  # noqa: F401
from backend.app.modules.erp_catalog import erp_007_models as erp_catalog_models  # noqa: F401
from backend.app.modules.erp_finance import erp_006_models as erp_settlement_models  # noqa: F401
from backend.app.modules.office_review import ofc_009_models as office_review_models  # noqa: F401
from backend.app.modules.analytics import recommendation_models as analytics_recommendations  # noqa: F401
from backend.app.workers import outbox as outbox_models  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))
target_metadata = Base.metadata

# Virtual/search shadow tables have explicit migration DDL, not ORM entities.
# Ignore only these exact SQLite FTS5 names, never unrelated schema drift.
FTS_TABLES = {"search_projection_fts", "search_projection_fts_data", "search_projection_fts_idx",
              "search_projection_fts_docsize", "search_projection_fts_config"}

def include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "table" and reflected and compare_to is None and name in FTS_TABLES)



def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"}, compare_type=True, include_object=include_object)
    configure_migration_dialect(context.get_context().dialect)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    scripts = ScriptDirectory.from_config(config)
    parents = {}
    for revision in scripts.walk_revisions():
        down = revision.down_revision
        parents[revision.revision] = () if down is None else down if isinstance(down, tuple) else (down,)
    # Raw-only preflight: current/check remain read-only and do not acquire a
    # writer lock. Physical FK-ON BEGIN belongs only to actual0079 upgrade.
    with checked_migration_connection(connectable, parents) as connection:
        configure_migration_dialect(connection.dialect)
        with preserve_inline_fk_actions(connection):
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True, include_object=include_object)
            with context.begin_transaction():
                context.run_migrations()



if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
