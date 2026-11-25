"""P1-A resource manifest DRAFT storage; no execution/approval migration.

Old reserved-key collisions abort before DDL. Existing rows stay legacy-digest.
Never batch-rebuild the referenced skill table or remove historical Run pins.
"""
from alembic import op
import sqlalchemy as sa

revision = "0074_skill_resource_manifests"
down_revision = "0073_guanjia_category_receipts"
branch_labels = depends_on = None
_KIND_CHECK = "(spec_kind = 'legacy-digest' AND substr(skill_key,1,12) <> 'pi-resource.') OR " \
              "(spec_kind = 'pi-skill-resource/1' AND substr(skill_key,1,12) = 'pi-resource.')"
_COLLISION = "SELECT 1 FROM agent_skill_versions WHERE substr(skill_key,1,12) = 'pi-resource.' LIMIT 1"


def upgrade():
    ctx, bind = op.get_context(), op.get_bind()
    if ctx.as_sql:
        if bind.dialect.name != "postgresql":
            raise RuntimeError("skill_resource_upgrade_requires_collision_check")
        op.execute("DO $$ BEGIN IF EXISTS (" + _COLLISION + ") THEN "
                   "RAISE EXCEPTION 'skill_resource_reserved_key_collision'; END IF; END $$;")
    elif bind.execute(sa.text(_COLLISION)).first() is not None:
        raise RuntimeError("skill_resource_reserved_key_collision")
    op.add_column("agent_skill_versions", sa.Column("spec_kind", sa.String(32),
        sa.CheckConstraint(_KIND_CHECK, name="ck_agent_skill_spec_kind"),
        nullable=False, server_default="legacy-digest"))
    op.create_table("agent_skill_resource_manifests",
        sa.Column("skill_id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.String(36), nullable=False),
        sa.Column("knowledge_entry_id", sa.String(36), nullable=False),
        sa.Column("revision_id", sa.String(36), nullable=False),
        sa.Column("canonical_manifest", sa.Text(), nullable=False),
        sa.Column("spec_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "skill_id"],
            ["agent_skill_versions.organization_id", "agent_skill_versions.store_id", "agent_skill_versions.id"],
            name="fk_skill_resource_version", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "knowledge_entry_id"],
            ["knowledge_entries.organization_id", "knowledge_entries.store_id", "knowledge_entries.id"],
            name="fk_skill_resource_entry", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "store_id", "revision_id"],
            ["knowledge_revisions.organization_id", "knowledge_revisions.store_id", "knowledge_revisions.id"],
            name="fk_skill_resource_revision", ondelete="RESTRICT"),
        sa.CheckConstraint("length(spec_sha256) = 64", name="ck_skill_resource_sha"),
        sa.CheckConstraint("length(canonical_manifest) BETWEEN 1 AND 16384", name="ck_skill_resource_manifest_size"))
    if bind.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER trg_skill_resource_no_{action.lower()} BEFORE {action} "
                "ON agent_skill_resource_manifests BEGIN SELECT RAISE(ABORT, 'skill resource immutable'); END")
        op.execute("""CREATE TRIGGER trg_skill_resource_parent_insert BEFORE INSERT ON agent_skill_resource_manifests
            WHEN NOT EXISTS (SELECT 1 FROM agent_skill_versions s JOIN knowledge_revisions r
              ON r.id = NEW.revision_id AND r.entry_id = NEW.knowledge_entry_id
              AND r.organization_id = NEW.organization_id AND r.store_id = NEW.store_id
              WHERE s.id = NEW.skill_id AND s.organization_id = NEW.organization_id
              AND s.store_id = NEW.store_id AND s.spec_kind = 'pi-skill-resource/1'
              AND s.spec_sha256 = NEW.spec_sha256)
            BEGIN SELECT RAISE(ABORT, 'skill resource binding invalid'); END""")
        op.execute("""CREATE TRIGGER trg_skill_resource_version_update BEFORE UPDATE OF
            id,organization_id,store_id,skill_key,release_tag,spec_kind,spec_sha256,provider_key,model,prompt_version,toolset_version
            ON agent_skill_versions WHEN OLD.spec_kind = 'pi-skill-resource/1'
            BEGIN SELECT RAISE(ABORT, 'resource skill binding immutable'); END""")
        op.execute("""CREATE TRIGGER trg_skill_resource_version_delete BEFORE DELETE ON agent_skill_versions
            WHEN OLD.spec_kind = 'pi-skill-resource/1'
            BEGIN SELECT RAISE(ABORT, 'resource skill fact immutable'); END""")
    elif bind.dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION deny_skill_resource_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'skill resource immutable'; END; $$""")
        op.execute("""CREATE TRIGGER trg_skill_resource_immutable BEFORE UPDATE OR DELETE
            ON agent_skill_resource_manifests FOR EACH ROW EXECUTE FUNCTION deny_skill_resource_mutation()""")
        op.execute("""CREATE FUNCTION check_skill_resource_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NOT EXISTS (SELECT 1 FROM agent_skill_versions s JOIN knowledge_revisions r
              ON r.id = NEW.revision_id AND r.entry_id = NEW.knowledge_entry_id
              AND r.organization_id = NEW.organization_id AND r.store_id = NEW.store_id
              WHERE s.id = NEW.skill_id AND s.organization_id = NEW.organization_id
              AND s.store_id = NEW.store_id AND s.spec_kind = 'pi-skill-resource/1'
              AND s.spec_sha256 = NEW.spec_sha256) THEN RAISE EXCEPTION 'skill resource binding invalid';
              END IF; RETURN NEW; END; $$""")
        op.execute("""CREATE TRIGGER trg_skill_resource_parent_insert BEFORE INSERT
            ON agent_skill_resource_manifests FOR EACH ROW EXECUTE FUNCTION check_skill_resource_insert()""")
        op.execute("""CREATE TRIGGER trg_skill_resource_version_update BEFORE UPDATE OF
            id,organization_id,store_id,skill_key,release_tag,spec_kind,spec_sha256,provider_key,model,prompt_version,toolset_version
            ON agent_skill_versions FOR EACH ROW WHEN (OLD.spec_kind = 'pi-skill-resource/1')
            EXECUTE FUNCTION deny_skill_resource_mutation()""")
        op.execute("""CREATE TRIGGER trg_skill_resource_version_delete BEFORE DELETE ON agent_skill_versions
            FOR EACH ROW WHEN (OLD.spec_kind = 'pi-skill-resource/1')
            EXECUTE FUNCTION deny_skill_resource_mutation()""")


def downgrade():
    ctx, bind = op.get_context(), op.get_bind()
    if ctx.as_sql:
        raise RuntimeError("skill_resource_downgrade_requires_data_check")
    if (bind.execute(sa.text("SELECT 1 FROM agent_skill_resource_manifests LIMIT 1")).first() is not None
            or bind.execute(sa.text("SELECT 1 FROM agent_skill_versions WHERE spec_kind <> 'legacy-digest' "
                                    "OR substr(skill_key,1,12) = 'pi-resource.' LIMIT 1")).first() is not None):
        raise RuntimeError("skill_resource_facts_require_safe_forward_recovery")
    if bind.dialect.name == "sqlite":
        for name in ("no_update", "no_delete", "parent_insert", "version_update", "version_delete"):
            op.execute("DROP TRIGGER trg_skill_resource_" + name)
    elif bind.dialect.name == "postgresql":
        for name, table in (("immutable", "agent_skill_resource_manifests"),
                            ("parent_insert", "agent_skill_resource_manifests"),
                            ("version_update", "agent_skill_versions"), ("version_delete", "agent_skill_versions")):
            op.execute("DROP TRIGGER trg_skill_resource_" + name + " ON " + table)
        op.execute("DROP FUNCTION check_skill_resource_insert()")
        op.execute("DROP FUNCTION deny_skill_resource_mutation()")
    op.drop_table("agent_skill_resource_manifests")
    op.drop_column("agent_skill_versions", "spec_kind")
