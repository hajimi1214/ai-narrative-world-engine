"""replay runtime integrity"""
from alembic import op
import sqlalchemy as sa

revision = "0015_replay_runtime_integrity"
down_revision = "0014_replay_execution_integrity"
branch_labels = None
depends_on = None

def upgrade():
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("scene_execution_bindings") as batch:
            batch.drop_constraint("uq_scene_execution_binding_active", type_="unique")
    else:
        op.drop_constraint("uq_scene_execution_binding_active", "scene_execution_bindings", type_="unique")
    op.create_index("uq_scene_active_execution_binding", "scene_execution_bindings", ["scene_id"], unique=True, postgresql_where=sa.text("active = true"), sqlite_where=sa.text("active = 1"))
    op.add_column("retcon_cognition_invalidations", sa.Column("resolution_report", sa.JSON(), nullable=True))

def downgrade():
    op.drop_column("retcon_cognition_invalidations", "resolution_report")
    op.drop_index("uq_scene_active_execution_binding", table_name="scene_execution_bindings")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("scene_execution_bindings") as batch:
            batch.create_unique_constraint("uq_scene_execution_binding_active", ["scene_id", "active"])
    else:
        op.create_unique_constraint("uq_scene_execution_binding_active", "scene_execution_bindings", ["scene_id", "active"])
