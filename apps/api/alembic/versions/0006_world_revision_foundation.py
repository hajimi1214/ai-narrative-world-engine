"""world revision preview foundation"""
from alembic import op
import sqlalchemy as sa

revision = "0006_world_revision_foundation"
down_revision = "0005_world_resolution_foundation"
branch_labels = None
depends_on = None

revision_status = sa.Enum("DRAFT", "PREVIEWED", "STALE", "CANCELLED", name="revisionstatus")

def upgrade() -> None:
    op.create_table("world_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False), sa.Column("description", sa.Text()),
        sa.Column("status", revision_status, nullable=False), sa.Column("base_state_fingerprint", sa.String(100)),
        sa.Column("change_set", sa.JSON(), nullable=False), sa.Column("normalized_changes", sa.JSON(), nullable=False), sa.Column("impact_report", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False), sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False))
    op.create_index("ix_world_revisions_project_id", "world_revisions", ["project_id"])

def downgrade() -> None:
    op.drop_index("ix_world_revisions_project_id", table_name="world_revisions")
    op.drop_table("world_revisions")
    revision_status.drop(op.get_bind(), checkfirst=True)
