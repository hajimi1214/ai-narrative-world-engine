"""incremental formal state identity v2

Revision ID: 0031_incremental_formal_state_identity
Revises: 0030_dimension_aware_memory_ann
"""
from alembic import op
import sqlalchemy as sa

revision = "0031_incremental_formal_state_identity"
down_revision = "0030_dimension_aware_memory_ann"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_formal_state_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("protocol_version", sa.String(60), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="DIRTY"),
        sa.Column("resource_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("collection_state", sa.JSON(), nullable=False),
        sa.Column("state_fingerprint", sa.String(120)),
        sa.Column("built_through_sequence", sa.Integer()),
        sa.Column("dirty_reason", sa.String(200)),
        sa.Column("last_rebuilt_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", name="uq_project_formal_state_identity_project"),
    )
    op.create_index("ix_project_formal_state_identities_project_id", "project_formal_state_identities", ["project_id"])
    op.create_table(
        "formal_state_leaves",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("collection_name", sa.String(80), nullable=False),
        sa.Column("resource_id", sa.String(36), nullable=False),
        sa.Column("leaf_fingerprint", sa.String(120), nullable=False),
        sa.UniqueConstraint("project_id", "collection_name", "resource_id", name="uq_formal_state_leaf_resource"),
    )
    op.create_index("ix_formal_state_leaves_project_id", "formal_state_leaves", ["project_id"])
    op.create_index("ix_formal_state_leaf_project_collection", "formal_state_leaves", ["project_id", "collection_name"])
    op.create_index("ix_formal_state_leaf_project_resource", "formal_state_leaves", ["project_id", "resource_id"])
    op.add_column("world_snapshots", sa.Column("state_fingerprint_protocol", sa.String(60), nullable=False, server_default="world-snapshot-v1"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name in {"postgresql", "sqlite"}:
        try:
            has_v2 = bind.execute(sa.text("SELECT 1 FROM world_snapshots WHERE state_fingerprint_protocol = 'formal-world-state-v2' LIMIT 1")).first()
        except Exception:
            has_v2 = None
        if has_v2:
            raise RuntimeError("FORMAL_STATE_V2_DOWNGRADE_REQUIRES_MATERIALIZATION")
    op.drop_column("world_snapshots", "state_fingerprint_protocol")
    op.drop_index("ix_formal_state_leaf_project_resource", table_name="formal_state_leaves")
    op.drop_index("ix_formal_state_leaf_project_collection", table_name="formal_state_leaves")
    op.drop_index("ix_formal_state_leaves_project_id", table_name="formal_state_leaves")
    op.drop_table("formal_state_leaves")
    op.drop_index("ix_project_formal_state_identities_project_id", table_name="project_formal_state_identities")
    op.drop_table("project_formal_state_identities")
