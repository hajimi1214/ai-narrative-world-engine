"""model gateway controls for unified routing and resilient requests

Revision ID: 0034_model_gateway_controls
Revises: 0033_story_planning
"""
from alembic import op
import sqlalchemy as sa

revision = "0034_model_gateway_controls"
down_revision = "0033_story_planning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project_model_configs", sa.Column("single_model_mode", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("project_model_configs", sa.Column("shared_model", sa.String(length=200), nullable=True))
    op.add_column("project_model_configs", sa.Column("request_timeout_seconds", sa.Float(), nullable=False, server_default="120"))
    op.add_column("project_model_configs", sa.Column("max_retries", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("project_model_configs", sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    for name in ("rate_limit_per_minute", "max_retries", "request_timeout_seconds", "shared_model", "single_model_mode"):
        op.drop_column("project_model_configs", name)
