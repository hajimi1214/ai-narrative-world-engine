"""add explicit per-character agent profile settings

Revision ID: 0036_character_agent_profiles
Revises: 0035_story_plan_detail_fields
"""
from alembic import op
import sqlalchemy as sa

revision = "0036_character_agent_profiles"
down_revision = "0035_story_plan_detail_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("characters") as batch:
        batch.add_column(sa.Column("agent_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("agent_profile", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("characters", "agent_profile")
    op.drop_column("characters", "agent_enabled")
