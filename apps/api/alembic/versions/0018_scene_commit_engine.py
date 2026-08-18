"""scene commit engine"""
from alembic import op
import sqlalchemy as sa


revision = "0018_scene_commit_engine"
down_revision = "0017_state_delta_validation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scene_commits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("proposal_id", sa.String(36), sa.ForeignKey("scene_proposals.id"), nullable=False),
        sa.Column("performance_id", sa.String(36), sa.ForeignKey("scene_performances.id"), nullable=False),
        sa.Column("scene_id", sa.String(36), sa.ForeignKey("scenes.id"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("delta_batch_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("pre_snapshot_id", sa.String(36), sa.ForeignKey("world_snapshots.id")),
        sa.Column("post_snapshot_id", sa.String(36), sa.ForeignKey("world_snapshots.id")),
        sa.Column("checkpoint_id", sa.String(36), sa.ForeignKey("scene_state_checkpoints.id")),
        sa.Column("pre_world_fingerprint", sa.String(120)),
        sa.Column("post_world_fingerprint", sa.String(120)),
        sa.Column("source_fingerprint", sa.String(120), nullable=False),
        sa.Column("commit_fingerprint", sa.String(120)),
        sa.Column("applied_delta_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_knowledge_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_memory_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime()),
        sa.UniqueConstraint("project_id", "performance_id", name="uq_scene_commit_project_performance"),
        sa.UniqueConstraint("scene_id", name="uq_scene_commit_scene"),
    )
    op.create_index("ix_scene_commits_project_id", "scene_commits", ["project_id"])
    op.create_index("ix_scene_commits_status", "scene_commits", ["status"])
    with op.batch_alter_table("state_delta_batches") as batch:
        batch.add_column(sa.Column("applied_scene_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("applied_commit_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("applied_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key("fk_state_delta_batches_applied_scene", "scenes", ["applied_scene_id"], ["id"])
        batch.create_foreign_key("fk_state_delta_batches_applied_commit", "scene_commits", ["applied_commit_id"], ["id"])
        batch.create_index("ix_state_delta_batches_applied_scene_id", ["applied_scene_id"])
        batch.create_index("ix_state_delta_batches_applied_commit_id", ["applied_commit_id"])


def downgrade():
    with op.batch_alter_table("state_delta_batches") as batch:
        batch.drop_index("ix_state_delta_batches_applied_commit_id")
        batch.drop_index("ix_state_delta_batches_applied_scene_id")
        batch.drop_constraint("fk_state_delta_batches_applied_commit", type_="foreignkey")
        batch.drop_constraint("fk_state_delta_batches_applied_scene", type_="foreignkey")
        batch.drop_column("applied_at")
        batch.drop_column("applied_commit_id")
        batch.drop_column("applied_scene_id")
    op.drop_index("ix_scene_commits_status", table_name="scene_commits")
    op.drop_index("ix_scene_commits_project_id", table_name="scene_commits")
    op.drop_table("scene_commits")
