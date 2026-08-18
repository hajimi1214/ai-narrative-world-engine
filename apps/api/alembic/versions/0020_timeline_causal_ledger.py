"""derived timeline and causal ledger"""
from alembic import op
import sqlalchemy as sa

revision = "0020_timeline_causal_ledger"
down_revision = "0019_scene_checkpoint_unification"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "timeline_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False), sa.Column("source_type", sa.String(60), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False), sa.Column("source_key", sa.String(500), nullable=False),
        sa.Column("scene_id", sa.String(36), sa.ForeignKey("scenes.id")), sa.Column("sequence", sa.Integer()), sa.Column("ordinal", sa.Integer()),
        sa.Column("world_time", sa.DateTime()), sa.Column("origin", sa.String(30), nullable=False), sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("supersedes_event_id", sa.String(36), sa.ForeignKey("timeline_events.id")), sa.Column("checkpoint_id", sa.String(36), sa.ForeignKey("scene_state_checkpoints.id")),
        sa.Column("target_type", sa.String(40)), sa.Column("target_id", sa.String(36)), sa.Column("path", sa.String(500)),
        sa.Column("before_value", sa.JSON()), sa.Column("after_value", sa.JSON()), sa.Column("structured_payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("event_fingerprint", sa.String(120), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", "source_key", name="uq_timeline_event_project_source_key"),
    )
    op.create_index("ix_timeline_events_project_id", "timeline_events", ["project_id"])
    op.create_index("ix_timeline_events_event_fingerprint", "timeline_events", ["event_fingerprint"])
    op.create_index("ix_timeline_event_project_sequence_active", "timeline_events", ["project_id", "sequence", "active"])
    op.create_index("ix_timeline_event_project_type_active", "timeline_events", ["project_id", "event_type", "active"])
    op.create_index("ix_timeline_event_target_path", "timeline_events", ["target_type", "target_id", "path"])
    op.create_table(
        "causal_links",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("cause_type", sa.String(40), nullable=False), sa.Column("cause_id", sa.String(36), nullable=False), sa.Column("effect_type", sa.String(40), nullable=False), sa.Column("effect_id", sa.String(36), nullable=False),
        sa.Column("edge_kind", sa.String(30), nullable=False), sa.Column("relation_type", sa.String(60), nullable=False), sa.Column("scene_id", sa.String(36), sa.ForeignKey("scenes.id")), sa.Column("sequence", sa.Integer()),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source_key", sa.String(700), nullable=False), sa.Column("link_fingerprint", sa.String(120), nullable=False), sa.Column("replay_session_id", sa.String(36), sa.ForeignKey("retcon_replay_sessions.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")), sa.UniqueConstraint("project_id", "source_key", name="uq_causal_link_project_source_key"),
    )
    op.create_index("ix_causal_links_project_id", "causal_links", ["project_id"])
    op.create_index("ix_causal_links_link_fingerprint", "causal_links", ["link_fingerprint"])


def downgrade():
    op.drop_index("ix_causal_links_link_fingerprint", table_name="causal_links"); op.drop_index("ix_causal_links_project_id", table_name="causal_links"); op.drop_table("causal_links")
    op.drop_index("ix_timeline_event_target_path", table_name="timeline_events"); op.drop_index("ix_timeline_event_project_type_active", table_name="timeline_events"); op.drop_index("ix_timeline_event_project_sequence_active", table_name="timeline_events"); op.drop_index("ix_timeline_events_event_fingerprint", table_name="timeline_events"); op.drop_index("ix_timeline_events_project_id", table_name="timeline_events"); op.drop_table("timeline_events")
