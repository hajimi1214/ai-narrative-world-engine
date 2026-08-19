"""persistent bounded autonomous world loop audit"""
from alembic import op
import sqlalchemy as sa

revision = "0021_autonomous_world_loop"
down_revision = "0020_timeline_causal_ledger"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "autonomous_world_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False), sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("scene_budget", sa.Integer(), nullable=False), sa.Column("committed_scene_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_turns_per_scene", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("performance_mode", sa.String(20), nullable=False), sa.Column("resolver_mode", sa.String(20), nullable=False),
        sa.Column("start_sequence", sa.Integer(), nullable=False, server_default="0"), sa.Column("last_committed_sequence", sa.Integer()),
        sa.Column("start_world_fingerprint", sa.String(120), nullable=False), sa.Column("current_world_fingerprint", sa.String(120), nullable=False),
        sa.Column("start_history_fingerprint", sa.String(120), nullable=False), sa.Column("current_history_fingerprint", sa.String(120), nullable=False), sa.Column("autonomous_run_fingerprint", sa.String(120), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("stop_reason", sa.String(120)),
        sa.Column("last_error_code", sa.String(120)), sa.Column("last_error_detail", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("client_request_id", sa.String(200)), sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("started_at", sa.DateTime()), sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime()), sa.Column("lock_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_autonomous_world_runs_project_id", "autonomous_world_runs", ["project_id"])
    op.create_index("ix_autonomous_world_runs_status", "autonomous_world_runs", ["status"])
    op.create_index("ix_autonomous_world_runs_active", "autonomous_world_runs", ["active"])
    op.create_index("uq_autonomous_run_project_active", "autonomous_world_runs", ["project_id"], unique=True, postgresql_where=sa.text("active = true"), sqlite_where=sa.text("active = 1"))
    op.create_table(
        "autonomous_world_steps",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("autonomous_world_runs.id"), nullable=False), sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False), sa.Column("request_key", sa.String(200), nullable=False), sa.Column("request_offset", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage", sa.String(80), nullable=False), sa.Column("scene_sequence_before", sa.Integer(), nullable=False), sa.Column("scene_sequence_after", sa.Integer()),
        sa.Column("world_fingerprint_before", sa.String(120), nullable=False), sa.Column("world_fingerprint_after", sa.String(120)),
        sa.Column("step_input_fingerprint", sa.String(120)), sa.Column("step_output_fingerprint", sa.String(120)),
        sa.Column("director_context_fingerprint", sa.String(120)), sa.Column("gravity_fingerprint", sa.String(120)), sa.Column("candidate_key", sa.String(500)),
        sa.Column("proposal_id", sa.String(36), sa.ForeignKey("scene_proposals.id")), sa.Column("performance_id", sa.String(36), sa.ForeignKey("scene_performances.id")),
        sa.Column("scene_commit_id", sa.String(36), sa.ForeignKey("scene_commits.id")), sa.Column("scene_id", sa.String(36), sa.ForeignKey("scenes.id")), sa.Column("checkpoint_id", sa.String(36), sa.ForeignKey("scene_state_checkpoints.id")),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("resolution_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delta_batch_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")), sa.Column("recovery_candidate_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("stop_reason", sa.String(120)), sa.Column("error_code", sa.String(120)), sa.Column("error_detail", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")), sa.Column("started_at", sa.DateTime()), sa.Column("completed_at", sa.DateTime()),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_autonomous_step_run_ordinal"), sa.UniqueConstraint("run_id", "request_key", "request_offset", name="uq_autonomous_step_request"),
    )
    op.create_index("ix_autonomous_world_steps_project_id", "autonomous_world_steps", ["project_id"])
    op.create_index("ix_autonomous_world_steps_run_id", "autonomous_world_steps", ["run_id"])
    op.create_index("ix_autonomous_world_steps_status", "autonomous_world_steps", ["status"])


def downgrade():
    op.drop_index("ix_autonomous_world_steps_status", table_name="autonomous_world_steps")
    op.drop_index("ix_autonomous_world_steps_run_id", table_name="autonomous_world_steps")
    op.drop_index("ix_autonomous_world_steps_project_id", table_name="autonomous_world_steps")
    op.drop_table("autonomous_world_steps")
    op.drop_index("uq_autonomous_run_project_active", table_name="autonomous_world_runs")
    op.drop_index("ix_autonomous_world_runs_active", table_name="autonomous_world_runs")
    op.drop_index("ix_autonomous_world_runs_status", table_name="autonomous_world_runs")
    op.drop_index("ix_autonomous_world_runs_project_id", table_name="autonomous_world_runs")
    op.drop_table("autonomous_world_runs")
