"""Versioned scene-state boundaries and legacy historical helpers."""
from __future__ import annotations
import copy, hashlib, json
from typing import Any
from sqlalchemy import func, select
from .models import CharacterKnowledge, CharacterMemory, Scene, SceneCheckpointOrigin, SceneCommit, SceneStateCheckpoint, SnapshotType, WorldSnapshot, RetconCognitionInvalidation
from .revision import RevisionPatchEngine
from .versioning import WorldSnapshotBuilder

def snapshot_fingerprint(payload: dict[str, Any]) -> str:
    return "world-snapshot-v1:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

def checkpoint_fingerprint(checkpoint, scene, pre, post):
    value = {"project_id": checkpoint.project_id, "scene_id": checkpoint.scene_id, "sequence": scene.sequence, "version": checkpoint.version, "origin": getattr(checkpoint.origin, "value", checkpoint.origin), "pre": pre.state_fingerprint, "post": post.state_fingerprint, "source_scene_commit_id": checkpoint.source_scene_commit_id, "source_replay_session_id": checkpoint.source_replay_session_id, "supersedes_checkpoint_id": checkpoint.supersedes_checkpoint_id, "capture_protocol_version": checkpoint.capture_protocol_version}
    return "scene-checkpoint-v3:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

class CurrentSceneCheckpointResolver:
    def current(self, db, project_id, scene_id):
        rows = db.scalars(select(SceneStateCheckpoint).where(SceneStateCheckpoint.project_id == project_id, SceneStateCheckpoint.scene_id == scene_id, SceneStateCheckpoint.active.is_(True)).order_by(SceneStateCheckpoint.version.desc(), SceneStateCheckpoint.id.desc())).all()
        if len(rows) > 1: raise ValueError("SCENE_CHECKPOINT_CURRENT_AMBIGUOUS")
        if not rows: raise ValueError("SCENE_CHECKPOINT_MISSING")
        return rows[0]
    def history(self, db, project_id, scene_id):
        return db.scalars(select(SceneStateCheckpoint).where(SceneStateCheckpoint.project_id == project_id, SceneStateCheckpoint.scene_id == scene_id).order_by(SceneStateCheckpoint.version.desc(), SceneStateCheckpoint.id.desc())).all()

class SceneCheckpointIntegrityValidator:
    def validate_integrity(self, db, checkpoint):
        scene = db.get(Scene, checkpoint.scene_id); pre = db.get(WorldSnapshot, checkpoint.pre_snapshot_id); post = db.get(WorldSnapshot, checkpoint.post_snapshot_id)
        if not scene or scene.project_id != checkpoint.project_id or checkpoint.sequence != scene.sequence or checkpoint.current_scene_id != scene.id: raise ValueError("SCENE_CHECKPOINT_LINEAGE_INVALID")
        if not pre or not post or pre.project_id != checkpoint.project_id or post.project_id != checkpoint.project_id: raise ValueError("SCENE_CHECKPOINT_LINEAGE_INVALID")
        if snapshot_fingerprint(pre.payload) != pre.state_fingerprint or snapshot_fingerprint(post.payload) != post.state_fingerprint: raise ValueError("SCENE_CHECKPOINT_SNAPSHOT_FINGERPRINT_INVALID")
        if checkpoint.capture_protocol_version < 3: return
        if pre.snapshot_type != SnapshotType.PRE_SCENE_STATE or post.snapshot_type != SnapshotType.POST_SCENE_STATE: raise ValueError("SCENE_CHECKPOINT_LINEAGE_INVALID")
        if checkpoint.pre_state_fingerprint != pre.state_fingerprint or checkpoint.post_state_fingerprint != post.state_fingerprint: raise ValueError("SCENE_CHECKPOINT_SNAPSHOT_FINGERPRINT_INVALID")
        if checkpoint.checkpoint_fingerprint != checkpoint_fingerprint(checkpoint, scene, pre, post): raise ValueError("SCENE_CHECKPOINT_FINGERPRINT_INVALID")
        origin = getattr(checkpoint.origin, "value", checkpoint.origin)
        if origin == "NORMAL_COMMIT":
            commit = db.get(SceneCommit, checkpoint.source_scene_commit_id) if checkpoint.source_scene_commit_id else None
            if not commit or checkpoint.source_replay_session_id or commit.project_id != checkpoint.project_id or commit.scene_id != scene.id or commit.checkpoint_id != checkpoint.id or getattr(commit.status, "value", commit.status) != "COMMITTED": raise ValueError("SCENE_CHECKPOINT_PROVENANCE_INVALID")
        elif origin == "REPLAY_COMMIT":
            from .models import RetconReplaySession
            replay = db.get(RetconReplaySession, checkpoint.source_replay_session_id) if checkpoint.source_replay_session_id else None
            if not replay or checkpoint.source_scene_commit_id or replay.project_id != checkpoint.project_id or getattr(replay.status, "value", replay.status) != "COMPLETED": raise ValueError("SCENE_CHECKPOINT_PROVENANCE_INVALID")
        else: raise ValueError("SCENE_CHECKPOINT_PROVENANCE_INVALID")
        before = next((row for row in pre.payload.get("scenes", []) if row.get("id") == scene.id and row.get("history_status") == "ACTIVE"), None)
        after = next((row for row in post.payload.get("scenes", []) if row.get("id") == scene.id), None)
        # A replay replacement is a new Scene identity and must not appear in
        # its PRE.  A preserved Scene deliberately keeps its identity while
        # receiving a new boundary version, so its historical row may appear.
        if before:
            if origin == "NORMAL_COMMIT":
                raise ValueError("SCENE_CHECKPOINT_LINEAGE_INVALID")
            from .models import ReplaySceneRun
            preserved = db.scalar(select(ReplaySceneRun).where(
                ReplaySceneRun.replay_session_id == checkpoint.source_replay_session_id,
                ReplaySceneRun.original_scene_id == scene.id,
                ReplaySceneRun.mode == "VALIDATE_PRESERVED",
            ))
            if not preserved:
                raise ValueError("SCENE_CHECKPOINT_LINEAGE_INVALID")
        if not after or after.get("status") != "OCCURRED" or after.get("history_status") != "ACTIVE": raise ValueError("SCENE_CHECKPOINT_LINEAGE_INVALID")

