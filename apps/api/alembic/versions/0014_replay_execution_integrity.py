"""replay execution integrity"""
from alembic import op
import sqlalchemy as sa

revision = "0014_replay_execution_integrity"
down_revision = "0013_replay_historical_state_integrity"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("scene_state_checkpoints", sa.Column("capture_protocol_version", sa.Integer(), nullable=False, server_default="1"))
    op.create_table(
        "scene_execution_bindings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("scene_id", sa.String(length=36), sa.ForeignKey("scenes.id"), nullable=False),
        sa.Column("performance_id", sa.String(length=36), sa.ForeignKey("scene_performances.id"), nullable=False),
        sa.Column("replay_session_id", sa.String(length=36), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("scene_id", "active", name="uq_scene_execution_binding_active"),
    )
    op.create_index("ix_scene_execution_bindings_project_id", "scene_execution_bindings", ["project_id"])
    op.create_index("uq_scene_active_sequence", "scenes", ["project_id", "sequence"], unique=True, postgresql_where=sa.text("history_status = 'ACTIVE'"))

def downgrade():
    op.drop_index("uq_scene_active_sequence", table_name="scenes")
    op.drop_index("ix_scene_execution_bindings_project_id", table_name="scene_execution_bindings")
    op.drop_table("scene_execution_bindings")
    op.drop_column("scene_state_checkpoints", "capture_protocol_version")
