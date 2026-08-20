"""long horizon retrieval index

Revision ID: 0029_long_horizon_retrieval_index
Revises: 0028_compact_scene_checkpoints
"""
from alembic import op
import sqlalchemy as sa


revision = "0029_long_horizon_retrieval_index"
down_revision = "0028_compact_scene_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_cognition_retrieval_indexes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("protocol_version", sa.String(60), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("built_through_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed_knowledge_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed_memory_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usage_head_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_fingerprint", sa.String(120)), sa.Column("index_fingerprint", sa.String(120)),
        sa.Column("dirty_from_sequence", sa.Integer()), sa.Column("last_rebuilt_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", name="uq_project_cognition_retrieval_index_project"),
    )
    op.create_index("ix_project_cognition_retrieval_indexes_project_id", "project_cognition_retrieval_indexes", ["project_id"])
    op.create_table(
        "character_knowledge_search_indexes",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("character_id", sa.String(36), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False), sa.Column("knowledge_id", sa.String(36), sa.ForeignKey("character_knowledge.id", ondelete="CASCADE"), nullable=False),
        sa.Column("knowledge_status", sa.String(30), nullable=False), sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("subject_type", sa.String(80)), sa.Column("subject_id", sa.String(200)), sa.Column("predicate", sa.String(300)), sa.Column("value_fingerprint", sa.String(120)),
        sa.Column("proposition_fingerprint", sa.String(120), nullable=False), sa.Column("source_scene_id", sa.String(36), sa.ForeignKey("scenes.id", ondelete="SET NULL")), sa.Column("acquired_at", sa.DateTime()), sa.Column("index_fingerprint", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")), sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("knowledge_id", name="uq_character_knowledge_search_knowledge"),
    )
    op.create_index("ix_knowledge_search_project_character_status", "character_knowledge_search_indexes", ["project_id", "character_id", "knowledge_status"])
    op.create_index("ix_character_knowledge_search_indexes_project_id", "character_knowledge_search_indexes", ["project_id"])
    op.create_index("ix_character_knowledge_search_indexes_character_id", "character_knowledge_search_indexes", ["character_id"])
    op.create_index("ix_character_knowledge_search_indexes_knowledge_id", "character_knowledge_search_indexes", ["knowledge_id"])
    op.create_table(
        "character_memory_search_indexes",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("character_id", sa.String(36), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False), sa.Column("memory_id", sa.String(36), sa.ForeignKey("character_memories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False), sa.Column("emotional_weight", sa.Float(), nullable=False), sa.Column("confidence", sa.Float(), nullable=False), sa.Column("happened_at", sa.DateTime()),
        sa.Column("source_scene_id", sa.String(36), sa.ForeignKey("scenes.id", ondelete="SET NULL")), sa.Column("source_sequence", sa.Integer()), sa.Column("source_bucket", sa.String(160), nullable=False),
        sa.Column("content_fingerprint", sa.String(120), nullable=False), sa.Column("index_fingerprint", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")), sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("memory_id", name="uq_character_memory_search_memory"),
    )
    op.create_index("ix_memory_search_project_character", "character_memory_search_indexes", ["project_id", "character_id"])
    op.create_index("ix_character_memory_search_indexes_project_id", "character_memory_search_indexes", ["project_id"])
    op.create_index("ix_character_memory_search_indexes_character_id", "character_memory_search_indexes", ["character_id"])
    op.create_index("ix_character_memory_search_indexes_memory_id", "character_memory_search_indexes", ["memory_id"])
    op.create_table(
        "character_memory_cue_refs",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("character_id", sa.String(36), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False), sa.Column("memory_id", sa.String(36), sa.ForeignKey("character_memories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cue_type", sa.String(30), nullable=False), sa.Column("cue_value", sa.String(300), nullable=False), sa.Column("source", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")), sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("memory_id", "cue_type", "cue_value", "source", name="uq_memory_cue_ref"),
    )
    op.create_index("ix_memory_cue_ref_lookup", "character_memory_cue_refs", ["project_id", "character_id", "cue_type", "cue_value", "memory_id"])
    op.create_index("ix_character_memory_cue_refs_project_id", "character_memory_cue_refs", ["project_id"])
    op.create_index("ix_character_memory_cue_refs_character_id", "character_memory_cue_refs", ["character_id"])
    op.create_index("ix_character_memory_cue_refs_memory_id", "character_memory_cue_refs", ["memory_id"])
    op.create_table(
        "cognition_usage_heads",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("resource_type", sa.String(50), nullable=False), sa.Column("resource_id", sa.String(36), nullable=False),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("latest_sequence", sa.Integer(), nullable=False, server_default="-1"), sa.Column("usage_fingerprint", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")), sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", "resource_type", "resource_id", name="uq_cognition_usage_head_resource"),
    )
    op.create_index("ix_cognition_usage_head_lookup", "cognition_usage_heads", ["project_id", "resource_type", "resource_id"])
    op.create_index("ix_cognition_usage_heads_project_id", "cognition_usage_heads", ["project_id"])
    op.create_table(
        "research_lexical_index_states",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("protocol_version", sa.String(60), nullable=False), sa.Column("corpus_fingerprint", sa.String(120)),
        sa.Column("active_chunk_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("total_token_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("average_document_length", sa.Float(), nullable=False, server_default="0"), sa.Column("posting_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("term_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("index_fingerprint", sa.String(120)), sa.Column("last_rebuilt_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")), sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", name="uq_research_lexical_index_state_project"),
    )
    op.create_index("ix_research_lexical_index_states_project_id", "research_lexical_index_states", ["project_id"])
    op.create_table(
        "research_chunk_lexical_indexes",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("document_id", sa.String(36), sa.ForeignKey("research_documents.id", ondelete="CASCADE"), nullable=False), sa.Column("revision_id", sa.String(36), sa.ForeignKey("research_document_revisions.id", ondelete="CASCADE"), nullable=False), sa.Column("chunk_id", sa.String(36), sa.ForeignKey("research_chunks.id", ondelete="CASCADE"), nullable=False), sa.Column("content_fingerprint", sa.String(120), nullable=False), sa.Column("token_count", sa.Integer(), nullable=False), sa.Column("index_fingerprint", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")), sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("chunk_id", name="uq_research_chunk_lexical_chunk"),
    )
    op.create_index("ix_research_chunk_lexical_project", "research_chunk_lexical_indexes", ["project_id", "document_id", "revision_id"])
    op.create_index("ix_research_chunk_lexical_indexes_project_id", "research_chunk_lexical_indexes", ["project_id"])
    op.create_index("ix_research_chunk_lexical_indexes_document_id", "research_chunk_lexical_indexes", ["document_id"])
    op.create_index("ix_research_chunk_lexical_indexes_revision_id", "research_chunk_lexical_indexes", ["revision_id"])
    op.create_index("ix_research_chunk_lexical_indexes_chunk_id", "research_chunk_lexical_indexes", ["chunk_id"])
    op.create_table(
        "research_term_postings",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("chunk_id", sa.String(36), sa.ForeignKey("research_chunks.id", ondelete="CASCADE"), nullable=False), sa.Column("term", sa.String(300), nullable=False), sa.Column("term_frequency", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")), sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("chunk_id", "term", name="uq_research_term_posting"),
    )
    op.create_index("ix_research_term_posting_lookup", "research_term_postings", ["project_id", "term", "chunk_id"])
    op.create_index("ix_research_term_posting_chunk", "research_term_postings", ["chunk_id"])
    op.create_index("ix_research_term_postings_project_id", "research_term_postings", ["project_id"])
    op.create_table(
        "research_term_stats",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("term", sa.String(300), nullable=False), sa.Column("document_frequency", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")), sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", "term", name="uq_research_term_stat"),
    )
    op.create_index("ix_research_term_stat_lookup", "research_term_stats", ["project_id", "term"])
    op.create_index("ix_research_term_stats_project_id", "research_term_stats", ["project_id"])
    op.create_index("ix_0029_character_knowledge_character_status", "character_knowledge", ["character_id", "status"])
    op.create_index("ix_0029_character_memory_character_source", "character_memories", ["character_id", "source_scene"])
    op.create_index("ix_0029_retcon_cognition_lookup", "retcon_cognition_invalidations", ["project_id", "character_id", "resource_type", "status", "resource_id"])
    op.create_index("ix_0029_causal_usage_lookup", "causal_links", ["project_id", "cause_type", "relation_type", "active", "cause_id", "sequence"])


def downgrade() -> None:
    for name, table in [
        ("ix_0029_causal_usage_lookup", "causal_links"), ("ix_0029_retcon_cognition_lookup", "retcon_cognition_invalidations"),
        ("ix_0029_character_memory_character_source", "character_memories"), ("ix_0029_character_knowledge_character_status", "character_knowledge"),
        ("ix_research_term_stats_project_id", "research_term_stats"), ("ix_research_term_stat_lookup", "research_term_stats"), ("ix_research_term_postings_project_id", "research_term_postings"), ("ix_research_term_posting_chunk", "research_term_postings"), ("ix_research_term_posting_lookup", "research_term_postings"),
        ("ix_research_chunk_lexical_indexes_chunk_id", "research_chunk_lexical_indexes"), ("ix_research_chunk_lexical_indexes_revision_id", "research_chunk_lexical_indexes"), ("ix_research_chunk_lexical_indexes_document_id", "research_chunk_lexical_indexes"), ("ix_research_chunk_lexical_indexes_project_id", "research_chunk_lexical_indexes"), ("ix_research_chunk_lexical_project", "research_chunk_lexical_indexes"), ("ix_research_lexical_index_states_project_id", "research_lexical_index_states"),
        ("ix_cognition_usage_heads_project_id", "cognition_usage_heads"), ("ix_cognition_usage_head_lookup", "cognition_usage_heads"), ("ix_character_memory_cue_refs_memory_id", "character_memory_cue_refs"), ("ix_character_memory_cue_refs_character_id", "character_memory_cue_refs"), ("ix_character_memory_cue_refs_project_id", "character_memory_cue_refs"), ("ix_memory_cue_ref_lookup", "character_memory_cue_refs"), ("ix_character_memory_search_indexes_memory_id", "character_memory_search_indexes"), ("ix_character_memory_search_indexes_character_id", "character_memory_search_indexes"), ("ix_character_memory_search_indexes_project_id", "character_memory_search_indexes"), ("ix_memory_search_project_character", "character_memory_search_indexes"),
        ("ix_character_knowledge_search_indexes_knowledge_id", "character_knowledge_search_indexes"), ("ix_character_knowledge_search_indexes_character_id", "character_knowledge_search_indexes"), ("ix_character_knowledge_search_indexes_project_id", "character_knowledge_search_indexes"), ("ix_knowledge_search_project_character_status", "character_knowledge_search_indexes"), ("ix_project_cognition_retrieval_indexes_project_id", "project_cognition_retrieval_indexes"),
    ]:
        # Derived-index deployments may have been stamped during a rolling
        # upgrade before every optional accelerator index existed. Downgrade
        # still removes only Phase16C1 projections and remains idempotent.
        op.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
    for table in ["research_term_stats", "research_term_postings", "research_chunk_lexical_indexes", "research_lexical_index_states", "cognition_usage_heads", "character_memory_cue_refs", "character_memory_search_indexes", "character_knowledge_search_indexes", "project_cognition_retrieval_indexes"]:
        op.drop_table(table)
