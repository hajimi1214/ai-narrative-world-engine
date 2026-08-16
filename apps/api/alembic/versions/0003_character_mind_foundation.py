"""character mind foundation"""
from alembic import op
import sqlalchemy as sa

revision = "0003_character_mind_foundation"
down_revision = "0002_director_foundation"
branch_labels = None
depends_on = None

decision_type = sa.Enum("ACT", "WAIT", "ASK", "INVESTIGATE", "CONFRONT", "WITHDRAW", "REFUSE", "HELP", "HIDE", "NEGOTIATE", "OBSERVE", "CUSTOM", name="characterdecisiontype")
decision_status = sa.Enum("DRAFT", "VALID", "REJECTED", "SUPERSEDED", name="characterdecisionstatus")

def upgrade() -> None:
    op.create_table("character_decisions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False), sa.Column("scene_proposal_id", sa.String(36), sa.ForeignKey("scene_proposals.id"), nullable=False), sa.Column("character_id", sa.String(36), sa.ForeignKey("characters.id"), nullable=False), sa.Column("context_fingerprint", sa.String(100), nullable=False), sa.Column("decision_type", decision_type, nullable=False), sa.Column("intent", sa.Text(), nullable=False), sa.Column("chosen_action", sa.Text(), nullable=False), sa.Column("target_character_id", sa.String(36)), sa.Column("target_entity_id", sa.String(36)), sa.Column("motivation", sa.Text(), nullable=False), sa.Column("goal_refs", sa.JSON(), nullable=False), sa.Column("knowledge_used", sa.JSON(), nullable=False), sa.Column("memory_refs", sa.JSON(), nullable=False), sa.Column("ability_refs", sa.JSON(), nullable=False), sa.Column("inventory_refs", sa.JSON(), nullable=False), sa.Column("relationship_factors", sa.JSON(), nullable=False), sa.Column("perceived_risk", sa.Text()), sa.Column("accepted_cost", sa.Text()), sa.Column("expected_personal_result", sa.Text()), sa.Column("uncertainties", sa.JSON(), nullable=False), sa.Column("refused_options", sa.JSON(), nullable=False), sa.Column("boundary_override_reason", sa.Text()), sa.Column("decision_summary", sa.Text(), nullable=False), sa.Column("status", decision_status, nullable=False, server_default="DRAFT"), sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False))
    op.create_index("ix_character_decisions_project_id", "character_decisions", ["project_id"])
    op.create_index("ix_character_decisions_scene_proposal_id", "character_decisions", ["scene_proposal_id"])
    op.create_index("ix_character_decisions_character_id", "character_decisions", ["character_id"])

def downgrade() -> None:
    for index in ("ix_character_decisions_character_id", "ix_character_decisions_scene_proposal_id", "ix_character_decisions_project_id"): op.drop_index(index, table_name="character_decisions")
    op.drop_table("character_decisions")
    for enum in (decision_status, decision_type): enum.drop(op.get_bind(), checkfirst=True)
