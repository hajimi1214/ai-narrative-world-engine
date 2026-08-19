"""project research library with immutable revisions and chunks"""
from alembic import op
import sqlalchemy as sa

revision = "0025_knowledge_brain_research"
down_revision = "0024_quality_gate"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "research_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("source_tier", sa.String(30), nullable=False),
        sa.Column("source_kind", sa.String(30), nullable=False),
        sa.Column("source_uri", sa.String(2048)),
        sa.Column("source_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("client_request_id", sa.String(200)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("archived_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", "client_request_id", name="uq_research_document_request"),
    )
    op.create_index("ix_research_documents_project_id", "research_documents", ["project_id"])
    op.create_index("ix_research_documents_project_active", "research_documents", ["project_id", "active"])
    op.create_index("ix_research_documents_source_tier", "research_documents", ["source_tier"])
    op.create_index("ix_research_documents_source_kind", "research_documents", ["source_kind"])

    op.create_table(
        "research_document_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("research_documents.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(120), nullable=False),
        sa.Column("normalized_fingerprint", sa.String(120), nullable=False),
        sa.Column("ingestion_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("ingestion_config_fingerprint", sa.String(120), nullable=False),
        sa.Column("supersedes_revision_id", sa.String(36), sa.ForeignKey("research_document_revisions.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("document_id", "version", name="uq_research_revision_version"),
    )
    op.create_index("ix_research_document_revisions_project_id", "research_document_revisions", ["project_id"])
    op.create_index("ix_research_document_revisions_document_id", "research_document_revisions", ["document_id"])
    op.create_index("ix_research_document_revisions_content_fingerprint", "research_document_revisions", ["content_fingerprint"])
    op.create_index("uq_research_revision_active", "research_document_revisions", ["document_id"], unique=True, postgresql_where=sa.text("active = true"), sqlite_where=sa.text("active = 1"))

    op.create_table(
        "research_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("research_documents.id"), nullable=False),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("research_document_revisions.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(120), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("revision_id", "ordinal", name="uq_research_chunk_revision_ordinal"),
    )
    op.create_index("ix_research_chunks_project_id", "research_chunks", ["project_id"])
    op.create_index("ix_research_chunks_document_id", "research_chunks", ["document_id"])
    op.create_index("ix_research_chunks_revision_id", "research_chunks", ["revision_id"])
    op.create_index("ix_research_chunks_project_active", "research_chunks", ["project_id", "active"])
    op.create_index("ix_research_chunks_content_fingerprint", "research_chunks", ["content_fingerprint"])


def downgrade():
    op.drop_index("ix_research_chunks_content_fingerprint", table_name="research_chunks")
    op.drop_index("ix_research_chunks_project_active", table_name="research_chunks")
    op.drop_index("ix_research_chunks_revision_id", table_name="research_chunks")
    op.drop_index("ix_research_chunks_document_id", table_name="research_chunks")
    op.drop_index("ix_research_chunks_project_id", table_name="research_chunks")
    op.drop_table("research_chunks")
    op.drop_index("uq_research_revision_active", table_name="research_document_revisions")
    op.drop_index("ix_research_document_revisions_content_fingerprint", table_name="research_document_revisions")
    op.drop_index("ix_research_document_revisions_document_id", table_name="research_document_revisions")
    op.drop_index("ix_research_document_revisions_project_id", table_name="research_document_revisions")
    op.drop_table("research_document_revisions")
    op.drop_index("ix_research_documents_source_kind", table_name="research_documents")
    op.drop_index("ix_research_documents_source_tier", table_name="research_documents")
    op.drop_index("ix_research_documents_project_active", table_name="research_documents")
    op.drop_index("ix_research_documents_project_id", table_name="research_documents")
    op.drop_table("research_documents")
