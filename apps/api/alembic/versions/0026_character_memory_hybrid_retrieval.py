"""character memory hybrid retrieval and encrypted provider credentials"""
from alembic import op
import sqlalchemy as sa


revision = "0026_character_memory_hybrid_retrieval"
down_revision = "0025_knowledge_brain_research"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        from pgvector.sqlalchemy import Vector
        vector_type = Vector()
    else:
        vector_type = sa.JSON()
    op.add_column("project_model_configs", sa.Column("embedding_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("project_model_configs", sa.Column("embedding_use_main_connection", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("project_model_configs", sa.Column("embedding_provider", sa.String(100)))
    op.add_column("project_model_configs", sa.Column("embedding_base_url", sa.String(500)))
    op.add_column("project_model_configs", sa.Column("embedding_model", sa.String(200)))
    op.add_column("project_model_configs", sa.Column("embedding_dimension", sa.Integer()))
    op.add_column("project_model_configs", sa.Column("memory_retrieval_mode", sa.String(30), nullable=False, server_default="DETERMINISTIC"))
    op.add_column("project_model_configs", sa.Column("memory_vector_top_k", sa.Integer(), nullable=False, server_default="12"))
    op.add_column("project_model_configs", sa.Column("memory_rrf_k", sa.Integer(), nullable=False, server_default="60"))
    op.add_column("project_model_configs", sa.Column("memory_semantic_min_similarity", sa.Float()))
    op.create_table(
        "project_provider_credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("secret_fingerprint", sa.String(120), nullable=False),
        sa.Column("secret_hint", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", "purpose", name="uq_project_provider_credential_purpose"),
    )
    op.create_index("ix_project_provider_credentials_project_id", "project_provider_credentials", ["project_id"])
    op.create_table(
        "character_memory_embeddings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("character_id", sa.String(36), sa.ForeignKey("characters.id"), nullable=False),
        sa.Column("memory_id", sa.String(36), sa.ForeignKey("character_memories.id"), nullable=False),
        sa.Column("embedding_config_fingerprint", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("content_fingerprint", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("embedding", vector_type),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(120)),
        sa.Column("request_id", sa.String(200)),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("indexed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("memory_id", "embedding_config_fingerprint", name="uq_memory_embedding_config"),
    )
    op.create_index("ix_character_memory_embeddings_project_id", "character_memory_embeddings", ["project_id"])
    op.create_index("ix_character_memory_embeddings_character_id", "character_memory_embeddings", ["character_id"])
    op.create_index("ix_character_memory_embeddings_memory_id", "character_memory_embeddings", ["memory_id"])
    op.create_index("ix_memory_embedding_project_character_config_status", "character_memory_embeddings", ["project_id", "character_id", "embedding_config_fingerprint", "status"])


def downgrade():
    op.drop_index("ix_memory_embedding_project_character_config_status", table_name="character_memory_embeddings")
    op.drop_index("ix_character_memory_embeddings_memory_id", table_name="character_memory_embeddings")
    op.drop_index("ix_character_memory_embeddings_character_id", table_name="character_memory_embeddings")
    op.drop_index("ix_character_memory_embeddings_project_id", table_name="character_memory_embeddings")
    op.drop_table("character_memory_embeddings")
    op.drop_index("ix_project_provider_credentials_project_id", table_name="project_provider_credentials")
    op.drop_table("project_provider_credentials")
    for column in ("memory_semantic_min_similarity", "memory_rrf_k", "memory_vector_top_k", "memory_retrieval_mode", "embedding_dimension", "embedding_model", "embedding_base_url", "embedding_provider", "embedding_use_main_connection", "embedding_enabled"):
        op.drop_column("project_model_configs", column)
