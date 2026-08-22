"""add editable volume and arc planning detail fields

Revision ID: 0035_story_plan_detail_fields
Revises: 0034_model_gateway_controls
"""
from alembic import op
import sqlalchemy as sa

revision = "0035_story_plan_detail_fields"
down_revision = "0034_model_gateway_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, columns in {
        "story_plan_volumes": [
            ("theme", sa.Text(), ""), ("core_question", sa.Text(), ""),
            ("major_conflict", sa.Text(), ""), ("start_state", sa.JSON(), "{}"),
            ("end_state", sa.JSON(), "{}"), ("main_thread", sa.Text(), ""),
            ("ending_turn", sa.Text(), ""), ("foreshadowing", sa.JSON(), "[]"),
        ],
        "story_plan_arcs": [
            ("core_question", sa.Text(), ""), ("start_state", sa.JSON(), "{}"),
            ("end_state", sa.JSON(), "{}"),
        ],
    }.items():
        for name, column_type, default in columns:
            op.add_column(table, sa.Column(name, column_type, nullable=False, server_default=default))
            op.alter_column(table, name, server_default=None)


def downgrade() -> None:
    for table, names in {
        "story_plan_arcs": ["end_state", "start_state", "core_question"],
        "story_plan_volumes": ["foreshadowing", "ending_turn", "main_thread", "end_state", "start_state", "major_conflict", "core_question", "theme"],
    }.items():
        for name in names:
            op.drop_column(table, name)
