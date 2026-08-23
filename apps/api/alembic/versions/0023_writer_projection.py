"""formal writer drafts and chapter adoption pointer"""
from alembic import op
import sqlalchemy as sa

revision = "0023_writer_projection"
down_revision = "0022_narrative_structure_formation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chapter_writer_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("chapter_id", sa.String(36), sa.ForeignKey("chapters.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="GENERATING"),
        sa.Column("client_request_id", sa.String(200)),
        sa.Column("request_fingerprint", sa.String(120), nullable=False),
        sa.Column("chapter_structure_fingerprint", sa.String(120), nullable=False),
        sa.Column("chapter_source_fingerprint", sa.String(120), nullable=False),
        sa.Column("writer_context_fingerprint", sa.String(120), nullable=False),
        sa.Column("source_structure_status", sa.String(30), nullable=False),
        sa.Column("source_scene_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("source_manifest", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("writing_bible_id", sa.String(36), sa.ForeignKey("writing_bibles.id")),
        sa.Column("writing_bible_version", sa.Integer()),
        sa.Column("writing_bible_fingerprint", sa.String(120), nullable=False),
        sa.Column("pov_mode", sa.String(30), nullable=False),
        sa.Column("pov_character_id", sa.String(36), sa.ForeignKey("characters.id")),
        sa.Column("provider", sa.String(100)),
        sa.Column("model", sa.String(200)),
        sa.Column("model_request_id", sa.String(200)),
        sa.Column("prompt_fingerprint", sa.String(120)),
        sa.Column("title_candidate", sa.String(300)),
        sa.Column("content", sa.Text()),
        sa.Column("content_fingerprint", sa.String(120)),
        sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scene_coverage", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("source_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("validation_report", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("parent_draft_id", sa.String(36), sa.ForeignKey("chapter_writer_drafts.id")),
        sa.Column("supersedes_draft_id", sa.String(36), sa.ForeignKey("chapter_writer_drafts.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("adopted_at", sa.DateTime()),
        sa.Column("stale_at", sa.DateTime()),
        sa.UniqueConstraint("chapter_id", "version", name="uq_chapter_writer_draft_version"),
        sa.UniqueConstraint("chapter_id", "client_request_id", name="uq_chapter_writer_draft_request"),
    )
    op.create_index("ix_chapter_writer_drafts_project_id", "chapter_writer_drafts", ["project_id"])
    op.create_index("ix_chapter_writer_drafts_chapter_id", "chapter_writer_drafts", ["chapter_id"])
    op.create_index("ix_chapter_writer_drafts_status", "chapter_writer_drafts", ["status"])
    op.create_index("ix_chapter_writer_drafts_request_fingerprint", "chapter_writer_drafts", ["request_fingerprint"])
    op.create_index("ix_chapter_writer_drafts_content_fingerprint", "chapter_writer_drafts", ["content_fingerprint"])
    op.create_index("ix_chapter_writer_drafts_chapter_status", "chapter_writer_drafts", ["chapter_id", "status"])
    with op.batch_alter_table("chapters") as batch:
        batch.add_column(sa.Column("current_writer_draft_id", sa.String(36), sa.ForeignKey("chapter_writer_drafts.id", name="fk_chapters_current_writer_draft_id")))
        batch.add_column(sa.Column("writer_content_fingerprint", sa.String(120)))
        batch.add_column(sa.Column("writer_context_fingerprint", sa.String(120)))
        batch.add_column(sa.Column("written_at", sa.DateTime()))


def downgrade():
    op.drop_column("chapters", "written_at")
    op.drop_column("chapters", "writer_context_fingerprint")
    op.drop_column("chapters", "writer_content_fingerprint")
    op.drop_column("chapters", "current_writer_draft_id")
    op.drop_index("ix_chapter_writer_drafts_chapter_status", table_name="chapter_writer_drafts")
    op.drop_index("ix_chapter_writer_drafts_content_fingerprint", table_name="chapter_writer_drafts")
    op.drop_index("ix_chapter_writer_drafts_request_fingerprint", table_name="chapter_writer_drafts")
    op.drop_index("ix_chapter_writer_drafts_status", table_name="chapter_writer_drafts")
    op.drop_index("ix_chapter_writer_drafts_chapter_id", table_name="chapter_writer_drafts")
    op.drop_index("ix_chapter_writer_drafts_project_id", table_name="chapter_writer_drafts")
    op.drop_table("chapter_writer_drafts")