class SceneCheckpointService:
    resolver = CurrentSceneCheckpointResolver(); validator = SceneCheckpointIntegrityValidator()
    def capture_formal_pre(self, db, project_id): return WorldSnapshotBuilder().create(db, project_id, SnapshotType.PRE_SCENE_STATE)
    def finalize_formal_post(self, db, project_id): return WorldSnapshotBuilder().create(db, project_id, SnapshotType.POST_SCENE_STATE)
    def materialize_from_payloads(self, db, project_id, scene, pre_payload, post_payload, *, origin, source_scene_commit_id=None, source_replay_session_id=None):
        pre = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.PRE_SCENE_STATE, state_fingerprint=snapshot_fingerprint(pre_payload), payload=copy.deepcopy(pre_payload)); post = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.POST_SCENE_STATE, state_fingerprint=snapshot_fingerprint(post_payload), payload=copy.deepcopy(post_payload)); db.add_all([pre, post]); db.flush(); return self._create(db, project_id, scene, pre, post, origin=origin, source_scene_commit_id=source_scene_commit_id, source_replay_session_id=source_replay_session_id)
    def create_from_snapshots(self, db, project_id, scene, pre, post, *, origin, source_scene_commit_id=None, source_replay_session_id=None): return self._create(db, project_id, scene, pre, post, origin=origin, source_scene_commit_id=source_scene_commit_id, source_replay_session_id=source_replay_session_id)
    def _create(self, db, project_id, scene, pre, post, *, origin, source_scene_commit_id, source_replay_session_id):
        if scene.project_id != project_id or pre.project_id != project_id or post.project_id != project_id: raise ValueError("SCENE_CHECKPOINT_LINEAGE_INVALID")
        active = db.scalars(select(SceneStateCheckpoint).where(SceneStateCheckpoint.project_id == project_id, SceneStateCheckpoint.scene_id == scene.id, SceneStateCheckpoint.active.is_(True))).all()
        if len(active) > 1: raise ValueError("SCENE_CHECKPOINT_CURRENT_AMBIGUOUS")
        prior = active[0] if active else None; version = (db.scalar(select(func.max(SceneStateCheckpoint.version)).where(SceneStateCheckpoint.project_id == project_id, SceneStateCheckpoint.scene_id == scene.id)) or 0) + 1
        # PostgreSQL enforces the partial unique index immediately; flush the
        # inactive lifecycle change before inserting the next active version.
        if prior:
            prior.active = False
            db.flush()
        checkpoint = SceneStateCheckpoint(project_id=project_id, scene_id=scene.id, sequence=scene.sequence, pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id, capture_protocol_version=3, version=version, active=True, origin=getattr(origin, "value", origin), source_scene_commit_id=source_scene_commit_id, source_replay_session_id=source_replay_session_id, supersedes_checkpoint_id=prior.id if prior else None, pre_state_fingerprint=pre.state_fingerprint, post_state_fingerprint=post.state_fingerprint)
        db.add(checkpoint); db.flush(); checkpoint.checkpoint_fingerprint = checkpoint_fingerprint(checkpoint, scene, pre, post); db.flush(); return checkpoint
    def current(self, db, project_id, scene_id): return self.resolver.current(db, project_id, scene_id)
    def history(self, db, project_id, scene_id): return self.resolver.history(db, project_id, scene_id)
    def validate_integrity(self, db, checkpoint): return self.validator.validate_integrity(db, checkpoint)

