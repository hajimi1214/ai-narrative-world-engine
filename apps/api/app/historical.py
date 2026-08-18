import copy
from sqlalchemy import select
from .models import CharacterKnowledge, CharacterMemory, Scene, SceneStateCheckpoint, WorldSnapshot, SnapshotType, RetconCognitionInvalidation
from .versioning import WorldSnapshotBuilder
from .revision import RevisionPatchEngine

class SceneStateCheckpointService:
    def capture_pre(self, db, project_id, scene_id):
        scene = db.get(Scene, scene_id)
        if not scene or scene.project_id != project_id or scene.history_status != "ACTIVE":
            raise ValueError("SCENE_CHECKPOINT_TARGET_INVALID")
        payload, fingerprint = WorldSnapshotBuilder().build(db, project_id)
        pre = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.BASELINE, state_fingerprint=fingerprint, payload=payload)
        db.add(pre); db.flush()
        return pre

    def finalize(self, db, project_id, scene_id, pre_snapshot_id):
        scene = db.get(Scene, scene_id)
        pre = db.get(WorldSnapshot, pre_snapshot_id)
        if not scene or scene.project_id != project_id or not pre or pre.project_id != project_id:
            raise ValueError("SCENE_CHECKPOINT_TARGET_INVALID")
        payload, fingerprint = WorldSnapshotBuilder().build(db, project_id)
        post = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.BASELINE, state_fingerprint=fingerprint, payload=payload)
        db.add(post); db.flush()
        checkpoint = SceneStateCheckpoint(project_id=project_id, scene_id=scene.id, sequence=scene.sequence, pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id, capture_protocol_version=2)
        db.add(checkpoint); db.flush(); return checkpoint

class ReplayBaselineBuilder:
    def build(self, db, project_id, scene_id, revision=None):
        checkpoint = db.scalar(select(SceneStateCheckpoint).where(SceneStateCheckpoint.project_id == project_id, SceneStateCheckpoint.scene_id == scene_id))
        if not checkpoint: raise ValueError("HISTORICAL_BASELINE_UNAVAILABLE")
        snapshot = db.get(WorldSnapshot, checkpoint.pre_snapshot_id)
        if not snapshot: raise ValueError("HISTORICAL_BASELINE_UNAVAILABLE")
        payload = copy.deepcopy(snapshot.payload)
        if revision is not None:
            models = {"CANON_FACT": "canon_facts", "WORLD_ENTITY": "world_entities", "CHARACTER": "characters"}
            changes = revision.normalized_changes or []
            engine = RevisionPatchEngine()
            for change in changes:
                rows = payload.get(models.get(change["target_type"], ""), [])
                target = next((row for row in rows if row.get("id") == change["target_id"]), None)
                if target is None: raise ValueError("HISTORICAL_BASELINE_TARGET_UNAVAILABLE")
                engine.apply(target, change["operation"], change["path"], change.get("after_value"))
        import hashlib, json
        fingerprint = "world-snapshot-v1:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        return payload, fingerprint

class TemporalCharacterCognitionReader:
    def read(self, db, project_id, character_id, replay_session, sequence):
        checkpoint = db.get(SceneStateCheckpoint, replay_session.baseline_snapshot_id)
        snapshot = db.get(WorldSnapshot, replay_session.baseline_snapshot_id)
        # baseline_snapshot_id points to the historical PRE snapshot; never query current cognition as authority.
        source = snapshot.payload if snapshot else {}
        invalidated = set(db.scalars(select(RetconCognitionInvalidation.resource_id).where(RetconCognitionInvalidation.project_id == project_id, RetconCognitionInvalidation.character_id == character_id, RetconCognitionInvalidation.status != "ROLLED_BACK")).all())
        dynamic = (replay_session.staged_world_state or {}).get("dynamic_cognition_invalidations", [])
        invalidated.update(item["resource_id"] for item in dynamic if item.get("character_id") == character_id and item.get("sequence", 0) < sequence)
        knowledge = [self._row(CharacterKnowledge, row) for row in source.get("character_knowledge", []) if row.get("character_id") == character_id and row.get("id") not in invalidated]
        memories = [self._row(CharacterMemory, row) for row in source.get("character_memories", []) if row.get("character_id") == character_id and row.get("id") not in invalidated]
        staged = (replay_session.staged_world_state or {}).get("staged_cognition", {})
        for item in staged.get("knowledge", []):
            if item.get("character_id") == character_id and item.get("source_sequence", 0) < sequence: knowledge.append(self._row(CharacterKnowledge, {"id": item["temp_id"], **item}))
        for item in staged.get("memories", []):
            if item.get("character_id") == character_id and item.get("source_sequence", 0) < sequence: memories.append(self._row(CharacterMemory, {"id": item["temp_id"], **item}))
        return {"knowledge": knowledge, "memories": memories}

    def _row(self, model, data):
        return type("TemporalRow", (), {key: value for key, value in data.items()})()
