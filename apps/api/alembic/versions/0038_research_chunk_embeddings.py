"""persist semantic vectors for research chunks

Revision ID: 0038_research_chunk_embeddings
Revises: 0037_scene_beat_execution
"""
from alembic import op
import sqlalchemy as sa


revision = "0038_research_chunk_embeddings"
down_revision = "0037_scene_beat_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        from pgvector.sqlalchemy import Vector
        vector_type = Vector()
    else:
        vector_type = sa.JSON()
    op.create_table(
        "research_chunk_embeddings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_id", sa.String(length=36), nullable=False),
        sa.Column("embedding_config_fingerprint", sa.String(length=120), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("embedding", vector_type, nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(length=120), nullable=True),
        sa.Column("request_id", sa.String(length=200), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["research_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["revision_id"], ["research_document_revisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chunk_id"], ["research_chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chunk_id", "embedding_config_fingerprint", name="uq_research_chunk_embedding_config"),
    )
    op.create_index("ix_research_chunk_embeddings_project_id", "research_chunk_embeddings", ["project_id"])
    op.create_index("ix_research_chunk_embeddings_document_id", "research_chunk_embeddings", ["document_id"])
    op.create_index("ix_research_chunk_embeddings_revision_id", "research_chunk_embeddings", ["revision_id"])
    op.create_index("ix_research_chunk_embeddings_chunk_id", "research_chunk_embeddings", ["chunk_id"])
    op.create_index("ix_research_chunk_embedding_project_config_status", "research_chunk_embeddings", ["project_id", "embedding_config_fingerprint", "status"])


def downgrade() -> None:
    op.drop_index("ix_research_chunk_embedding_project_config_status", table_name="research_chunk_embeddings")
    op.drop_index("ix_research_chunk_embeddings_chunk_id", table_name="research_chunk_embeddings")
    op.drop_index("ix_research_chunk_embeddings_revision_id", table_name="research_chunk_embeddings")
    op.drop_index("ix_research_chunk_embeddings_document_id", table_name="research_chunk_embeddings")
    op.drop_index("ix_research_chunk_embeddings_project_id", table_name="research_chunk_embeddings")
    op.drop_table("research_chunk_embeddings")
