"""versioned scene checkpoint protocol v3"""
from alembic import op
import sqlalchemy as sa


revision = "0019_scene_checkpoint_unification"
down_revision = "0018_scene_commit_engine"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scene_state_checkpoints") as batch:
        batch.drop_constraint("uq_scene_state_checkpoint", type_="unique")
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("origin", sa.String(30), nullable=False, server_default="LEGACY"))
        batch.add_column(sa.Column("source_scene_commit_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("source_replay_session_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("supersedes_checkpoint_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("pre_state_fingerprint", sa.String(120), nullable=True))
        batch.add_column(sa.Column("post_state_fingerprint", sa.String(120), nullable=True))
        batch.add_column(sa.Column("checkpoint_fingerprint", sa.String(120), nullable=True))
        batch.create_unique_constraint("uq_scene_state_checkpoint_version", ["project_id", "scene_id", "version"])
        batch.create_foreign_key("fk_scene_checkpoint_scene_commit", "scene_commits", ["source_scene_commit_id"], ["id"])
        batch.create_foreign_key("fk_scene_checkpoint_replay_session", "retcon_replay_sessions", ["source_replay_session_id"], ["id"])
        batch.create_foreign_key("fk_scene_checkpoint_supersedes", "scene_state_checkpoints", ["supersedes_checkpoint_id"], ["id"])
    op.create_index("uq_scene_state_checkpoint_active", "scene_state_checkpoints", ["project_id", "scene_id"], unique=True, postgresql_where=sa.text("active = true"), sqlite_where=sa.text("active = 1"))
    op.create_index("ix_scene_state_checkpoints_checkpoint_fingerprint", "scene_state_checkpoints", ["checkpoint_fingerprint"])


def downgrade():
    # 0018 can only express one checkpoint per (project, scene).  Preserve
    # the active/latest boundary and intentionally discard older index rows;
    # snapshots remain append-only audit records and are never cascaded here.
    op.execute("""
        DELETE FROM scene_state_checkpoints
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY project_id, scene_id
                    ORDER BY active DESC, version DESC, id DESC
                ) AS row_number
                FROM scene_state_checkpoints
            ) ranked
            WHERE ranked.row_number > 1
        )
    """)
    op.drop_index("ix_scene_state_checkpoints_checkpoint_fingerprint", table_name="scene_state_checkpoints")
    op.drop_index("uq_scene_state_checkpoint_active", table_name="scene_state_checkpoints")
    with op.batch_alter_table("scene_state_checkpoints") as batch:
        batch.drop_constraint("fk_scene_checkpoint_supersedes", type_="foreignkey")
        batch.drop_constraint("fk_scene_checkpoint_replay_session", type_="foreignkey")
        batch.drop_constraint("fk_scene_checkpoint_scene_commit", type_="foreignkey")
        batch.drop_constraint("uq_scene_state_checkpoint_version", type_="unique")
        batch.drop_column("checkpoint_fingerprint")
        batch.drop_column("post_state_fingerprint")
        batch.drop_column("pre_state_fingerprint")
        batch.drop_column("supersedes_checkpoint_id")
        batch.drop_column("source_replay_session_id")
        batch.drop_column("source_scene_commit_id")
        batch.drop_column("origin")
        batch.drop_column("active")
        batch.drop_column("version")
        batch.create_unique_constraint("uq_scene_state_checkpoint", ["project_id", "scene_id"])
