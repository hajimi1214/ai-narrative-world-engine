"""dimension-aware pgvector ANN accelerators

Revision ID: 0030_dimension_aware_memory_ann
Revises: 0029_long_horizon_retrieval_index
"""
from alembic import op
import sqlalchemy as sa

revision = "0030_dimension_aware_memory_ann"
down_revision = "0029_long_horizon_retrieval_index"
branch_labels = None
depends_on = None

VECTOR_DIMS = (384, 512, 768, 1024, 1536)
HALFVEC_DIMS = (3072,)


def upgrade() -> None:
    op.add_column("project_model_configs", sa.Column("memory_vector_search_mode", sa.String(10), nullable=False, server_default="EXACT"))
    op.add_column("project_model_configs", sa.Column("memory_ann_ef_search", sa.Integer(), nullable=False, server_default="200"))
    op.add_column("project_model_configs", sa.Column("memory_ann_candidate_multiplier", sa.Integer(), nullable=False, server_default="8"))
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    version = bind.execute(sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
    try:
        version_supported = tuple(int(part) for part in (version or "0").split(".")[:2]) >= (0, 8)
    except ValueError:
        version_supported = False
    if version_supported:
        for dimension in VECTOR_DIMS:
            name = f"ix_memory_embedding_hnsw_cosine_{dimension}"
            op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {name} ON character_memory_embeddings USING hnsw ((embedding::vector({dimension})) vector_cosine_ops) WHERE status = 'READY' AND dimension = {dimension}"))
        for dimension in HALFVEC_DIMS:
            name = f"ix_memory_embedding_hnsw_halfvec_cosine_{dimension}"
            op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {name} ON character_memory_embeddings USING hnsw ((embedding::halfvec({dimension})) halfvec_cosine_ops) WHERE status = 'READY' AND dimension = {dimension}"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for dimension in VECTOR_DIMS:
            op.execute(sa.text(f"DROP INDEX IF EXISTS ix_memory_embedding_hnsw_cosine_{dimension}"))
        for dimension in HALFVEC_DIMS:
            op.execute(sa.text(f"DROP INDEX IF EXISTS ix_memory_embedding_hnsw_halfvec_cosine_{dimension}"))
    op.drop_column("project_model_configs", "memory_ann_candidate_multiplier")
    op.drop_column("project_model_configs", "memory_ann_ef_search")
    op.drop_column("project_model_configs", "memory_vector_search_mode")
