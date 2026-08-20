from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.formal_state import FormalStateIdentityAudit, FormalStateIdentityService, formal_world_state_v2_fingerprint
from app.models import CreationMode, EntityType, Project, WorldEntity


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def test_v2_fingerprint_is_order_and_history_independent():
    first = {
        "project": {"id": "project", "status": "DRAFT"},
        "world_entities": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
    }
    same_final_state = {
        "world_entities": [{"id": "b", "name": "B"}, {"id": "a", "name": "A"}],
        "project": {"status": "DRAFT", "id": "project"},
    }
    assert formal_world_state_v2_fingerprint(first) == formal_world_state_v2_fingerprint(same_final_state)


def test_incremental_leaf_sync_matches_explicit_rebuild():
    db = _session()
    project = Project(name="identity", status="DRAFT", creation_mode=CreationMode.AUTONOMOUS)
    db.add(project); db.commit()
    service = FormalStateIdentityService()
    service.rebuild(db, project.id)
    entity = WorldEntity(project_id=project.id, entity_type=EntityType.CUSTOM, name="door", profile={"locked": True})
    db.info["formal_state_sync_in_progress"] = True
    db.add(entity); db.flush()
    service.sync_manifest(db, project.id, {"project": False, "collections": {"world_entities": {entity.id}}})
    incremental = service.status(db, project.id)["state_fingerprint"]
    rebuilt = service.rebuild(db, project.id).state_fingerprint
    assert incremental == rebuilt
    FormalStateIdentityAudit().audit(db, project.id)


def test_direct_formal_write_marks_identity_dirty():
    db = _session()
    project = Project(name="identity", status="DRAFT", creation_mode=CreationMode.AUTONOMOUS)
    db.add(project); db.commit()
    service = FormalStateIdentityService(); service.rebuild(db, project.id); db.commit()
    db.add(WorldEntity(project_id=project.id, entity_type=EntityType.CUSTOM, name="untracked", profile={}))
    db.commit()
    assert service.status(db, project.id)["status"] == "DIRTY"
