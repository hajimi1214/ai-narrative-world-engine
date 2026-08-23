"""add local single-user auto director checkpoints

Revision ID: 0039_auto_director
Revises: 0038_research_chunk_embeddings
"""
from alembic import op
import sqlalchemy as sa

revision = "0039_auto_director"
down_revision = "0038_research_chunk_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auto_director_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="CREATED"),
        sa.Column("current_stage", sa.String(30), nullable=False, server_default="IDEA"),
        sa.Column("current_chapter_id", sa.String(36), sa.ForeignKey("chapters.id"), nullable=True),
        sa.Column("run_mode", sa.String(30), nullable=False, server_default="LOCAL_SINGLE_USER"),
        sa.Column("pause_reason", sa.String(500)),
        sa.Column("next_action", sa.String(500)),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("token_usage", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("settings", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("context", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime()),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_auto_director_run_project_idempotency"),
    )
    op.create_index("ix_auto_director_runs_project_id", "auto_director_runs", ["project_id"])
    op.create_index("ix_auto_director_run_project_status", "auto_director_runs", ["project_id", "status"])
    op.create_table(
        "auto_director_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("auto_director_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("input_fingerprint", sa.String(120), nullable=False),
        sa.Column("output_artifact_id", sa.String(120)),
        sa.Column("output_payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error_code", sa.String(120)),
        sa.Column("error_summary", sa.Text()),
        sa.Column("token_usage", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.UniqueConstraint("run_id", "stage", "input_fingerprint", name="uq_auto_director_step_checkpoint"),
    )
    op.create_index("ix_auto_director_steps_run_id", "auto_director_steps", ["run_id"])
    op.create_index("ix_auto_director_step_run_stage", "auto_director_steps", ["run_id", "stage"])


def downgrade() -> None:
    op.drop_index("ix_auto_director_step_run_stage", table_name="auto_director_steps")
    op.drop_index("ix_auto_director_steps_run_id", table_name="auto_director_steps")
    op.drop_table("auto_director_steps")
    op.drop_index("ix_auto_director_run_project_status", table_name="auto_director_runs")
    op.drop_index("ix_auto_director_runs_project_id", table_name="auto_director_runs")
    op.drop_table("auto_director_runs")
