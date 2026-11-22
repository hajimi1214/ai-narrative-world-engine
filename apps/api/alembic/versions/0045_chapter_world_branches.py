"""Persist isolated world candidates for automatic chapter writing."""

from alembic import op
import sqlalchemy as sa


revision = "0045_chapter_world_branches"
down_revision = "0044_auto_director_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("narrative_structure_scene_features", sa.Column("chapter_boundary_id", sa.String(length=36), nullable=True))
    op.create_table(
        "chapter_world_branches",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("auto_director_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chapter_id", sa.String(length=36), nullable=True),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="OPEN"),
        sa.Column("base_world_fingerprint", sa.String(length=120), nullable=False),
        sa.Column("base_history_fingerprint", sa.String(length=120), nullable=False),
        sa.Column("current_fingerprint", sa.String(length=120), nullable=False),
        sa.Column("chapter_structure_fingerprint", sa.String(length=120), nullable=True),
        sa.Column("state_overrides", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("staged_cognition", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("state_changes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("scene_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("performance_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("source_fingerprint", sa.String(length=120), nullable=True),
        sa.Column("writer_source_manifest", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("adopted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "chapter_number", name="uq_chapter_world_branch_run_chapter"),
    )
    op.create_index("ix_chapter_world_branches_project_id", "chapter_world_branches", ["project_id"])
    op.create_index("ix_chapter_world_branches_run_id", "chapter_world_branches", ["run_id"])
    op.create_index("ix_chapter_world_branches_chapter_id", "chapter_world_branches", ["chapter_id"])
    op.create_index("ix_chapter_world_branches_status", "chapter_world_branches", ["status"])
    op.create_index("ix_chapter_world_branch_project_status", "chapter_world_branches", ["project_id", "status"])

    with op.batch_alter_table("scenes") as batch:
        batch.add_column(sa.Column("world_branch_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key("fk_scenes_world_branch_id", "chapter_world_branches", ["world_branch_id"], ["id"])
    op.create_index("ix_scenes_world_branch_id", "scenes", ["world_branch_id"])

    with op.batch_alter_table("chapters") as batch:
        batch.add_column(sa.Column("world_branch_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("pending_world_branch_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("source_world_branch_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key("fk_chapters_world_branch_id", "chapter_world_branches", ["world_branch_id"], ["id"])
        batch.create_foreign_key("fk_chapters_pending_world_branch_id", "chapter_world_branches", ["pending_world_branch_id"], ["id"])
        batch.create_foreign_key("fk_chapters_source_world_branch_id", "chapter_world_branches", ["source_world_branch_id"], ["id"])
    op.create_index("ix_chapters_world_branch_id", "chapters", ["world_branch_id"])
    op.create_index("ix_chapters_pending_world_branch_id", "chapters", ["pending_world_branch_id"])
    op.create_index("ix_chapters_source_world_branch_id", "chapters", ["source_world_branch_id"])

def downgrade() -> None:
    op.drop_index("ix_chapters_world_branch_id", table_name="chapters")
    op.drop_index("ix_chapters_source_world_branch_id", table_name="chapters")
    op.drop_index("ix_chapters_pending_world_branch_id", table_name="chapters")
    with op.batch_alter_table("chapters") as batch:
        batch.drop_constraint("fk_chapters_world_branch_id", type_="foreignkey")
        batch.drop_constraint("fk_chapters_pending_world_branch_id", type_="foreignkey")
        batch.drop_constraint("fk_chapters_source_world_branch_id", type_="foreignkey")
        batch.drop_column("world_branch_id")
        batch.drop_column("pending_world_branch_id")
        batch.drop_column("source_world_branch_id")

    op.drop_index("ix_scenes_world_branch_id", table_name="scenes")
    with op.batch_alter_table("scenes") as batch:
        batch.drop_constraint("fk_scenes_world_branch_id", type_="foreignkey")
        batch.drop_column("world_branch_id")

    op.drop_index("ix_chapter_world_branch_project_status", table_name="chapter_world_branches")
    op.drop_index("ix_chapter_world_branches_status", table_name="chapter_world_branches")
    op.drop_index("ix_chapter_world_branches_chapter_id", table_name="chapter_world_branches")
    op.drop_index("ix_chapter_world_branches_run_id", table_name="chapter_world_branches")
    op.drop_index("ix_chapter_world_branches_project_id", table_name="chapter_world_branches")
    op.drop_table("chapter_world_branches")

    op.drop_column("narrative_structure_scene_features", "chapter_boundary_id")
