import copy
import hashlib
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CanonFact, Character, CharacterKnowledge, CharacterMemory, Chapter, Project, RevealConstraint, RevisionApplication, RevisionApplicationStatus, RevisionStatus, Scene, StoryArc, StoryThread, WorldEntity, WorldRevision, WorldSnapshot, SnapshotType
from .revision import RevisionChangeNormalizer, RevisionChangePayload, RevisionPatchEngine, RevisionStateFingerprintBuilder, _record, target_fingerprint
from .snapshot_storage import ProjectWorldSnapshotHeadService, SnapshotPayloadResolver


def formal_world_state_v2_fingerprint(payload):
    """Compatibility export for callers that keep fingerprint helpers here."""
    from .formal_state import formal_world_state_v2_fingerprint as calculate
    return calculate(payload)


class WorldSnapshotBuilder:
    protocol = "world-snapshot-v1"
    MODELS = (CanonFact, WorldEntity, Character, CharacterKnowledge, CharacterMemory, RevealConstraint, StoryThread, StoryArc, Scene, Chapter)

    def build(self, db: Session, project_id: str):
        from .formal_state import FormalSnapshotRowSerializer
        serializer = FormalSnapshotRowSerializer()
        characters = db.scalars(select(Character).where(Character.project_id == project_id)).all()
        character_ids = [item.id for item in characters]
        project = db.get(Project, project_id)
        data = {"project": serializer.row_payload(project)}
        for model in self.MODELS:
            if model in (CharacterKnowledge, CharacterMemory):
                rows = db.scalars(select(model).where(model.character_id.in_(character_ids)).order_by(model.id)).all() if character_ids else []
            else:
                rows = db.scalars(select(model).where(model.project_id == project_id).order_by(model.id)).all()
            values = []
            for row in rows:
                value = serializer.row_payload(row)
                values.append(value)
            data[model.__tablename__] = values
        stable = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
        return data, "world-snapshot-v1:" + hashlib.sha256(stable.encode()).hexdigest()

    def create(self, db: Session, project_id: str, kind: SnapshotType, revision_id: str | None = None):
        payload, fingerprint = self.build(db, project_id)
        snapshot = WorldSnapshot(project_id=project_id, snapshot_type=kind, state_fingerprint=fingerprint, payload=payload, source_revision_id=revision_id)
        db.add(snapshot)
        db.flush()
        return snapshot


