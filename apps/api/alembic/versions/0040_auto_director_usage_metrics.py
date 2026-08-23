"""add explicit auto director usage metrics"""
from alembic import op
import sqlalchemy as sa

revision = "0040_auto_director_usage_metrics"
down_revision = "0039_auto_director"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("auto_director_runs") as batch:
        batch.add_column(sa.Column("total_calls", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("estimated_cost", sa.Float(), nullable=True))
        batch.add_column(sa.Column("cost_status", sa.String(20), nullable=False, server_default="UNKNOWN"))
    with op.batch_alter_table("auto_director_steps") as batch:
        batch.add_column(sa.Column("calls", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("provider", sa.String(120), nullable=True))
        batch.add_column(sa.Column("model", sa.String(200), nullable=True))
        batch.add_column(sa.Column("estimated_cost", sa.Float(), nullable=True))
        batch.add_column(sa.Column("cost_status", sa.String(20), nullable=False, server_default="UNKNOWN"))


def downgrade() -> None:
    with op.batch_alter_table("auto_director_steps") as batch:
        for name in ("cost_status", "estimated_cost", "model", "provider", "latency_ms", "total_tokens", "completion_tokens", "prompt_tokens", "calls"):
            batch.drop_column(name)
    with op.batch_alter_table("auto_director_runs") as batch:
        for name in ("cost_status", "estimated_cost", "latency_ms", "total_tokens", "completion_tokens", "prompt_tokens", "total_calls"):
            batch.drop_column(name)
