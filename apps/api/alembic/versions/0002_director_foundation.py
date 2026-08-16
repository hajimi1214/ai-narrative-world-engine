"""director foundation tables"""
from alembic import op
import sqlalchemy as sa

revision = "0002_director_foundation"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

proposal_type = sa.Enum("CONTINUE_THREAD", "CHARACTER_DRIVEN", "CONSEQUENCE", "REVEAL", "ESCALATION", "RELATIONSHIP", "TRANSITION", "NEW_THREAD", name="proposaltype")
proposal_status = sa.Enum("DRAFT", "VALID", "REJECTED", "APPROVED", "EXECUTED", name="proposalstatus")
reveal_status = sa.Enum("LOCKED", "AVAILABLE", "REVEALED", name="revealstatus")
decision_type = sa.Enum("DRY_RUN", "APPROVE", "REJECT", name="decisiontype")

def upgrade() -> None:
    op.create_table("reveal_constraints", sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False), sa.Column("canon_fact_id", sa.String(36), sa.ForeignKey("canon_facts.id"), nullable=False), sa.Column("status", reveal_status, nullable=False, server_default="LOCKED"), sa.Column("minimum_condition", sa.Text()), sa.Column("allowed_character_ids", sa.JSON(), nullable=False), sa.Column("notes", sa.Text()), sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False), sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False))
    op.create_table("scene_proposals", sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False), sa.Column("context_fingerprint", sa.String(100), nullable=False), sa.Column("proposal_type", proposal_type, nullable=False), sa.Column("primary_thread_id", sa.String(36), sa.ForeignKey("story_threads.id")), sa.Column("location_id", sa.String(36)), sa.Column("proposed_location", sa.String(200)), sa.Column("participants", sa.JSON(), nullable=False), sa.Column("scene_goal", sa.Text(), nullable=False), sa.Column("character_motivations", sa.JSON(), nullable=False), sa.Column("entry_state", sa.JSON(), nullable=False), sa.Column("planned_pressure", sa.Text()), sa.Column("expected_progress", sa.JSON(), nullable=False), sa.Column("allowed_reveals", sa.JSON(), nullable=False), sa.Column("forbidden_reveals", sa.JSON(), nullable=False), sa.Column("required_canon", sa.JSON(), nullable=False), sa.Column("possible_outcomes", sa.JSON(), nullable=False), sa.Column("new_entity_requests", sa.JSON(), nullable=False), sa.Column("risk_flags", sa.JSON(), nullable=False), sa.Column("director_reasoning_summary", sa.Text(), nullable=False), sa.Column("status", proposal_status, nullable=False, server_default="DRAFT"), sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False), sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False))
    op.create_table("director_decision_logs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False), sa.Column("context_version", sa.String(100), nullable=False), sa.Column("proposal_id", sa.String(36), sa.ForeignKey("scene_proposals.id")), sa.Column("decision_type", decision_type, nullable=False), sa.Column("brief_reason", sa.Text(), nullable=False), sa.Column("validation_result", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False))
    for table in ("reveal_constraints", "scene_proposals", "director_decision_logs"): op.create_index(f"ix_{table}_project_id", table, ["project_id"])

def downgrade() -> None:
    for table in ("director_decision_logs", "scene_proposals", "reveal_constraints"): op.drop_index(f"ix_{table}_project_id", table_name=table)
    for table in ("director_decision_logs", "scene_proposals", "reveal_constraints"): op.drop_table(table)
    for enum in (decision_type, reveal_status, proposal_status, proposal_type): enum.drop(op.get_bind(), checkfirst=True)
