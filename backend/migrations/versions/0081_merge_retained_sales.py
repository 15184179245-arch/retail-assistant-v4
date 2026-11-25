"""Join both retained Sales histories without changing their immutable nodes."""
revision = "0081_merge_retained_sales"
down_revision = ("0080_workspace_concurrency", "0055_merge_sales_quote")
branch_labels = depends_on = None

def upgrade():
    pass

def downgrade():
    raise RuntimeError("retained_sales_merge_forward_only_preserve_histories")
