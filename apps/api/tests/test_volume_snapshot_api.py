"""Snapshot history API resolves the sealed version, or latest when unsealed."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

import app.api as api_module
from app.db import Base
from app.main import app
from app.models import Project, VolumeContract, VolumeContractStatus, VolumeContinuitySnapshot


def test_snapshot_and_continuity_select_immutable_sealed_ref_then_latest_unsealed(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        project = Project(name="snapshot-api-isolated")
        db.add(project); db.flush()
        sealed = VolumeContract(project_id=project.id, volume_number=1, status=VolumeContractStatus.SEALED)
        unsealed = VolumeContract(project_id=project.id, volume_number=2, status=VolumeContractStatus.ACTIVE)
        db.add_all([sealed, unsealed]); db.flush()
        records = {}
        for volume, id_prefix in ((sealed, "sealed"), (unsealed, "open")):
            first = VolumeContinuitySnapshot(
                id=f"{id_prefix}-v1", project_id=project.id, volume_id=volume.id,
                snapshot_version=1, source_fingerprint=f"{id_prefix}-old",
                summary=f"{id_prefix} first", character_states={"hero": {"state": "old"}},
            )
            second = VolumeContinuitySnapshot(
                id=f"{id_prefix}-v2", project_id=project.id, volume_id=volume.id,
                snapshot_version=2, source_fingerprint=f"{id_prefix}-new",
                summary=f"{id_prefix} second", character_states={"hero": {"state": "new"}},
            )
            db.add_all([first, second]); records[volume.volume_number] = (volume, first, second)
        sealed.sealed_snapshot_id = records[1][1].id
        db.commit()
        project_id = project.id
        (sealed_id, open_id) = (records[1][0].id, records[2][0].id)

    monkeypatch.setattr(api_module, "SessionLocal", Session)
    client = TestClient(app)
    for volume_id, expected_version, expected_id in ((sealed_id, 1, "sealed-v1"), (open_id, 2, "open-v2")):
        base = f"/projects/{project_id}/volumes/{volume_id}"
        snapshot = client.get(f"{base}/snapshot")
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["snapshot_version"] == expected_version
        assert snapshot.json()["id"] == expected_id
        continuity = client.get(f"{base}/continuity")
        assert continuity.status_code == 200, continuity.text
        assert continuity.json()["snapshot"]["id"] == expected_id
    assert client.get("/projects/not-this-project/volumes/" + sealed_id + "/snapshot").status_code == 404
    client.close(); engine.dispose()
