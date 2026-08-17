"""retcon and replay planning foundation"""
from alembic import op
import sqlalchemy as sa

revision = "0009_retcon_replay_planning_foundation"
down_revision = "0008_recovery_candidate_foundation"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("retcon_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("source_revision_id", sa.String(36), sa.ForeignKey("world_revisions.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("current_plan_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False))
    op.create_index("ix_retcon_requests_project_id", "retcon_requests", ["project_id"])
    op.create_index("ix_retcon_requests_source_revision_id", "retcon_requests", ["source_revision_id"])
    op.create_table("retcon_impact_plans",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("retcon_request_id", sa.String(36), sa.ForeignKey("retcon_requests.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("parent_plan_id", sa.String(36), sa.ForeignKey("retcon_impact_plans.id")),
        sa.Column("basis_fingerprint", sa.String(120), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("earliest_affected_scene_id", sa.String(36)), sa.Column("earliest_affected_sequence", sa.Integer()),
        sa.Column("impact_summary", sa.JSON(), nullable=False), sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False))
    op.create_index("ix_retcon_impact_plans_project_id", "retcon_impact_plans", ["project_id"])
    op.create_index("ix_retcon_impact_plans_request_id", "retcon_impact_plans", ["retcon_request_id"])
    op.create_table("retcon_impact_items",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("plan_id", sa.String(36), sa.ForeignKey("retcon_impact_plans.id"), nullable=False),
        sa.Column("resource_type", sa.String(80), nullable=False), sa.Column("resource_id", sa.String(36), nullable=False),
        sa.Column("classification", sa.String(40), nullable=False), sa.Column("reason_code", sa.String(100), nullable=False),
        sa.Column("reason_summary", sa.Text(), nullable=False), sa.Column("character_id", sa.String(36)), sa.Column("scene_id", sa.String(36)),
        sa.Column("dependency_path", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False))
    op.create_index("ix_retcon_impact_items_plan_id", "retcon_impact_items", ["plan_id"])
    op.create_index("ix_retcon_impact_items_resource", "retcon_impact_items", ["resource_type", "resource_id"])

def downgrade():
    op.drop_index("ix_retcon_impact_items_resource", table_name="retcon_impact_items")
    op.drop_index("ix_retcon_impact_items_plan_id", table_name="retcon_impact_items")
    op.drop_table("retcon_impact_items")
    op.drop_index("ix_retcon_impact_plans_request_id", table_name="retcon_impact_plans")
    op.drop_index("ix_retcon_impact_plans_project_id", table_name="retcon_impact_plans")
    op.drop_table("retcon_impact_plans")
    op.drop_index("ix_retcon_requests_source_revision_id", table_name="retcon_requests")
    op.drop_index("ix_retcon_requests_project_id", table_name="retcon_requests")
    op.drop_table("retcon_requests")
