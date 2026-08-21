"""incremental narrative structure projection

Revision ID: 0032_narrative_structure_projection
Revises: 0031_incremental_formal_state_identity
"""
from alembic import op
import sqlalchemy as sa

revision = "0032_narrative_structure_projection"
down_revision = "0031_incremental_formal_state_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_narrative_structure_projections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("protocol_version", sa.String(60), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="DIRTY"),
        sa.Column("config_fingerprint", sa.String(120)),
        sa.Column("source_feature_fingerprint", sa.String(120)),
        sa.Column("feature_accumulator", sa.JSON(), nullable=False),
        sa.Column("structure_fingerprint", sa.String(120)),
        sa.Column("active_revision_id", sa.String(36), sa.ForeignKey("narrative_structure_revisions.id")),
        sa.Column("built_through_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sealed_through_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tail_start_sequence", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("dirty_from_sequence", sa.Integer()),
        sa.Column("dirty_reason", sa.String(200)),
        sa.Column("last_rebuilt_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", name="uq_narrative_structure_projection_project"),
    )
    op.create_index("ix_project_narrative_structure_projections_project_id", "project_narrative_structure_projections", ["project_id"])
    op.create_table(
        "narrative_structure_scene_features",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("scene_id", sa.String(36), sa.ForeignKey("scenes.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("world_time", sa.String(80)),
        sa.Column("location_id", sa.String(36)),
        sa.Column("participant_ids", sa.JSON(), nullable=False),
        sa.Column("thread_ids", sa.JSON(), nullable=False),
        sa.Column("primary_thread_id", sa.String(36)),
        sa.Column("proposal_type", sa.String(60)),
        sa.Column("state_change_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state_change_targets", sa.JSON(), nullable=False),
        sa.Column("state_change_paths", sa.JSON(), nullable=False),
        sa.Column("thread_state_event_ids", sa.JSON(), nullable=False),
        sa.Column("checkpoint_fingerprint", sa.String(120)),
        sa.Column("source_fingerprint", sa.String(120), nullable=False),
        sa.Column("feature_fingerprint", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", "scene_id", name="uq_narrative_structure_feature_scene"),
        sa.UniqueConstraint("project_id", "sequence", name="uq_narrative_structure_feature_sequence"),
    )
    op.create_index("ix_narrative_structure_scene_features_project_id", "narrative_structure_scene_features", ["project_id"])
    op.create_index("ix_narrative_structure_scene_features_scene_id", "narrative_structure_scene_features", ["scene_id"])
    op.create_index("ix_narrative_structure_scene_features_feature_fingerprint", "narrative_structure_scene_features", ["feature_fingerprint"])
    op.create_index("ix_narrative_structure_feature_project_active_sequence", "narrative_structure_scene_features", ["project_id", "active", "sequence"])


def downgrade() -> None:
    op.drop_index("ix_narrative_structure_feature_project_active_sequence", table_name="narrative_structure_scene_features")
    op.drop_index("ix_narrative_structure_scene_features_feature_fingerprint", table_name="narrative_structure_scene_features")
    op.drop_index("ix_narrative_structure_scene_features_scene_id", table_name="narrative_structure_scene_features")
    op.drop_index("ix_narrative_structure_scene_features_project_id", table_name="narrative_structure_scene_features")
    op.drop_table("narrative_structure_scene_features")
    op.drop_index("ix_project_narrative_structure_projections_project_id", table_name="project_narrative_structure_projections")
    op.drop_table("project_narrative_structure_projections")
