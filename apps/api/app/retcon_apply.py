"""Explicit, deterministic Phase 6B Retcon apply and cognition quarantine."""
from __future__ import annotations
from datetime import datetime
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    CharacterKnowledge, CharacterMemory, RetconApplication, RetconApplicationStatus,
    RetconCognitionInvalidation, RetconCognitionInvalidationStatus, RetconImpactItem,
    RetconImpactPlan, RetconRequest, RevisionStatus, RevisionApplication, WorldSnapshot,
)
from .retcon import RetconPlanStalenessChecker, semantic_fingerprint
from .revision import RevisionChangeNormalizer
from .versioning import RevisionApplyService, WorldSnapshotBuilder, _record, target_fingerprint


def has_pending_replay(db: Session, project_id: str) -> bool:
    return db.scalar(select(RetconApplication.id).where(
        RetconApplication.project_id == project_id,
        RetconApplication.status == RetconApplicationStatus.APPLIED_PENDING_REPLAY,
    ).limit(1)) is not None


class RetconApplyService:
    def _fail(self, code: str):
        raise ValueError(code)

    def _blocking(self, plan: RetconImpactPlan) -> bool:
        report = plan.validation_report or {}
        return any(str(issue.get("severity", "")).upper() == "BLOCKING" for issue in report.get("issues", []))

    def _author_override_required(self, revision, items) -> bool:
        if (revision.impact_report or {}).get("author_override_required"):
            return True
        return any(item.resource_type == "CANON_FACT" and item.classification in {"INVALIDATED", "REPLAY_REQUIRED"} for item in items)

    def _prepare(self, db: Session, project_id: str, request: RetconRequest, plan: RetconImpactPlan,
                 revision, author_override: bool, author_override_reason: str | None):
        if request.project_id != project_id or plan.project_id != project_id or revision.project_id != project_id:
            self._fail("CROSS_PROJECT_REFERENCE")
        existing = db.scalar(select(RetconApplication).where(RetconApplication.retcon_request_id == request.id))
        if existing:
            self._fail("RETCON_ALREADY_APPLIED")
        if request.status == "ABORTED":
            self._fail("RETCON_REQUEST_ABORTED")
        if plan.retcon_request_id != request.id:
            self._fail("CROSS_PROJECT_REFERENCE")
        latest = db.scalar(select(RetconImpactPlan).where(RetconImpactPlan.retcon_request_id == request.id).order_by(RetconImpactPlan.version.desc(), RetconImpactPlan.id.desc()))
        if not latest or latest.id != plan.id:
            self._fail("RETCON_PLAN_NOT_LATEST")
        if plan.status != "READY":
            self._fail("RETCON_PLAN_BLOCKED" if plan.status == "BLOCKED" else "RETCON_PLAN_NOT_READY")
        if RetconPlanStalenessChecker().is_stale(db, plan):
            self._fail("RETCON_PLAN_STALE")
        if revision.status != RevisionStatus.PREVIEWED:
            self._fail("SOURCE_REVISION_NOT_PREVIEWED")
        if revision.id != request.source_revision_id:
            self._fail("CROSS_PROJECT_REFERENCE")
        items = db.scalars(select(RetconImpactItem).where(RetconImpactItem.plan_id == plan.id)).all()
        if self._blocking(plan):
            self._fail("RETCON_PLAN_BLOCKED")
        if any(item.classification == "REVALIDATE" for item in items):
            self._fail("RETCON_REVALIDATION_REQUIRED")
        replay_items = [item for item in items if item.classification in {"REPLAY_REQUIRED", "INVALIDATED"}]
        if replay_items and plan.earliest_affected_sequence is None:
            self._fail("RETCON_REPLAY_BOUNDARY_REQUIRED")
        if not author_override and self._author_override_required(revision, items):
            self._fail("AUTHOR_OVERRIDE_REQUIRED")
        if self._author_override_required(revision, items) and not (author_override_reason or "").strip():
            self._fail("AUTHOR_OVERRIDE_REQUIRED")

        changes = RevisionChangeNormalizer().normalize(db, project_id, RevisionApplyService()._changes(revision))
        expected = revision.normalized_changes or []
        if len(changes) != len(expected) or any(a.get("target_fingerprint_before") != b.get("target_fingerprint_before") for a, b in zip(changes, expected)):
            self._fail("TARGET_STATE_STALE")
        candidates = RevisionApplyService()._candidates(db, project_id, RevisionApplyService()._changes(revision))
        normalizer = RevisionChangeNormalizer()
        for (target_type, _), candidate in candidates.items():
            normalizer._validate_target(candidate, target_type, db, project_id)
            normalizer._validate_references(candidate, db, project_id)
        return changes, candidates, items

    def apply(self, db: Session, project_id: str, request: RetconRequest, plan: RetconImpactPlan,
              revision, explicit_confirmation: bool, author_override: bool = False,
              author_override_reason: str | None = None):
        if not explicit_confirmation:
            self._fail("EXPLICIT_CONFIRMATION_REQUIRED")
        changes, candidates, items = self._prepare(db, project_id, request, plan, revision, author_override, author_override_reason)
        pre_payload, pre_fingerprint = WorldSnapshotBuilder().build(db, project_id)
        application = RetconApplication(
            project_id=project_id, retcon_request_id=request.id, retcon_plan_id=plan.id,
            source_revision_id=revision.id, status=RetconApplicationStatus.PENDING,
            plan_basis_fingerprint=plan.basis_fingerprint,
            pre_apply_world_fingerprint=pre_fingerprint,
            cognition_summary={}, replay_summary={},
        )
        db.add(application); db.flush()

        revision_service = RevisionApplyService()
        revision_application = revision_service.apply(
            db, project_id, revision, author_override, author_override_reason,
            prepared=(plan.basis_fingerprint, changes, candidates),
        )
        application.revision_application_id = revision_application.id
        invalidations = []
        seen = set()
        for item in items:
            if item.classification != "REBUILD_COGNITION" or item.resource_type not in {"CHARACTER_KNOWLEDGE", "CHARACTER_MEMORY"}:
                continue
            key = (item.resource_type, item.resource_id)
            if key in seen:
                continue
            row = db.get(CharacterKnowledge if item.resource_type == "CHARACTER_KNOWLEDGE" else CharacterMemory, item.resource_id)
            if not row:
                self._fail("COGNITION_TARGET_NOT_FOUND")
            character_id = item.character_id or row.character_id
            invalidation = RetconCognitionInvalidation(
                project_id=project_id, retcon_application_id=application.id,
                character_id=character_id, resource_type="KNOWLEDGE" if item.resource_type == "CHARACTER_KNOWLEDGE" else "MEMORY",
                resource_id=item.resource_id, source_impact_item_id=item.id,
                reason=item.reason_summary or item.reason_code,
                original_semantic_fingerprint=semantic_fingerprint(_record(row)),
                status=RetconCognitionInvalidationStatus.ACTIVE,
            )
            db.add(invalidation); invalidations.append(invalidation); seen.add(key)
        db.flush()
        post_snapshot = db.get(WorldSnapshot, revision_application.post_snapshot_id)
        application.post_apply_world_fingerprint = post_snapshot.state_fingerprint if post_snapshot else None
        application.cognition_summary = {
            "knowledge_count": sum(item.resource_type == "KNOWLEDGE" for item in invalidations),
            "memory_count": sum(item.resource_type == "MEMORY" for item in invalidations),
            "invalidation_ids": [item.id for item in invalidations],
        }
        application.replay_summary = {
            "earliest_affected_scene_id": plan.earliest_affected_scene_id,
            "earliest_affected_sequence": plan.earliest_affected_sequence,
            "replay_scene_ids": [item.resource_id for item in items if item.resource_type == "SCENE" and item.classification == "REPLAY_REQUIRED"],
            "preserved_scene_count": (plan.impact_summary or {}).get("preserved_scene_count", 0),
            "preserved_scene_ranges": (plan.impact_summary or {}).get("preserved_scene_ranges", []),
        }
        application.status = RetconApplicationStatus.APPLIED_PENDING_REPLAY
        application.applied_at = datetime.utcnow()
        request.status = "APPLIED_PENDING_REPLAY"
        db.flush()
        return application, revision_application, invalidations

    def rollback(self, db: Session, project_id: str, application: RetconApplication):
        if application.status != RetconApplicationStatus.APPLIED_PENDING_REPLAY:
            self._fail("RETCON_ALREADY_ROLLED_BACK")
        latest = db.scalar(select(RetconApplication).where(RetconApplication.project_id == project_id, RetconApplication.status == RetconApplicationStatus.APPLIED_PENDING_REPLAY).order_by(RetconApplication.created_at.desc(), RetconApplication.id.desc()))
        if not latest or latest.id != application.id:
            self._fail("RETCON_ROLLBACK_NOT_LATEST")
        revision_application = db.get(RevisionApplication, application.revision_application_id)
        if not revision_application:
            self._fail("RETCON_REVISION_APPLICATION_MISSING")
        RevisionApplyService().rollback(db, project_id, revision_application)
        for row in db.scalars(select(RetconCognitionInvalidation).where(RetconCognitionInvalidation.retcon_application_id == application.id, RetconCognitionInvalidation.status == RetconCognitionInvalidationStatus.ACTIVE)).all():
            row.status = RetconCognitionInvalidationStatus.ROLLED_BACK
        application.status = RetconApplicationStatus.ROLLED_BACK
        application.rolled_back_at = datetime.utcnow()
        request = db.get(RetconRequest, application.retcon_request_id)
        if request:
            request.status = "ROLLED_BACK"
        db.flush()
        return application
