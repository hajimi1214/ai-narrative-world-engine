"""allow immutable continuity snapshot versions per volume

Revision ID: 0043_versioned_volume_snapshots
Revises: 0042_volume_snapshot_uniqueness
"""
from alembic import op
from alembic.operations import Operations
import sqlalchemy as sa


revision = "0043_versioned_volume_snapshots"
down_revision = "0042_volume_snapshot_uniqueness"
branch_labels = None
depends_on = None


OLD_INDEX = "uq_volume_continuity_snapshot_volume"
NEW_INDEX = "uq_volume_continuity_snapshot_volume_version"
TABLE = "volume_continuity_snapshots"


def upgrade_snapshot_history(operations: Operations) -> None:
    bind = operations.get_bind()
    inspector = sa.inspect(bind)
    for index in inspector.get_indexes(TABLE):
        if index["name"] == OLD_INDEX and not index.get("duplicates_constraint"):
            operations.drop_index(OLD_INDEX, table_name=TABLE)

    # Older ORM-created databases use an inline UNIQUE constraint. SQLite
    # requires a table rebuild to remove that constraint.
    constraints = [
        constraint["name"] or f"uq_{TABLE}_volume_id"
        for constraint in inspector.get_unique_constraints(TABLE)
        if constraint["column_names"] == ["volume_id"]
    ]
    if constraints:
        convention = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
        with operations.batch_alter_table(TABLE, naming_convention=convention) as batch:
            for constraint in constraints:
                batch.drop_constraint(constraint, type_="unique")

    indexes = {item["name"] for item in sa.inspect(bind).get_indexes(TABLE)}
    if NEW_INDEX not in indexes:
        operations.create_index(
            NEW_INDEX,
            TABLE,
            ["volume_id", "snapshot_version"],
            unique=True,
        )


def upgrade() -> None:
    upgrade_snapshot_history(Operations(op.get_context()))


def downgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(sa.text(
        "SELECT volume_id FROM volume_continuity_snapshots "
        "GROUP BY volume_id HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicate:
        raise RuntimeError("Cannot downgrade: immutable volume snapshot history contains multiple versions")
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("volume_continuity_snapshots")}
    if NEW_INDEX in indexes:
        op.drop_index(NEW_INDEX, table_name="volume_continuity_snapshots")
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("volume_continuity_snapshots")}
    if OLD_INDEX not in indexes:
        op.create_index(
            OLD_INDEX,
            "volume_continuity_snapshots",
            ["volume_id"],
            unique=True,
        )