class CurrentHistoryCheckpointAudit:
    def audit(self, db, project_id):
        for scene in db.scalars(select(Scene).where(Scene.project_id == project_id, Scene.status == "OCCURRED", Scene.history_status == "ACTIVE")).all():
            checkpoint = CurrentSceneCheckpointResolver().current(db, project_id, scene.id); SceneCheckpointIntegrityValidator().validate_integrity(db, checkpoint)

class SceneStateCheckpointService:
    """Protocol-v2 setup compatibility; not a production write path."""
    def capture_pre(self, db, project_id, scene_id):
        scene = db.get(Scene, scene_id)
        if not scene or scene.project_id != project_id or scene.history_status != "ACTIVE": raise ValueError("SCENE_CHECKPOINT_TARGET_INVALID")
        payload, fingerprint = WorldSnapshotBuilder().build(db, project_id); pre = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.BASELINE, state_fingerprint=fingerprint, payload=payload); db.add(pre); db.flush(); return pre
    def finalize(self, db, project_id, scene_id, pre_snapshot_id):
        scene = db.get(Scene, scene_id); pre = db.get(WorldSnapshot, pre_snapshot_id)
        if not scene or scene.project_id != project_id or not pre or pre.project_id != project_id: raise ValueError("SCENE_CHECKPOINT_TARGET_INVALID")
        payload, fingerprint = WorldSnapshotBuilder().build(db, project_id); post = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.BASELINE, state_fingerprint=fingerprint, payload=payload); db.add(post); db.flush(); checkpoint = SceneStateCheckpoint(project_id=project_id, scene_id=scene.id, sequence=scene.sequence, pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id, capture_protocol_version=2, version=1, active=True, origin=SceneCheckpointOrigin.LEGACY.value); db.add(checkpoint); db.flush(); return checkpoint

class ReplayBaselineBuilder:
    def build(self, db, project_id, scene_id, revision=None):
        try: checkpoint = CurrentSceneCheckpointResolver().current(db, project_id, scene_id)
        except ValueError as exc: raise ValueError("HISTORICAL_BASELINE_UNAVAILABLE") from exc
        if checkpoint.capture_protocol_version < 2: raise ValueError("HISTORICAL_BASELINE_UNAVAILABLE")
        if checkpoint.capture_protocol_version >= 3:
            try: SceneCheckpointIntegrityValidator().validate_integrity(db, checkpoint)
            except ValueError as exc: raise ValueError("SCENE_CHECKPOINT_INTEGRITY_INVALID") from exc
        snapshot = db.get(WorldSnapshot, checkpoint.pre_snapshot_id)
        if not snapshot: raise ValueError("HISTORICAL_BASELINE_UNAVAILABLE")
        payload = copy.deepcopy(snapshot.payload)
        if revision is not None:
            models = {"CANON_FACT": "canon_facts", "WORLD_ENTITY": "world_entities", "CHARACTER": "characters"}; engine = RevisionPatchEngine()
            for change in revision.normalized_changes or []:
                target = next((row for row in payload.get(models.get(change["target_type"], ""), []) if row.get("id") == change["target_id"]), None)
                if target is None: raise ValueError("HISTORICAL_BASELINE_TARGET_UNAVAILABLE")
                engine.apply(target, change["operation"], change["path"], change.get("after_value"))
        return payload, snapshot_fingerprint(payload)

class TemporalCharacterCognitionReader:
    def read(self, db, project_id, character_id, replay_session, sequence):
        snapshot = db.get(WorldSnapshot, replay_session.baseline_snapshot_id); source = snapshot.payload if snapshot else {}
        invalidated = set(db.scalars(select(RetconCognitionInvalidation.resource_id).where(RetconCognitionInvalidation.project_id == project_id, RetconCognitionInvalidation.character_id == character_id, RetconCognitionInvalidation.status != "ROLLED_BACK")).all()); dynamic = (replay_session.staged_world_state or {}).get("dynamic_cognition_invalidations", []); invalidated.update(item["resource_id"] for item in dynamic if item.get("character_id") == character_id and item.get("sequence", 0) < sequence)
        knowledge = [self._row(CharacterKnowledge, row) for row in source.get("character_knowledge", []) if row.get("character_id") == character_id and row.get("id") not in invalidated]; memories = [self._row(CharacterMemory, row) for row in source.get("character_memories", []) if row.get("character_id") == character_id and row.get("id") not in invalidated]
        staged = (replay_session.staged_world_state or {}).get("staged_cognition", {})
        for item in staged.get("knowledge", []):
            if item.get("character_id") == character_id and item.get("source_sequence", 0) < sequence: knowledge.append(self._row(CharacterKnowledge, {"id": item["temp_id"], **item}))
        for item in staged.get("memories", []):
            if item.get("character_id") == character_id and item.get("source_sequence", 0) < sequence: memories.append(self._row(CharacterMemory, {"id": item["temp_id"], **item}))
        return {"knowledge": knowledge, "memories": memories}
    def _row(self, model, data): return type("TemporalRow", (), {key: value for key, value in data.items()})()
