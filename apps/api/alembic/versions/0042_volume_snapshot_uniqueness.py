"""enforce one immutable continuity snapshot per volume"""
from alembic import op


revision = "0042_volume_snapshot_uniqueness"
down_revision = "0041_author_guided_volume"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_volume_continuity_snapshot_volume",
        "volume_continuity_snapshots",
        ["volume_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_volume_continuity_snapshot_volume", table_name="volume_continuity_snapshots")
