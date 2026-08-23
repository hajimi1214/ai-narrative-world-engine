"""history-derived chapter, narrative arc, and volume formation"""
from alembic import op
import sqlalchemy as sa

revision = "0022_narrative_structure_formation"
down_revision = "0021_autonomous_world_loop"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "narrative_structure_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("protocol_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_history_fingerprint", sa.String(120), nullable=False),
        sa.Column("source_max_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("config_fingerprint", sa.String(120), nullable=False),
        sa.Column("rebuild_from_sequence", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("structure_fingerprint", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime()),
    )
    op.create_index("ix_narrative_structure_revisions_project_id", "narrative_structure_revisions", ["project_id"])
    op.create_index("uq_narrative_structure_revision_project_active", "narrative_structure_revisions", ["project_id"], unique=True, postgresql_where=sa.text("active = true"), sqlite_where=sa.text("active = 1"))

    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.add_column(sa.Column("structure_revision_id", sa.String(36), sa.ForeignKey("narrative_structure_revisions.id", name="fk_chapters_structure_revision_id")))
        batch_op.add_column(sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("structure_status", sa.String(20), nullable=False, server_default="LEGACY"))
        batch_op.add_column(sa.Column("start_sequence", sa.Integer()))
        batch_op.add_column(sa.Column("end_sequence", sa.Integer()))
        batch_op.add_column(sa.Column("structure_fingerprint", sa.String(120)))
        batch_op.add_column(sa.Column("boundary_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        batch_op.add_column(sa.Column("supersedes_chapter_id", sa.String(36), sa.ForeignKey("chapters.id", name="fk_chapters_supersedes_chapter_id")))
    op.create_index("uq_chapter_project_active_number", "chapters", ["project_id", "number"], unique=True, postgresql_where=sa.text("active = true"), sqlite_where=sa.text("active = 1"))

    op.create_table(
        "chapter_scene_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("chapter_id", sa.String(36), sa.ForeignKey("chapters.id"), nullable=False),
        sa.Column("scene_id", sa.String(36), sa.ForeignKey("scenes.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("scene_sequence", sa.Integer(), nullable=False),
        sa.UniqueConstraint("chapter_id", "ordinal", name="uq_chapter_scene_binding_ordinal"),
        sa.UniqueConstraint("chapter_id", "scene_id", name="uq_chapter_scene_binding_scene"),
    )
    op.create_index("ix_chapter_scene_bindings_chapter_id", "chapter_scene_bindings", ["chapter_id"])
    op.create_index("ix_chapter_scene_bindings_scene_id", "chapter_scene_bindings", ["scene_id"])

    op.create_table(
        "narrative_arcs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("structure_revision_id", sa.String(36), sa.ForeignKey("narrative_structure_revisions.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False), sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(20), nullable=False), sa.Column("start_sequence", sa.Integer(), nullable=False), sa.Column("end_sequence", sa.Integer(), nullable=False),
        sa.Column("dominant_thread_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")), sa.Column("supporting_thread_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("structure_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("structure_fingerprint", sa.String(120), nullable=False),
        sa.Column("supersedes_arc_id", sa.String(36), sa.ForeignKey("narrative_arcs.id")), sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_narrative_arcs_project_id", "narrative_arcs", ["project_id"])
    op.create_index("ix_narrative_arcs_structure_revision_id", "narrative_arcs", ["structure_revision_id"])
    op.create_index("uq_narrative_arc_project_active_number", "narrative_arcs", ["project_id", "number"], unique=True, postgresql_where=sa.text("active = true"), sqlite_where=sa.text("active = 1"))
    op.create_table(
        "narrative_arc_chapter_bindings",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("narrative_arc_id", sa.String(36), sa.ForeignKey("narrative_arcs.id"), nullable=False),
        sa.Column("chapter_id", sa.String(36), sa.ForeignKey("chapters.id"), nullable=False), sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.UniqueConstraint("narrative_arc_id", "ordinal", name="uq_narrative_arc_chapter_ordinal"), sa.UniqueConstraint("narrative_arc_id", "chapter_id", name="uq_narrative_arc_chapter_chapter"),
    )
    op.create_index("ix_narrative_arc_chapter_bindings_narrative_arc_id", "narrative_arc_chapter_bindings", ["narrative_arc_id"])
    op.create_index("ix_narrative_arc_chapter_bindings_chapter_id", "narrative_arc_chapter_bindings", ["chapter_id"])

    op.create_table(
        "narrative_volumes",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("structure_revision_id", sa.String(36), sa.ForeignKey("narrative_structure_revisions.id"), nullable=False), sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200)), sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("start_sequence", sa.Integer(), nullable=False), sa.Column("end_sequence", sa.Integer(), nullable=False),
        sa.Column("dominant_thread_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")), sa.Column("structure_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("structure_fingerprint", sa.String(120), nullable=False), sa.Column("supersedes_volume_id", sa.String(36), sa.ForeignKey("narrative_volumes.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_narrative_volumes_project_id", "narrative_volumes", ["project_id"])
    op.create_index("ix_narrative_volumes_structure_revision_id", "narrative_volumes", ["structure_revision_id"])
    op.create_index("uq_narrative_volume_project_active_number", "narrative_volumes", ["project_id", "number"], unique=True, postgresql_where=sa.text("active = true"), sqlite_where=sa.text("active = 1"))
    op.create_table(
        "narrative_volume_arc_bindings",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("volume_id", sa.String(36), sa.ForeignKey("narrative_volumes.id"), nullable=False),
        sa.Column("narrative_arc_id", sa.String(36), sa.ForeignKey("narrative_arcs.id"), nullable=False), sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.UniqueConstraint("volume_id", "ordinal", name="uq_narrative_volume_arc_ordinal"), sa.UniqueConstraint("volume_id", "narrative_arc_id", name="uq_narrative_volume_arc_arc"),
    )
    op.create_index("ix_narrative_volume_arc_bindings_volume_id", "narrative_volume_arc_bindings", ["volume_id"])
    op.create_index("ix_narrative_volume_arc_bindings_narrative_arc_id", "narrative_volume_arc_bindings", ["narrative_arc_id"])


def downgrade():
    op.drop_index("ix_narrative_volume_arc_bindings_narrative_arc_id", table_name="narrative_volume_arc_bindings")
    op.drop_index("ix_narrative_volume_arc_bindings_volume_id", table_name="narrative_volume_arc_bindings"); op.drop_table("narrative_volume_arc_bindings")
    op.drop_index("uq_narrative_volume_project_active_number", table_name="narrative_volumes"); op.drop_index("ix_narrative_volumes_structure_revision_id", table_name="narrative_volumes"); op.drop_index("ix_narrative_volumes_project_id", table_name="narrative_volumes"); op.drop_table("narrative_volumes")
    op.drop_index("ix_narrative_arc_chapter_bindings_chapter_id", table_name="narrative_arc_chapter_bindings"); op.drop_index("ix_narrative_arc_chapter_bindings_narrative_arc_id", table_name="narrative_arc_chapter_bindings"); op.drop_table("narrative_arc_chapter_bindings")
    op.drop_index("uq_narrative_arc_project_active_number", table_name="narrative_arcs"); op.drop_index("ix_narrative_arcs_structure_revision_id", table_name="narrative_arcs"); op.drop_index("ix_narrative_arcs_project_id", table_name="narrative_arcs"); op.drop_table("narrative_arcs")
    op.drop_index("ix_chapter_scene_bindings_scene_id", table_name="chapter_scene_bindings"); op.drop_index("ix_chapter_scene_bindings_chapter_id", table_name="chapter_scene_bindings"); op.drop_table("chapter_scene_bindings")
    op.drop_index("uq_chapter_project_active_number", table_name="chapters")
    for name in ("supersedes_chapter_id", "boundary_metadata", "structure_fingerprint", "end_sequence", "start_sequence", "structure_status", "active", "structure_revision_id"):
        op.drop_column("chapters", name)
    op.drop_index("uq_narrative_structure_revision_project_active", table_name="narrative_structure_revisions"); op.drop_index("ix_narrative_structure_revisions_project_id", table_name="narrative_structure_revisions"); op.drop_table("narrative_structure_revisions")
