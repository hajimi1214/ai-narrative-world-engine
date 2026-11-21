"""persist automatic writing claims, retry timing, and saved checkpoints"""

from alembic import op
import sqlalchemy as sa


revision = "0044_auto_director_recovery"
down_revision = "0043_versioned_volume_snapshots"
branch_labels = None
depends_on = None


def _columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("worker_claim_token", sa.String(36)),
        sa.Column("worker_lease_until", sa.DateTime()),
        sa.Column("next_retry_at", sa.DateTime()),
        sa.Column("recovery_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recovery_key", sa.String(180)),
        sa.Column("recovery_error_code", sa.String(100)),
        sa.Column("recovery_usage_uncertain", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("checkpoint_stage", sa.String(30)),
        sa.Column("checkpoint_saved_at", sa.DateTime()),
    )


def upgrade() -> None:
    for column in _columns():
        op.add_column("auto_director_runs", column)


def downgrade() -> None:
    with op.batch_alter_table("auto_director_runs") as batch:
        for column in reversed(_columns()):
            batch.drop_column(column.name)
