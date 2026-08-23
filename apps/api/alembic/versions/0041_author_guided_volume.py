"""author-led book contracts, rolling chapter windows and sealed continuity

Revision ID: 0041_author_guided_volume
Revises: 0040_auto_director_usage_metrics
"""
import json
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0041_author_guided_volume"
down_revision = "0040_auto_director_usage_metrics"
branch_labels = None
depends_on = None


def _id() -> str:
    return str(uuid.uuid4())


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def upgrade() -> None:
    op.create_table(
        "book_contracts",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(300)), sa.Column("theme", sa.Text()), sa.Column("premise", sa.Text()), sa.Column("ending_direction", sa.Text()),
        sa.Column("protagonist_contract", sa.JSON(), nullable=False, server_default="{}"), sa.Column("global_plot_direction", sa.Text()),
        sa.Column("global_required_events", sa.JSON(), nullable=False, server_default="[]"), sa.Column("global_forbidden_events", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("style_contract", sa.JSON(), nullable=False, server_default="{}"), sa.Column("author_locked_constraints", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("length_policy", sa.JSON(), nullable=False, server_default="{}"), sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("fingerprint", sa.String(120), nullable=False, server_default=""), sa.Column("status", sa.String(30), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", name="uq_book_contract_project"),
    )
    op.create_index("ix_book_contract_project", "book_contracts", ["project_id"])
    op.create_index("ix_book_contract_project_status", "book_contracts", ["project_id", "status"])

    op.create_table(
        "volume_contracts",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("book_contract_id", sa.String(36), sa.ForeignKey("book_contracts.id")),
        sa.Column("volume_number", sa.Integer(), nullable=False), sa.Column("title", sa.String(300)), sa.Column("status", sa.String(30), nullable=False, server_default="DRAFT"),
        sa.Column("estimated_chapter_start", sa.Integer()), sa.Column("estimated_chapter_end", sa.Integer()), sa.Column("actual_chapter_start", sa.Integer()), sa.Column("actual_chapter_end", sa.Integer()),
        sa.Column("volume_goal", sa.Text()), sa.Column("core_conflict", sa.Text()), sa.Column("opening_state", sa.JSON(), nullable=False, server_default="{}"), sa.Column("target_closing_state", sa.JSON(), nullable=False, server_default="{}"), sa.Column("completion_conditions", sa.JSON(), nullable=False, server_default="[]"), sa.Column("protagonist_arc", sa.JSON(), nullable=False, server_default="{}"), sa.Column("main_cast", sa.JSON(), nullable=False, server_default="[]"), sa.Column("new_cast", sa.JSON(), nullable=False, server_default="[]"), sa.Column("required_events", sa.JSON(), nullable=False, server_default="[]"), sa.Column("forbidden_events", sa.JSON(), nullable=False, server_default="[]"), sa.Column("allowed_reveals", sa.JSON(), nullable=False, server_default="[]"), sa.Column("forbidden_reveals", sa.JSON(), nullable=False, server_default="[]"), sa.Column("foreshadowing_seed_refs", sa.JSON(), nullable=False, server_default="[]"), sa.Column("foreshadowing_payoff_refs", sa.JSON(), nullable=False, server_default="[]"), sa.Column("unresolved_threads", sa.JSON(), nullable=False, server_default="[]"), sa.Column("next_volume_hooks", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"), sa.Column("fingerprint", sa.String(120), nullable=False, server_default=""), sa.Column("sealed_at", sa.DateTime()), sa.Column("sealed_snapshot_id", sa.String(36)), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", "volume_number", name="uq_volume_contract_project_number"),
    )
    op.create_index("ix_volume_contract_project", "volume_contracts", ["project_id"])
    op.create_index("ix_volume_contract_project_status", "volume_contracts", ["project_id", "status"])

    op.create_table(
        "chapter_planning_windows",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("volume_id", sa.String(36), sa.ForeignKey("volume_contracts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("start_chapter_number", sa.Integer(), nullable=False), sa.Column("end_chapter_number", sa.Integer(), nullable=False), sa.Column("actual_generated_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("status", sa.String(20), nullable=False, server_default="PLANNING"), sa.Column("plan_fingerprint", sa.String(120), nullable=False, server_default=""), sa.Column("source_volume_snapshot_id", sa.String(36)), sa.Column("author_note", sa.Text()), sa.Column("continuation_decision", sa.String(40)), sa.Column("error_code", sa.String(120)), sa.Column("error_summary", sa.Text()), sa.Column("completed_at", sa.DateTime()), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("volume_id", "start_chapter_number", "end_chapter_number", name="uq_chapter_window_range"),
    )
    op.create_index("ix_chapter_window_project", "chapter_planning_windows", ["project_id"])
    op.create_index("ix_chapter_window_volume", "chapter_planning_windows", ["volume_id"])
    op.create_index("ix_chapter_window_project_status", "chapter_planning_windows", ["project_id", "status"])

    op.create_table(
        "volume_continuity_snapshots",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("book_contract_id", sa.String(36), sa.ForeignKey("book_contracts.id")), sa.Column("volume_id", sa.String(36), sa.ForeignKey("volume_contracts.id", ondelete="CASCADE"), nullable=False), sa.Column("snapshot_version", sa.Integer(), nullable=False, server_default="1"), sa.Column("summary", sa.Text()), sa.Column("confirmed_facts", sa.JSON(), nullable=False, server_default="[]"), sa.Column("character_states", sa.JSON(), nullable=False, server_default="{}"), sa.Column("relationship_changes", sa.JSON(), nullable=False, server_default="[]"), sa.Column("timeline_end", sa.JSON(), nullable=False, server_default="{}"), sa.Column("location_states", sa.JSON(), nullable=False, server_default="{}"), sa.Column("item_states", sa.JSON(), nullable=False, server_default="{}"), sa.Column("active_threads", sa.JSON(), nullable=False, server_default="[]"), sa.Column("unresolved_foreshadowings", sa.JSON(), nullable=False, server_default="[]"), sa.Column("paid_off_foreshadowings", sa.JSON(), nullable=False, server_default="[]"), sa.Column("forbidden_future_reveals", sa.JSON(), nullable=False, server_default="[]"), sa.Column("next_volume_hooks", sa.JSON(), nullable=False, server_default="[]"), sa.Column("source_fingerprint", sa.String(120), nullable=False, server_default=""),
    )
    op.create_index("ix_volume_snapshot_project", "volume_continuity_snapshots", ["project_id"])
    op.create_index("ix_volume_snapshot_volume", "volume_continuity_snapshots", ["volume_id"])

    op.create_table(
        "foreshadowing_ledger",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("foreshadow_ref", sa.String(160), nullable=False), sa.Column("title", sa.String(300), nullable=False), sa.Column("description", sa.Text()), sa.Column("source_volume_id", sa.String(36), sa.ForeignKey("volume_contracts.id")), sa.Column("source_chapter_id", sa.String(36), sa.ForeignKey("chapters.id")), sa.Column("status", sa.String(30), nullable=False, server_default="SEEDED"), sa.Column("earliest_payoff_volume", sa.Integer()), sa.Column("target_payoff_volume", sa.Integer()), sa.Column("allowed_reveal_level", sa.String(50)), sa.Column("related_character_ids", sa.JSON(), nullable=False, server_default="[]"), sa.Column("related_fact_ids", sa.JSON(), nullable=False, server_default="[]"), sa.Column("aliases", sa.JSON(), nullable=False, server_default="[]"), sa.Column("fingerprint", sa.String(120), nullable=False, server_default=""),
        sa.UniqueConstraint("project_id", "foreshadow_ref", name="uq_foreshadow_project_ref"),
    )
    op.create_index("ix_foreshadow_project", "foreshadowing_ledger", ["project_id"])
    op.create_index("ix_foreshadow_project_status", "foreshadowing_ledger", ["project_id", "status"])

    op.create_table("author_guidance", sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("volume_id", sa.String(36), sa.ForeignKey("volume_contracts.id")), sa.Column("window_id", sa.String(36), sa.ForeignKey("chapter_planning_windows.id")), sa.Column("author_note", sa.Text(), nullable=False), sa.Column("author_locked_constraints", sa.JSON(), nullable=False, server_default="[]"), sa.Column("author_override_reason", sa.Text()), sa.Column("affected_scope", sa.String(40), nullable=False, server_default="WINDOW"), sa.Column("requires_replan", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("analysis", sa.JSON(), nullable=False, server_default="{}"), sa.Column("status", sa.String(30), nullable=False, server_default="APPLIED"), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_author_guidance_project", "author_guidance", ["project_id"])
    op.create_index("ix_author_guidance_project_scope", "author_guidance", ["project_id", "affected_scope"])

    op.create_table("book_completion_proposals", sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("book_contract_id", sa.String(36), sa.ForeignKey("book_contracts.id")), sa.Column("status", sa.String(30), nullable=False, server_default="NOT_READY"), sa.Column("reason", sa.Text()), sa.Column("unresolved_threads", sa.JSON(), nullable=False, server_default="[]"), sa.Column("unresolved_foreshadowings", sa.JSON(), nullable=False, server_default="[]"), sa.Column("protagonist_arc_status", sa.JSON(), nullable=False, server_default="{}"), sa.Column("main_conflict_status", sa.JSON(), nullable=False, server_default="{}"), sa.Column("ending_requirements", sa.JSON(), nullable=False, server_default="[]"), sa.Column("evidence_chapter_ids", sa.JSON(), nullable=False, server_default="[]"), sa.Column("generated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_book_completion_project", "book_completion_proposals", ["project_id"])
    op.create_index("ix_book_completion_project_status", "book_completion_proposals", ["project_id", "status"])

    # Preserve legacy estimates in the new contract without turning them into limits.
    bind = op.get_bind()
    projects = bind.execute(sa.text("SELECT id, name, story_seed, autonomy_settings FROM projects")).mappings().all()
    for project in projects:
        settings = project.get("autonomy_settings") or {}
        if isinstance(settings, str):
            try: settings = json.loads(settings)
            except Exception: settings = {}
        settings = settings if isinstance(settings, dict) else {}
        estimated = settings.get("target_chapters") or settings.get("estimated_chapters")
        budget = settings.get("max_chapters") or settings.get("operational_run_chapter_budget")
        contract_id = _id()
        bind.execute(sa.text("INSERT INTO book_contracts (id, project_id, title, premise, protagonist_contract, global_required_events, global_forbidden_events, style_contract, author_locked_constraints, length_policy, version, fingerprint, status) VALUES (:id, :project_id, :title, :premise, :protagonist_contract, :global_required_events, :global_forbidden_events, :style_contract, :author_locked_constraints, :length_policy, 1, :fingerprint, :status)"), {"id": contract_id, "project_id": project["id"], "title": project["name"], "premise": project["story_seed"], "protagonist_contract": _json({}), "global_required_events": _json([]), "global_forbidden_events": _json([]), "style_contract": _json({}), "author_locked_constraints": _json([]), "length_policy": _json({"mode": "ESTIMATE_ONLY", "estimated_chapters": estimated, "estimated_volumes": None, "completion_strategy": "AUTHOR_CONFIRMATION", "operational_run_chapter_budget": budget, "operational_token_budget": None}), "fingerprint": "legacy-import", "status": "ACTIVE"})


def downgrade() -> None:
    for name in ("book_completion_proposals", "author_guidance", "foreshadowing_ledger", "volume_continuity_snapshots", "chapter_planning_windows", "volume_contracts", "book_contracts"):
        op.drop_table(name)
