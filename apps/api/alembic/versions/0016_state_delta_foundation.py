"""state delta foundation"""
from alembic import op
import sqlalchemy as sa

revision = "0016_state_delta_foundation"
down_revision = "0015_replay_runtime_integrity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "state_delta_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("source_scene_proposal_id", sa.String(36), sa.ForeignKey("scene_proposals.id")),
        sa.Column("source_performance_id", sa.String(36), sa.ForeignKey("scene_performances.id")),
        sa.Column("source_turn_id", sa.String(36), sa.ForeignKey("scene_performance_turns.id")),
        sa.Column("source_resolution_id", sa.String(36), sa.ForeignKey("world_resolutions.id")),
        sa.Column("base_world_fingerprint", sa.String(120), nullable=False),
        sa.Column("input_fingerprint", sa.String(120), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("derivation_version", sa.String(40), nullable=False),
        sa.Column("derivation_report", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", "input_fingerprint", name="uq_state_delta_batch_project_input"),
    )
    op.create_index("ix_state_delta_batches_project_id", "state_delta_batches", ["project_id"])
    op.create_index("ix_state_delta_batches_source_resolution_id", "state_delta_batches", ["source_resolution_id"])
    op.create_index("ix_state_delta_batches_status", "state_delta_batches", ["status"])
    op.create_index("ix_state_delta_batches_input_fingerprint", "state_delta_batches", ["input_fingerprint"])
    op.create_table(
        "state_delta_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("state_delta_batches.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("domain", sa.String(50), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column("before_value", sa.JSON()),
        sa.Column("after_value", sa.JSON()),
        sa.Column("causal_reason", sa.Text(), nullable=False),
        sa.Column("source_turn_id", sa.String(36), sa.ForeignKey("scene_performance_turns.id")),
        sa.Column("source_resolution_id", sa.String(36), sa.ForeignKey("world_resolutions.id")),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("semantic_fingerprint", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("batch_id", "ordinal", name="uq_state_delta_item_batch_ordinal"),
    )
    op.create_index("ix_state_delta_items_project_id", "state_delta_items", ["project_id"])
    op.create_index("ix_state_delta_items_batch_id", "state_delta_items", ["batch_id"])
    op.create_index("ix_state_delta_items_semantic_fingerprint", "state_delta_items", ["semantic_fingerprint"])


def downgrade():
    op.drop_index("ix_state_delta_items_semantic_fingerprint", table_name="state_delta_items")
    op.drop_index("ix_state_delta_items_batch_id", table_name="state_delta_items")
    op.drop_index("ix_state_delta_items_project_id", table_name="state_delta_items")
    op.drop_table("state_delta_items")
    op.drop_index("ix_state_delta_batches_input_fingerprint", table_name="state_delta_batches")
    op.drop_index("ix_state_delta_batches_status", table_name="state_delta_batches")
    op.drop_index("ix_state_delta_batches_source_resolution_id", table_name="state_delta_batches")
    op.drop_index("ix_state_delta_batches_project_id", table_name="state_delta_batches")
    op.drop_table("state_delta_batches")
