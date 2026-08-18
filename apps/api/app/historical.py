from sqlalchemy import select
from .models import CharacterKnowledge, CharacterMemory, Scene, SceneStateCheckpoint, WorldSnapshot, SnapshotType
from .versioning import WorldSnapshotBuilder

class SceneStateCheckpointService:
    def capture(self, db, project_id, scene_id):
        scene = db.get(Scene, scene_id)
        if not scene or scene.project_id != project_id or scene.history_status != "ACTIVE":
            raise ValueError("SCENE_CHECKPOINT_TARGET_INVALID")
        payload, fingerprint = WorldSnapshotBuilder().build(db, project_id)
        pre = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.BASELINE, state_fingerprint=fingerprint, payload=payload)
        db.add(pre); db.flush()
        post = WorldSnapshot(project_id=project_id, snapshot_type=SnapshotType.BASELINE, state_fingerprint=fingerprint, payload=payload)
        db.add(post); db.flush()
        checkpoint = SceneStateCheckpoint(project_id=project_id, scene_id=scene.id, sequence=scene.sequence, pre_snapshot_id=pre.id, post_snapshot_id=post.id, current_scene_id=scene.id)
        db.add(checkpoint); db.flush(); return checkpoint

class ReplayBaselineBuilder:
    def build(self, db, project_id, scene_id):
        checkpoint = db.scalar(select(SceneStateCheckpoint).where(SceneStateCheckpoint.project_id == project_id, SceneStateCheckpoint.scene_id == scene_id))
        if not checkpoint: raise ValueError("HISTORICAL_BASELINE_UNAVAILABLE")
        snapshot = db.get(WorldSnapshot, checkpoint.pre_snapshot_id)
        if not snapshot: raise ValueError("HISTORICAL_BASELINE_UNAVAILABLE")
        return snapshot.payload, snapshot.state_fingerprint

class TemporalCharacterCognitionReader:
    def read(self, db, project_id, character_id, replay_session, sequence):
        payload = (replay_session.staged_world_state or {}).get("cognition", {})
        future_ids = {item.get("id") for item in payload.get(str(sequence), [])}
        invalidated = set(db.scalars(select(__import__("app.models", fromlist=["RetconCognitionInvalidation"]).RetconCognitionInvalidation.resource_id).where(__import__("app.models", fromlist=["RetconCognitionInvalidation"]).RetconCognitionInvalidation.project_id == project_id, __import__("app.models", fromlist=["RetconCognitionInvalidation"]).RetconCognitionInvalidation.character_id == character_id, __import__("app.models", fromlist=["RetconCognitionInvalidation"]).RetconCognitionInvalidation.status != "ROLLED_BACK")).all())
        knowledge = [row for row in db.scalars(select(CharacterKnowledge).where(CharacterKnowledge.character_id == character_id)).all() if row.id not in invalidated and row.id not in future_ids]
        memories = [row for row in db.scalars(select(CharacterMemory).where(CharacterMemory.character_id == character_id)).all() if row.id not in invalidated and row.id not in future_ids]
        return {"knowledge": knowledge, "memories": memories}
