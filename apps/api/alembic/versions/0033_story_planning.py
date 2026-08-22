"""versioned whole-book planning and chapter task sheets

Revision ID: 0033_story_planning
Revises: 0032_narrative_structure_projection
"""
from alembic import op
import sqlalchemy as sa

revision = "0033_story_planning"
down_revision = "0032_narrative_structure_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "story_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
        sa.Column("framing", sa.JSON(), nullable=False),
        sa.Column("premise", sa.Text()),
        sa.Column("macro_plan", sa.JSON(), nullable=False),
        sa.Column("style_guide", sa.JSON(), nullable=False),
        sa.Column("anti_ai_rules", sa.JSON(), nullable=False),
        sa.Column("source_fingerprint", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(100)),
        sa.Column("model", sa.String(200)),
        sa.Column("request_id", sa.String(200)),
        sa.Column("generation_report", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", "version", name="uq_story_plan_project_version"),
    )
    op.create_index("ix_story_plans_project_id", "story_plans", ["project_id"])
    op.create_index("ix_story_plans_status", "story_plans", ["status"])
    op.create_table(
        "story_plan_volumes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("story_plans.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False), sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False), sa.Column("start_chapter", sa.Integer(), nullable=False),
        sa.Column("end_chapter", sa.Integer(), nullable=False), sa.Column("arc_numbers", sa.JSON(), nullable=False),
        sa.Column("turning_points", sa.JSON(), nullable=False),
        sa.UniqueConstraint("plan_id", "number", name="uq_story_plan_volume_number"),
    )
    op.create_index("ix_story_plan_volumes_plan_id", "story_plan_volumes", ["plan_id"])
    op.create_table(
        "story_plan_arcs",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("plan_id", sa.String(36), sa.ForeignKey("story_plans.id"), nullable=False),
        sa.Column("volume_number", sa.Integer(), nullable=False), sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False), sa.Column("goal", sa.Text(), nullable=False), sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("turning_points", sa.JSON(), nullable=False), sa.Column("thread_refs", sa.JSON(), nullable=False),
        sa.UniqueConstraint("plan_id", "number", name="uq_story_plan_arc_number"),
    )
    op.create_index("ix_story_plan_arcs_plan_id", "story_plan_arcs", ["plan_id"])
    op.create_table(
        "story_plan_chapters",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("story_plans.id"), nullable=False), sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("volume_number", sa.Integer(), nullable=False), sa.Column("arc_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False), sa.Column("summary", sa.Text(), nullable=False), sa.Column("pov_mode", sa.String(40), nullable=False),
        sa.Column("pov_character_ref", sa.String(200)), sa.Column("cast_refs", sa.JSON(), nullable=False), sa.Column("location", sa.String(200)), sa.Column("time_anchor", sa.String(200)),
        sa.Column("start_state", sa.JSON(), nullable=False), sa.Column("end_state", sa.JSON(), nullable=False), sa.Column("objective", sa.Text(), nullable=False), sa.Column("conflict", sa.Text(), nullable=False),
        sa.Column("must_events", sa.JSON(), nullable=False), sa.Column("forbidden_events", sa.JSON(), nullable=False), sa.Column("allowed_reveals", sa.JSON(), nullable=False), sa.Column("forbidden_reveals", sa.JSON(), nullable=False),
        sa.Column("foreshadow_create", sa.JSON(), nullable=False), sa.Column("foreshadow_payoff", sa.JSON(), nullable=False), sa.Column("character_changes", sa.JSON(), nullable=False), sa.Column("consequences", sa.JSON(), nullable=False), sa.Column("scene_beats", sa.JSON(), nullable=False),
        sa.Column("target_words", sa.Integer(), nullable=False, server_default="3000"), sa.Column("pace", sa.String(30), nullable=False, server_default="MEDIUM"), sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"), sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("plan_id", "number", name="uq_story_plan_chapter_number"),
    )
    op.create_index("ix_story_plan_chapters_project_id", "story_plan_chapters", ["project_id"])
    op.create_index("ix_story_plan_chapters_plan_id", "story_plan_chapters", ["plan_id"])
    op.create_index("ix_story_plan_chapter_plan_number", "story_plan_chapters", ["plan_id", "number"])


def downgrade() -> None:
    op.drop_index("ix_story_plan_chapter_plan_number", table_name="story_plan_chapters")
    op.drop_index("ix_story_plan_chapters_plan_id", table_name="story_plan_chapters")
    op.drop_index("ix_story_plan_chapters_project_id", table_name="story_plan_chapters")
    op.drop_table("story_plan_chapters")
    op.drop_index("ix_story_plan_arcs_plan_id", table_name="story_plan_arcs")
    op.drop_table("story_plan_arcs")
    op.drop_index("ix_story_plan_volumes_plan_id", table_name="story_plan_volumes")
    op.drop_table("story_plan_volumes")
    op.drop_index("ix_story_plans_status", table_name="story_plans")
    op.drop_index("ix_story_plans_project_id", table_name="story_plans")
    op.drop_table("story_plans")
