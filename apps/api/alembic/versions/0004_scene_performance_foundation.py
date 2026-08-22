"""scene performance rehearsal foundation"""
from alembic import op
import sqlalchemy as sa

revision = "0004_scene_performance_foundation"
down_revision = "0003_character_mind_foundation"
branch_labels = None
depends_on = None

performance_mode = sa.Enum("HEURISTIC", "LLM", name="performancemode")
performance_status = sa.Enum("READY", "RUNNING", "AWAITING_WORLD", "PAUSED", "COMPLETED", "INVALIDATED", "FAILED", name="performancestatus")
action_visibility = sa.Enum("PUBLIC", "TARGETED", "COVERT", "PRIVATE", name="actionvisibility")

def upgrade() -> None:
    # SQLite does not support ALTER COLUMN TYPE, and its VARCHAR length is not
    # enforced. PostgreSQL still needs the wider revision column.
    if op.get_bind().dialect.name != "sqlite":
        op.alter_column("alembic_version", "version_num", type_=sa.String(64), existing_type=sa.String(32), existing_nullable=False)
    op.create_table("scene_performances",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("scene_proposal_id", sa.String(36), sa.ForeignKey("scene_proposals.id"), nullable=False), sa.Column("take_number", sa.Integer(), nullable=False),
        sa.Column("proposal_context_fingerprint", sa.String(100), nullable=False), sa.Column("mode", performance_mode, nullable=False),
        sa.Column("status", performance_status, nullable=False, server_default="READY"), sa.Column("participant_order", sa.JSON(), nullable=False),
        sa.Column("active_participant_ids", sa.JSON(), nullable=False), sa.Column("max_turns", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("stop_reason", sa.String(100)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False), sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("scene_proposal_id", "take_number", name="uq_scene_performances_proposal_take"))
    op.create_index("ix_scene_performances_project_id", "scene_performances", ["project_id"])
    op.create_index("ix_scene_performances_proposal_id", "scene_performances", ["scene_proposal_id"])
    op.create_table("scene_performance_turns",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("performance_id", sa.String(36), sa.ForeignKey("scene_performances.id"), nullable=False), sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("actor_character_id", sa.String(36), sa.ForeignKey("characters.id"), nullable=False), sa.Column("actor_context_fingerprint", sa.String(100), nullable=False),
        sa.Column("character_decision_id", sa.String(36), sa.ForeignKey("character_decisions.id"), nullable=False), sa.Column("action_visibility", action_visibility, nullable=False),
        sa.Column("observable_action", sa.Text()), sa.Column("spoken_content", sa.Text()), sa.Column("recipient_character_ids", sa.JSON(), nullable=False),
        sa.Column("requires_world_resolution", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("world_resolution_request", sa.JSON()),
        sa.Column("validation_result", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("performance_id", "sequence", name="uq_scene_performance_turns_sequence"))
    op.create_index("ix_scene_performance_turns_project_id", "scene_performance_turns", ["project_id"])
    op.create_index("ix_scene_performance_turns_performance_id", "scene_performance_turns", ["performance_id"])

def downgrade() -> None:
    op.drop_index("ix_scene_performance_turns_performance_id", table_name="scene_performance_turns")
    op.drop_index("ix_scene_performance_turns_project_id", table_name="scene_performance_turns")
    op.drop_table("scene_performance_turns")
    op.drop_index("ix_scene_performances_proposal_id", table_name="scene_performances")
    op.drop_index("ix_scene_performances_project_id", table_name="scene_performances")
    op.drop_table("scene_performances")
    for enum in (action_visibility, performance_status, performance_mode): enum.drop(op.get_bind(), checkfirst=True)
