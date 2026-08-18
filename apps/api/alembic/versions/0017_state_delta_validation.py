"""state delta validation"""
from alembic import op
import sqlalchemy as sa


revision = "0017_state_delta_validation"
down_revision = "0016_state_delta_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("state_delta_batches", sa.Column("validation_version", sa.String(40)))
    op.add_column("state_delta_batches", sa.Column("validation_report", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("state_delta_batches", sa.Column("validation_fingerprint", sa.String(120)))
    op.add_column("state_delta_batches", sa.Column("validated_world_fingerprint", sa.String(120)))
    op.add_column("state_delta_batches", sa.Column("validation_completed_at", sa.DateTime()))


def downgrade():
    op.drop_column("state_delta_batches", "validation_completed_at")
    op.drop_column("state_delta_batches", "validated_world_fingerprint")
    op.drop_column("state_delta_batches", "validation_fingerprint")
    op.drop_column("state_delta_batches", "validation_report")
    op.drop_column("state_delta_batches", "validation_version")
