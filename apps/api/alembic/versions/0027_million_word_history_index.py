"""million word history index

Revision ID: 0027_million_word_history_index
Revises: 0026_character_memory_hybrid_retrieval
"""
from alembic import op
import sqlalchemy as sa


revision = "0027_million_word_history_index"
down_revision = "0026_character_memory_hybrid_retrieval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_history_projections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("protocol_version", sa.String(length=60), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("built_through_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_scene_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_scene_id", sa.String(length=36), sa.ForeignKey("scenes.id")),
        sa.Column("recent_scene_signatures", sa.JSON(), nullable=False),
        sa.Column("thread_stats", sa.JSON(), nullable=False),
        sa.Column("character_stats", sa.JSON(), nullable=False),
        sa.Column("source_history_fingerprint", sa.String(length=120)),
        sa.Column("projection_fingerprint", sa.String(length=120)),
        sa.Column("dirty_from_sequence", sa.Integer()),
        sa.Column("last_rebuilt_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", name="uq_project_history_projection_project"),
    )
    op.create_index("ix_project_history_projections_project_id", "project_history_projections", ["project_id"])
    op.create_table(
        "scene_history_features",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("scene_id", sa.String(length=36), sa.ForeignKey("scenes.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("world_time", sa.DateTime()), sa.Column("location_id", sa.String(length=36)),
        sa.Column("participant_ids", sa.JSON(), nullable=False), sa.Column("thread_ids", sa.JSON(), nullable=False),
        sa.Column("proposal_type", sa.String(length=30)), sa.Column("primary_thread_id", sa.String(length=36)),
        sa.Column("checkpoint_id", sa.String(length=36), sa.ForeignKey("scene_state_checkpoints.id")),
        sa.Column("checkpoint_fingerprint", sa.String(length=120)),
        sa.Column("state_change_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state_change_targets", sa.JSON(), nullable=False), sa.Column("state_change_paths", sa.JSON(), nullable=False),
        sa.Column("thread_state_event_ids", sa.JSON(), nullable=False), sa.Column("feature_fingerprint", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", "scene_id", name="uq_scene_history_feature_project_scene"),
    )
    op.create_index("ix_scene_history_features_project_id", "scene_history_features", ["project_id"])
    op.create_index("ix_scene_history_feature_project_sequence", "scene_history_features", ["project_id", "sequence"])
    op.create_index("ix_scene_history_features_feature_fingerprint", "scene_history_features", ["feature_fingerprint"])
    op.create_table(
        "current_state_change_heads",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("timeline_event_id", sa.String(length=36), sa.ForeignKey("timeline_events.id"), nullable=False),
        sa.Column("scene_id", sa.String(length=36), sa.ForeignKey("scenes.id")),
        sa.Column("sequence", sa.Integer()), sa.Column("ordinal", sa.Integer()),
        sa.Column("target_type", sa.String(length=40), nullable=False), sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False), sa.Column("event_fingerprint", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", "target_type", "target_id", "path", name="uq_current_state_change_head_path"),
    )
    op.create_index("ix_current_state_change_heads_project_id", "current_state_change_heads", ["project_id"])
    op.create_index("ix_current_state_change_head_project_target_path", "current_state_change_heads", ["project_id", "target_type", "target_id", "path"])
    op.create_index("ix_scaling_scene_current_sequence", "scenes", ["project_id", "sequence"], postgresql_where=sa.text("history_status = 'ACTIVE' AND status = 'OCCURRED'"), sqlite_where=sa.text("history_status = 'ACTIVE' AND status = 'OCCURRED'"))
    op.create_index("ix_scaling_timeline_current_state", "timeline_events", ["project_id", "event_type", "active", "target_type", "target_id", "path", "sequence", "ordinal"])
    op.create_index("ix_scaling_causal_current_sequence", "causal_links", ["project_id", "relation_type", "active", "sequence", "cause_type", "cause_id"])
    op.create_index("ix_scaling_checkpoint_active_scene", "scene_state_checkpoints", ["project_id", "scene_id", "active"])


def downgrade() -> None:
    op.drop_index("ix_scaling_checkpoint_active_scene", table_name="scene_state_checkpoints")
    op.drop_index("ix_scaling_causal_current_sequence", table_name="causal_links")
    op.drop_index("ix_scaling_timeline_current_state", table_name="timeline_events")
    op.drop_index("ix_scaling_scene_current_sequence", table_name="scenes")
    op.drop_table("current_state_change_heads")
    op.drop_table("scene_history_features")
    op.drop_table("project_history_projections")
