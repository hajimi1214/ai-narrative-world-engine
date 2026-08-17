"""world resolution take-local foundation"""
from alembic import op
import sqlalchemy as sa

revision = "0005_world_resolution_foundation"
down_revision = "0004_scene_performance_foundation"
branch_labels = None
depends_on = None

resolver_mode = sa.Enum("HEURISTIC", "LLM", name="resolvermode")
resolution_status = sa.Enum("VALID", "REJECTED", "UNRESOLVED", name="resolutionstatus")
resolution_outcome = sa.Enum("SUCCESS", "PARTIAL", "FAILURE", "NO_EFFECT", "INTERRUPTED", "UNRESOLVED", name="resolutionoutcome")

def upgrade() -> None:
    op.create_table("world_resolutions",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("performance_id", sa.String(36), sa.ForeignKey("scene_performances.id"), nullable=False), sa.Column("performance_turn_id", sa.String(36), sa.ForeignKey("scene_performance_turns.id"), nullable=False),
        sa.Column("resolver_mode", resolver_mode, nullable=False), sa.Column("world_context_fingerprint", sa.String(100), nullable=False),
        sa.Column("status", resolution_status, nullable=False), sa.Column("outcome", resolution_outcome, nullable=False), sa.Column("outcome_summary", sa.Text(), nullable=False),
        sa.Column("objective_facts", sa.JSON(), nullable=False), sa.Column("actor_observation", sa.Text()), sa.Column("public_observation", sa.Text()), sa.Column("recipient_character_ids", sa.JSON(), nullable=False),
        sa.Column("canon_fact_ids_used", sa.JSON(), nullable=False), sa.Column("world_entity_ids_used", sa.JSON(), nullable=False), sa.Column("resolution_basis_summary", sa.Text()), sa.Column("missing_information", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False), sa.UniqueConstraint("performance_turn_id", name="uq_world_resolutions_turn"))
    op.create_index("ix_world_resolutions_project_id", "world_resolutions", ["project_id"])
    op.create_index("ix_world_resolutions_performance_id", "world_resolutions", ["performance_id"])

def downgrade() -> None:
    op.drop_index("ix_world_resolutions_performance_id", table_name="world_resolutions")
    op.drop_index("ix_world_resolutions_project_id", table_name="world_resolutions")
    op.drop_table("world_resolutions")
    for enum in (resolution_outcome, resolution_status, resolver_mode): enum.drop(op.get_bind(), checkfirst=True)