class RevisionApplyService:
    MODELS = {"CANON_FACT": CanonFact, "WORLD_ENTITY": WorldEntity, "CHARACTER": Character}
    SNAPSHOT_KEYS = {"CANON_FACT": "canon_facts", "WORLD_ENTITY": "world_entities", "CHARACTER": "characters"}
    IMMUTABLE = {"id", "project_id", "created_at", "updated_at"}

    def _changes(self, revision):
        return [RevisionChangePayload.model_validate(item) for item in revision.change_set]

    def _candidates(self, db, project_id, changes):
        candidates = {}
        engine = RevisionPatchEngine()
        for change in changes:
            key = (change.target_type, change.target_id)
            if key not in candidates:
                target = db.get(self.MODELS[change.target_type], change.target_id)
                if not target or target.project_id != project_id:
                    raise ValueError("REVISION_TARGET_NOT_FOUND")
                candidates[key] = copy.deepcopy(_record(target))
            engine.apply(candidates[key], change.operation, change.path, change.value)
        return candidates

    def preflight(self, db, project_id, revision, override, reason):
        if revision.status != RevisionStatus.PREVIEWED:
            raise ValueError("REVISION_NOT_PREVIEWED")
        actual = RevisionStateFingerprintBuilder().build(db, project_id)
        if actual != revision.base_state_fingerprint:
            raise ValueError("REVISION_STALE")
        if revision.impact_report.get("author_override_required") and (not override or not (reason or "").strip()):
            raise ValueError("AUTHOR_OVERRIDE_REQUIRED")
        normalizer = RevisionChangeNormalizer()
        changes = normalizer.normalize(db, project_id, self._changes(revision))
        if [item["target_fingerprint_before"] for item in changes] != [item.get("target_fingerprint_before") for item in revision.normalized_changes]:
            raise ValueError("TARGET_STATE_STALE")
        candidates = self._candidates(db, project_id, self._changes(revision))
        for (target_type, _), candidate in candidates.items():
            normalizer._validate_target(candidate, target_type, db, project_id)
            normalizer._validate_references(candidate, db, project_id)
        return actual, changes, candidates

    def apply(self, db, project_id, revision, override, reason, prepared=None):
        if not db.scalar(select(Project).where(Project.id == project_id).with_for_update()):
            raise ValueError("REVISION_PROJECT_NOT_FOUND")
        actual, changes, candidates = prepared or self.preflight(db, project_id, revision, override, reason)
        pre = WorldSnapshotBuilder().create(db, project_id, SnapshotType.PRE_REVISION, revision.id)
        application = RevisionApplication(project_id=project_id, revision_id=revision.id, status=RevisionApplicationStatus.PENDING, pre_snapshot_id=pre.id, expected_base_fingerprint=revision.base_state_fingerprint, actual_base_fingerprint=actual, author_override=override, author_override_reason=reason, applied_change_count=0)
        db.add(application)
        db.flush()
        normalizer = RevisionChangeNormalizer()
        expected = {(item["target_type"], item["target_id"]): item["target_fingerprint_after"] for item in changes}
        for (target_type, target_id), candidate in candidates.items():
            target = db.get(self.MODELS[target_type], target_id)
            for field, value in candidate.items():
                if field not in self.IMMUTABLE:
                    setattr(target, field, value)
        db.flush()
        for (target_type, target_id), _ in candidates.items():
            target = db.get(self.MODELS[target_type], target_id)
            persisted = _record(target)
            normalizer._validate_target(persisted, target_type, db, project_id)
            normalizer._validate_references(persisted, db, project_id)
            if target_fingerprint(persisted) != expected[(target_type, target_id)]:
                raise ValueError("APPLY_RESULT_MISMATCH")
        post = WorldSnapshotBuilder().create(db, project_id, SnapshotType.POST_REVISION, revision.id)
        ProjectWorldSnapshotHeadService().update(
            db, project_id, post, source_type="REVISION_APPLY", source_id=revision.id
        )
        from .formal_state import FormalStateIdentityService
        FormalStateIdentityService().rebuild_and_anchor(db, project_id, source_type="REVISION_APPLY_V2", source_id=revision.id, project_locked=True)
        # Revision application can change structure-sensitive formal source.
        # The D2 projection is non-authoritative and must never certify it.
        from .narrative_structure_projection import NarrativeStructureProjectionService
        NarrativeStructureProjectionService().sync_after_history_change(
            db, project_id, 1, "REVISION_APPLY"
        )
        application.post_snapshot_id = post.id
        application.status = RevisionApplicationStatus.APPLIED
        application.applied_change_count = len(changes)
        application.completed_at = datetime.utcnow()
        revision.status = RevisionStatus.APPLIED
        return application

    def rollback(self, db, project_id, application):
        if not db.scalar(select(Project).where(Project.id == project_id).with_for_update()):
            raise ValueError("REVISION_PROJECT_NOT_FOUND")
        latest = db.scalar(select(RevisionApplication).where(RevisionApplication.project_id == project_id, RevisionApplication.status == RevisionApplicationStatus.APPLIED).order_by(RevisionApplication.completed_at.desc(), RevisionApplication.id.desc()))
        if not latest or latest.id != application.id:
            raise ValueError("ROLLBACK_NOT_LATEST")
        revision = db.get(WorldRevision, application.revision_id)
        pre = db.get(WorldSnapshot, application.pre_snapshot_id)
        post = db.get(WorldSnapshot, application.post_snapshot_id)
        resolver = SnapshotPayloadResolver()
        pre_payload = resolver.materialize(db, pre)
        post_payload = resolver.materialize(db, post)
        targets = {(item.target_type, item.target_id) for item in self._changes(revision)}
        for target_type, target_id in targets:
            current = _record(db.get(self.MODELS[target_type], target_id))
            expected = copy.deepcopy(next(item for item in post_payload[self.SNAPSHOT_KEYS[target_type]] if item["id"] == target_id))
            for field in ("created_at", "updated_at"):
                current.pop(field, None)
                expected.pop(field, None)
            if current != expected:
                raise ValueError("ROLLBACK_TARGET_STALE")
        normalizer = RevisionChangeNormalizer()
        for target_type, target_id in targets:
            target = db.get(self.MODELS[target_type], target_id)
            saved = next(item for item in pre_payload[self.SNAPSHOT_KEYS[target_type]] if item["id"] == target_id)
            for field, value in saved.items():
                if field not in self.IMMUTABLE:
                    setattr(target, field, value)
        db.flush()
        for target_type, target_id in targets:
            current = _record(db.get(self.MODELS[target_type], target_id))
            expected = copy.deepcopy(next(item for item in pre_payload[self.SNAPSHOT_KEYS[target_type]] if item["id"] == target_id))
            for field in ("created_at", "updated_at"):
                current.pop(field, None)
                expected.pop(field, None)
            normalizer._validate_target(_record(db.get(self.MODELS[target_type], target_id)), target_type, db, project_id)
            normalizer._validate_references(_record(db.get(self.MODELS[target_type], target_id)), db, project_id)
            if current != expected:
                raise ValueError("ROLLBACK_RESULT_MISMATCH")
        application.status = RevisionApplicationStatus.ROLLED_BACK
        application.completed_at = datetime.utcnow()
        revision.status = RevisionStatus.ROLLED_BACK
        rollback_snapshot = WorldSnapshotBuilder().create(db, project_id, SnapshotType.ROLLBACK_POINT, revision.id)
        ProjectWorldSnapshotHeadService().update(
            db, project_id, rollback_snapshot, source_type="REVISION_ROLLBACK", source_id=revision.id
        )
        from .formal_state import FormalStateIdentityService
        FormalStateIdentityService().rebuild_and_anchor(db, project_id, source_type="REVISION_ROLLBACK_V2", source_id=revision.id, project_locked=True)
        from .narrative_structure_projection import NarrativeStructureProjectionService
        NarrativeStructureProjectionService().sync_after_history_change(
            db, project_id, 1, "REVISION_ROLLBACK"
        )
        return application
