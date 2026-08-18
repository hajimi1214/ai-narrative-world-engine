"""selective scene replay foundation"""
from alembic import op
import sqlalchemy as sa

revision = "0012_selective_scene_replay_foundation"
down_revision = "0011_retcon_apply_cognition_baseline"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("scenes", sa.Column("history_status", sa.String(30), nullable=False, server_default="ACTIVE"))
    op.add_column("scenes", sa.Column("superseded_by_scene_id", sa.String(36)))
    for table in ("character_knowledge", "character_memories", "character_decisions", "scene_performance_turns", "world_resolutions"):
        op.add_column(table, sa.Column("replay_session_id", sa.String(36)))
        op.add_column(table, sa.Column("replay_of_id", sa.String(36)))
    op.create_table(
        "retcon_replay_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("retcon_application_id", sa.String(36), sa.ForeignKey("retcon_applications.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("current_sequence", sa.Integer()),
        sa.Column("baseline_snapshot_id", sa.String(36), sa.ForeignKey("world_snapshots.id")),
        sa.Column("baseline_fingerprint", sa.String(120), nullable=False),
        sa.Column("current_fingerprint", sa.String(120)),
        sa.Column("queue", sa.JSON(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_report", sa.JSON()),
        sa.Column("started_at", sa.DateTime()), sa.Column("completed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("retcon_application_id", name="uq_replay_session_application"),
    )
    op.create_index("ix_retcon_replay_sessions_project_id", "retcon_replay_sessions", ["project_id"])
    op.create_table(
        "replay_scene_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("replay_session_id", sa.String(36), sa.ForeignKey("retcon_replay_sessions.id"), nullable=False),
        sa.Column("original_scene_id", sa.String(36), sa.ForeignKey("scenes.id"), nullable=False),
        sa.Column("original_sequence", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(30), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("input_fingerprint", sa.String(120)),
        sa.Column("new_decision_ids", sa.JSON(), nullable=False), sa.Column("new_turn_ids", sa.JSON(), nullable=False),
        sa.Column("new_resolution_ids", sa.JSON(), nullable=False), sa.Column("new_knowledge_ids", sa.JSON(), nullable=False), sa.Column("new_memory_ids", sa.JSON(), nullable=False),
        sa.Column("replacement_scene_id", sa.String(36), sa.ForeignKey("scenes.id")),
        sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime()), sa.Column("completed_at", sa.DateTime()),
    )
    op.create_index("ix_replay_scene_runs_project_id", "replay_scene_runs", ["project_id"])
    op.create_index("ix_replay_scene_runs_replay_session_id", "replay_scene_runs", ["replay_session_id"])

def downgrade():
    op.drop_index("ix_replay_scene_runs_replay_session_id", table_name="replay_scene_runs")
    op.drop_index("ix_replay_scene_runs_project_id", table_name="replay_scene_runs")
    op.drop_table("replay_scene_runs")
    op.drop_index("ix_retcon_replay_sessions_project_id", table_name="retcon_replay_sessions")
    op.drop_table("retcon_replay_sessions")
    for table in ("world_resolutions", "scene_performance_turns", "character_decisions", "character_memories", "character_knowledge"):
        op.drop_column(table, "replay_of_id"); op.drop_column(table, "replay_session_id")
    op.drop_column("scenes", "superseded_by_scene_id"); op.drop_column("scenes", "history_status")
