"""retcon apply and cognition invalidation baseline"""
from alembic import op
import sqlalchemy as sa

revision = "0011_retcon_apply_cognition_baseline"
down_revision = "0010_retcon_planning_integrity"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "retcon_applications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("retcon_request_id", sa.String(36), sa.ForeignKey("retcon_requests.id"), nullable=False),
        sa.Column("retcon_plan_id", sa.String(36), sa.ForeignKey("retcon_impact_plans.id"), nullable=False),
        sa.Column("source_revision_id", sa.String(36), sa.ForeignKey("world_revisions.id"), nullable=False),
        sa.Column("revision_application_id", sa.String(36), sa.ForeignKey("revision_applications.id")),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("plan_basis_fingerprint", sa.String(120), nullable=False),
        sa.Column("pre_apply_world_fingerprint", sa.String(120), nullable=False),
        sa.Column("post_apply_world_fingerprint", sa.String(120)),
        sa.Column("cognition_summary", sa.JSON(), nullable=False),
        sa.Column("replay_summary", sa.JSON(), nullable=False),
        sa.Column("failure_report", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("applied_at", sa.DateTime()),
        sa.Column("rolled_back_at", sa.DateTime()),
        sa.UniqueConstraint("retcon_request_id", name="uq_retcon_application_request"),
    )
    op.create_index("ix_retcon_applications_project_id", "retcon_applications", ["project_id"])
    op.create_index("ix_retcon_applications_retcon_request_id", "retcon_applications", ["retcon_request_id"])
    op.create_table(
        "retcon_cognition_invalidations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("retcon_application_id", sa.String(36), sa.ForeignKey("retcon_applications.id"), nullable=False),
        sa.Column("character_id", sa.String(36), sa.ForeignKey("characters.id"), nullable=False),
        sa.Column("resource_type", sa.String(20), nullable=False),
        sa.Column("resource_id", sa.String(36), nullable=False),
        sa.Column("source_impact_item_id", sa.String(36), sa.ForeignKey("retcon_impact_items.id")),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("original_semantic_fingerprint", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_retcon_cognition_invalidations_project_id", "retcon_cognition_invalidations", ["project_id"])
    op.create_index("ix_retcon_cognition_invalidations_retcon_application_id", "retcon_cognition_invalidations", ["retcon_application_id"])
    op.create_index("ix_retcon_cognition_invalidations_character_id", "retcon_cognition_invalidations", ["character_id"])

def downgrade():
    op.drop_index("ix_retcon_cognition_invalidations_character_id", table_name="retcon_cognition_invalidations")
    op.drop_index("ix_retcon_cognition_invalidations_retcon_application_id", table_name="retcon_cognition_invalidations")
    op.drop_index("ix_retcon_cognition_invalidations_project_id", table_name="retcon_cognition_invalidations")
    op.drop_table("retcon_cognition_invalidations")
    op.drop_index("ix_retcon_applications_retcon_request_id", table_name="retcon_applications")
    op.drop_index("ix_retcon_applications_project_id", table_name="retcon_applications")
    op.drop_table("retcon_applications")
