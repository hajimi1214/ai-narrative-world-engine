"""historical checkpoints and replay sandbox state"""
from alembic import op
import sqlalchemy as sa
revision = "0013_replay_historical_state_integrity"
down_revision = "0012_selective_scene_replay_foundation"
branch_labels = None
depends_on = None
def upgrade():
    if op.get_bind().dialect.name == "sqlite":
        op.add_column("retcon_replay_sessions", sa.Column("staged_world_state", sa.JSON(), nullable=False, server_default="{}"))
        op.add_column("retcon_replay_sessions", sa.Column("pre_commit_snapshot_id", sa.String(36)))
        op.add_column("retcon_replay_sessions", sa.Column("post_commit_snapshot_id", sa.String(36)))
    else:
        op.add_column("retcon_replay_sessions", sa.Column("staged_world_state", sa.JSON(), nullable=False, server_default="{}"))
        op.add_column("retcon_replay_sessions", sa.Column("pre_commit_snapshot_id", sa.String(36), sa.ForeignKey("world_snapshots.id")))
        op.add_column("retcon_replay_sessions", sa.Column("post_commit_snapshot_id", sa.String(36), sa.ForeignKey("world_snapshots.id")))
    op.create_table("scene_state_checkpoints",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("scene_id", sa.String(36), sa.ForeignKey("scenes.id"), nullable=False), sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("pre_snapshot_id", sa.String(36), sa.ForeignKey("world_snapshots.id"), nullable=False), sa.Column("post_snapshot_id", sa.String(36), sa.ForeignKey("world_snapshots.id"), nullable=False),
        sa.Column("current_scene_id", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", "scene_id", name="uq_scene_state_checkpoint"))
    op.create_index("ix_scene_state_checkpoints_project_id", "scene_state_checkpoints", ["project_id"])
def downgrade():
    op.drop_index("ix_scene_state_checkpoints_project_id", table_name="scene_state_checkpoints"); op.drop_table("scene_state_checkpoints")
    with op.batch_alter_table("retcon_replay_sessions") as batch:
        batch.drop_column("post_commit_snapshot_id"); batch.drop_column("pre_commit_snapshot_id"); batch.drop_column("staged_world_state")
