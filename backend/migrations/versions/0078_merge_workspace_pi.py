"""Merge retained Workspace and Pi migration histories without changing schema.

Revision ID: 0078_merge_workspace_pi
Revises: 0054_thread_workspace_artifacts, 0077_knowledge_projection_gen

This revision only joins the two retained heads. It does not prove that either
predecessor can safely run against an existing database. No live migration has
been run. Downgrading below this merge still requires separate predecessor
rollback review, especially the guarded Pi projection migrations.
"""
from __future__ import annotations

revision = "0078_merge_workspace_pi"
down_revision = (
    "0054_thread_workspace_artifacts",
    "0077_knowledge_projection_gen",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
