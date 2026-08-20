"""compact scene checkpoints

Revision ID: 0028_compact_scene_checkpoints
Revises: 0027_million_word_history_index
"""
from alembic import op
import sqlalchemy as sa


revision = "0028_compact_scene_checkpoints"
down_revision = "0027_million_word_history_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("world_snapshots", sa.Column("storage_mode", sa.String(length=30), nullable=False, server_default="LEGACY_FULL"))
    op.add_column("world_snapshots", sa.Column("base_snapshot_id", sa.String(length=36), sa.ForeignKey("world_snapshots.id")))
    op.add_column("world_snapshots", sa.Column("storage_fingerprint", sa.String(length=120)))
    op.add_column("world_snapshots", sa.Column("materialization_depth", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_world_snapshots_project_storage_mode", "world_snapshots", ["project_id", "storage_mode"])
    op.create_index("ix_world_snapshots_base_snapshot_id", "world_snapshots", ["base_snapshot_id"])
    op.create_table(
        "project_world_snapshot_heads",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), sa.ForeignKey("world_snapshots.id"), nullable=False),
        sa.Column("state_fingerprint", sa.String(length=120), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.String(length=36)),
        sa.Column("sequence", sa.Integer()),
        sa.Column("head_fingerprint", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", name="uq_project_world_snapshot_head_project"),
    )
    op.create_index("ix_project_world_snapshot_heads_project_id", "project_world_snapshot_heads", ["project_id"])


def downgrade() -> None:
    bind = op.get_bind()
    compact = bind.execute(sa.text("SELECT 1 FROM world_snapshots WHERE storage_mode <> 'LEGACY_FULL' LIMIT 1")).fetchone()
    if compact:
        raise RuntimeError("COMPACT_SNAPSHOT_DOWNGRADE_REQUIRES_MATERIALIZATION")
    op.drop_index("ix_project_world_snapshot_heads_project_id", table_name="project_world_snapshot_heads")
    op.drop_table("project_world_snapshot_heads")
    op.drop_index("ix_world_snapshots_base_snapshot_id", table_name="world_snapshots")
    op.drop_index("ix_world_snapshots_project_storage_mode", table_name="world_snapshots")
    op.drop_column("world_snapshots", "materialization_depth")
    op.drop_column("world_snapshots", "storage_fingerprint")
    op.drop_column("world_snapshots", "base_snapshot_id")
    op.drop_column("world_snapshots", "storage_mode")
