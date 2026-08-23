"""persist planning scene beat references on character performance turns

Revision ID: 0037_scene_beat_execution
Revises: 0036_character_agent_profiles
"""
from alembic import op
import sqlalchemy as sa

revision = "0037_scene_beat_execution"
down_revision = "0036_character_agent_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("scene_performance_turns") as batch:
        batch.add_column(sa.Column("scene_beat_refs", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("scene_performance_turns", "scene_beat_refs")
