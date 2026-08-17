"""recovery candidate foundation"""
from alembic import op
import sqlalchemy as sa

revision = "0008_recovery_candidate_foundation"
down_revision = "0007_world_version_execution_foundation"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("recovery_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("source_trace_id", sa.String(36), sa.ForeignKey("execution_traces.id"), nullable=False, unique=True),
        sa.Column("stage", sa.String(50), nullable=False), sa.Column("candidate_type", sa.String(50), nullable=False),
        sa.Column("source_type", sa.String(100)), sa.Column("source_id", sa.String(36)),
        sa.Column("context_fingerprint", sa.String(120), nullable=False), sa.Column("context_locator", sa.JSON(), nullable=False),
        sa.Column("initial_error_code", sa.String(100), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("current_version_number", sa.Integer(), nullable=False), sa.Column("adopted_resource_type", sa.String(100)),
        sa.Column("adopted_resource_id", sa.String(36)), sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False))
    op.create_index("ix_recovery_candidates_project_id", "recovery_candidates", ["project_id"])
    op.create_table("recovery_candidate_versions",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("candidate_id", sa.String(36), sa.ForeignKey("recovery_candidates.id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False), sa.Column("origin", sa.String(30), nullable=False),
        sa.Column("parent_version_id", sa.String(36), sa.ForeignKey("recovery_candidate_versions.id")), sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_fingerprint", sa.String(120), nullable=False), sa.Column("schema_valid", sa.Boolean(), nullable=False),
        sa.Column("constraint_valid", sa.Boolean(), nullable=False), sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("repair_trace_id", sa.String(36), sa.ForeignKey("execution_traces.id")), sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("candidate_id", "version_number"))
    op.create_index("ix_recovery_candidate_versions_candidate_id", "recovery_candidate_versions", ["candidate_id"])

def downgrade():
    op.drop_index("ix_recovery_candidate_versions_candidate_id", table_name="recovery_candidate_versions")
    op.drop_table("recovery_candidate_versions")
    op.drop_index("ix_recovery_candidates_project_id", table_name="recovery_candidates")
    op.drop_table("recovery_candidates")
