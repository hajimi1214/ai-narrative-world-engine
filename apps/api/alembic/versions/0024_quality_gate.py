"""grounded prose quality assessment and repair provenance"""
from alembic import op
import sqlalchemy as sa

revision = "0024_quality_gate"
down_revision = "0023_writer_projection"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chapter_quality_assessments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("chapter_id", sa.String(36), sa.ForeignKey("chapters.id"), nullable=False),
        sa.Column("writer_draft_id", sa.String(36), sa.ForeignKey("chapter_writer_drafts.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("client_request_id", sa.String(200)),
        sa.Column("request_fingerprint", sa.String(120), nullable=False),
        sa.Column("content_fingerprint", sa.String(120), nullable=False),
        sa.Column("writer_context_fingerprint", sa.String(120), nullable=False),
        sa.Column("chapter_source_fingerprint", sa.String(120), nullable=False),
        sa.Column("anti_ai_bible_id", sa.String(36), sa.ForeignKey("anti_ai_bibles.id")),
        sa.Column("anti_ai_bible_version", sa.Integer()),
        sa.Column("anti_ai_bible_fingerprint", sa.String(120), nullable=False),
        sa.Column("writing_bible_fingerprint", sa.String(120), nullable=False),
        sa.Column("quality_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("quality_config_fingerprint", sa.String(120), nullable=False),
        sa.Column("quality_context_fingerprint", sa.String(120), nullable=False),
        sa.Column("deterministic_report", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("critic_report", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("overall_score", sa.Float()),
        sa.Column("decision_reason_codes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("critic_provider", sa.String(100)),
        sa.Column("critic_model", sa.String(200)),
        sa.Column("critic_request_id", sa.String(200)),
        sa.Column("critic_prompt_fingerprint", sa.String(120)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("stale_at", sa.DateTime()),
        sa.Column("approved_at", sa.DateTime()),
        sa.UniqueConstraint("chapter_id", "version", name="uq_chapter_quality_assessment_version"),
        sa.UniqueConstraint("chapter_id", "client_request_id", name="uq_chapter_quality_assessment_request"),
    )
    op.create_index("ix_chapter_quality_assessments_project_id", "chapter_quality_assessments", ["project_id"])
    op.create_index("ix_chapter_quality_assessments_chapter_id", "chapter_quality_assessments", ["chapter_id"])
    op.create_index("ix_chapter_quality_assessments_writer_draft_id", "chapter_quality_assessments", ["writer_draft_id"])
    op.create_index("ix_chapter_quality_assessments_status", "chapter_quality_assessments", ["status"])
    op.create_index("ix_chapter_quality_assessments_request_fingerprint", "chapter_quality_assessments", ["request_fingerprint"])
    op.create_index("ix_chapter_quality_assessments_quality_context_fingerprint", "chapter_quality_assessments", ["quality_context_fingerprint"])
    op.create_index("uq_chapter_quality_assessment_active", "chapter_quality_assessments", ["chapter_id"], unique=True, postgresql_where=sa.text("active = true"), sqlite_where=sa.text("active = 1"))
    op.create_table(
        "chapter_quality_findings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("assessment_id", sa.String(36), sa.ForeignKey("chapter_quality_assessments.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("rule_code", sa.String(100), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer()),
        sa.Column("end_offset", sa.Integer()),
        sa.Column("excerpt", sa.Text()),
        sa.Column("source_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("finding_fingerprint", sa.String(120), nullable=False),
        sa.UniqueConstraint("assessment_id", "ordinal", name="uq_chapter_quality_finding_ordinal"),
    )
    op.create_index("ix_chapter_quality_findings_assessment_id", "chapter_quality_findings", ["assessment_id"])
    op.create_index("ix_chapter_quality_findings_finding_fingerprint", "chapter_quality_findings", ["finding_fingerprint"])
    op.add_column("chapter_writer_drafts", sa.Column("origin", sa.String(30), nullable=False, server_default="WRITER"))
    op.add_column("chapter_writer_drafts", sa.Column("source_quality_assessment_id", sa.String(36), sa.ForeignKey("chapter_quality_assessments.id")))
    op.add_column("chapters", sa.Column("current_quality_assessment_id", sa.String(36), sa.ForeignKey("chapter_quality_assessments.id")))
    op.add_column("chapters", sa.Column("quality_status", sa.String(30)))
    op.add_column("chapters", sa.Column("quality_content_fingerprint", sa.String(120)))
    op.add_column("chapters", sa.Column("quality_approved_at", sa.DateTime()))


def downgrade():
    op.drop_column("chapters", "quality_approved_at")
    op.drop_column("chapters", "quality_content_fingerprint")
    op.drop_column("chapters", "quality_status")
    op.drop_column("chapters", "current_quality_assessment_id")
    op.drop_column("chapter_writer_drafts", "source_quality_assessment_id")
    op.drop_column("chapter_writer_drafts", "origin")
    op.drop_index("ix_chapter_quality_findings_finding_fingerprint", table_name="chapter_quality_findings")
    op.drop_index("ix_chapter_quality_findings_assessment_id", table_name="chapter_quality_findings")
    op.drop_table("chapter_quality_findings")
    op.drop_index("uq_chapter_quality_assessment_active", table_name="chapter_quality_assessments")
    op.drop_index("ix_chapter_quality_assessments_quality_context_fingerprint", table_name="chapter_quality_assessments")
    op.drop_index("ix_chapter_quality_assessments_request_fingerprint", table_name="chapter_quality_assessments")
    op.drop_index("ix_chapter_quality_assessments_status", table_name="chapter_quality_assessments")
    op.drop_index("ix_chapter_quality_assessments_writer_draft_id", table_name="chapter_quality_assessments")
    op.drop_index("ix_chapter_quality_assessments_chapter_id", table_name="chapter_quality_assessments")
    op.drop_index("ix_chapter_quality_assessments_project_id", table_name="chapter_quality_assessments")
    op.drop_table("chapter_quality_assessments")
