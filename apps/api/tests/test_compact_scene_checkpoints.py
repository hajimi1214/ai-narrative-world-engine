"""Phase 16B compact snapshot storage contracts."""
from __future__ import annotations

import copy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Project, SnapshotStorageMode, SnapshotType, WorldSnapshot
from app.snapshot_storage import (
    CompactSnapshotAudit,
    CompactSnapshotService,
    ProjectWorldSnapshotHeadService,
    SnapshotDeltaCodec,
    SnapshotPayloadResolver,
    snapshot_fingerprint,
)
from app.versioning import WorldSnapshotBuilder


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        yield db


def _payload(project_id: str, value: int = 0) -> dict:
    return {
        "project": {"id": project_id, "status": "DRAFT", "current_world_time": None},
        "canon_facts": [],
        "world_entities": [{"id": "world-1", "project_id": project_id, "profile": {"value": value}}],
        "characters": [], "character_knowledge": [], "character_memories": [],
        "reveal_constraints": [], "story_threads": [], "story_arcs": [],
        "scenes": [], "chapters": [],
    }


def _project(db, name="Compact"):
    row = Project(name=name)
    db.add(row)
    db.flush()
    return row


def test_legacy_anchor_reference_delta_and_delete_materialize_exactly(session):
    project = _project(session)
    base = _payload(project.id)
    legacy = WorldSnapshot(
        project_id=project.id,
        snapshot_type=SnapshotType.BASELINE,
        payload=copy.deepcopy(base),
        state_fingerprint=snapshot_fingerprint(base),
    )
    session.add(legacy)
    session.flush()
    service = CompactSnapshotService()
    anchor = service.anchor(session, project.id, SnapshotType.PRE_SCENE_STATE, base)
    reference = service.reference(session, project.id, SnapshotType.PRE_SCENE_STATE, anchor)
    post = copy.deepcopy(base)
    post["world_entities"][0]["profile"]["value"] = 2
    post["scenes"].append({"id": "scene-1", "project_id": project.id, "sequence": 1})
    delta = service.delta(
        session, project.id, SnapshotType.POST_SCENE_STATE, reference,
        SnapshotDeltaCodec().diff(base, post), snapshot_fingerprint(post),
    )
    deleted = copy.deepcopy(post)
    deleted["world_entities"] = []
    deleted_delta = service.delta(
        session, project.id, SnapshotType.POST_SCENE_STATE, delta,
        SnapshotDeltaCodec().diff(post, deleted), snapshot_fingerprint(deleted),
    )
    resolver = SnapshotPayloadResolver()
    assert resolver.materialize(session, legacy) == base
    assert resolver.materialize(session, reference) == base
    assert resolver.materialize(session, delta) == post
    assert resolver.materialize(session, deleted_delta) == deleted
    assert anchor.storage_mode == SnapshotStorageMode.COMPACT_ANCHOR
    assert reference.storage_mode == SnapshotStorageMode.REFERENCE
    assert delta.storage_mode == SnapshotStorageMode.COMPACT_DELTA


def test_resolver_is_iterative_at_ten_thousand_reference_depth(session):
    project = _project(session)
    payload = _payload(project.id)
    service = CompactSnapshotService()
    node = service.anchor(session, project.id, SnapshotType.BASELINE, payload)
    for _ in range(10_000):
        node = service.reference(session, project.id, SnapshotType.PRE_SCENE_STATE, node)
    assert node.materialization_depth == 10_000
    assert SnapshotPayloadResolver().materialize(session, node) == payload


def test_resolver_fails_closed_for_missing_cycle_cross_project_and_storage_tamper(session):
    one, two = _project(session, "One"), _project(session, "Two")
    service = CompactSnapshotService()
    base = service.anchor(session, one.id, SnapshotType.BASELINE, _payload(one.id))
    reference = service.reference(session, one.id, SnapshotType.PRE_SCENE_STATE, base)
    reference.base_snapshot_id = "missing"
    with pytest.raises(ValueError, match="SNAPSHOT_BASE_MISSING"):
        SnapshotPayloadResolver().materialize(session, reference)
    reference.base_snapshot_id = base.id
    reference.storage_fingerprint = service.codec.fingerprint(
        project_id=one.id, mode=reference.storage_mode, base_snapshot_id=base.id,
        base_state_fingerprint=base.state_fingerprint, payload=reference.payload,
        schema_version=reference.schema_version,
    )
    base.base_snapshot_id = reference.id
    base.storage_mode = SnapshotStorageMode.REFERENCE
    base.payload = {"protocol": "compact-world-snapshot-v1", "kind": "reference", "base_snapshot_id": reference.id}
    base.materialization_depth = 2
    base.storage_fingerprint = service.codec.fingerprint(
        project_id=one.id, mode=base.storage_mode, base_snapshot_id=reference.id,
        base_state_fingerprint=reference.state_fingerprint, payload=base.payload,
        schema_version=base.schema_version,
    )
    with pytest.raises(ValueError, match="SNAPSHOT_CHAIN_INVALID"):
        SnapshotPayloadResolver().materialize(session, reference)
    foreign = service.anchor(session, two.id, SnapshotType.BASELINE, _payload(two.id))
    reference.base_snapshot_id = foreign.id
    reference.storage_fingerprint = service.codec.fingerprint(
        project_id=one.id, mode=reference.storage_mode, base_snapshot_id=foreign.id,
        base_state_fingerprint=foreign.state_fingerprint, payload=reference.payload,
        schema_version=reference.schema_version,
    )
    with pytest.raises(ValueError, match="SNAPSHOT_CHAIN_INVALID"):
        SnapshotPayloadResolver().validate_chain(session, reference)


def test_explicit_audit_compares_head_materialization_to_formal_db(session):
    project = _project(session)
    payload, fingerprint = WorldSnapshotBuilder().build(session, project.id)
    service = CompactSnapshotService()
    anchor = service.anchor(session, project.id, SnapshotType.BASELINE, payload, fingerprint)
    ProjectWorldSnapshotHeadService().update(session, project.id, anchor, source_type="TEST")
    assert CompactSnapshotAudit().audit_current_formal_state(session, project.id) == payload
    anchor.payload["project"]["status"] = "TAMPERED"
    with pytest.raises(ValueError, match="SNAPSHOT_CHAIN_INVALID"):
        CompactSnapshotAudit().audit_snapshot(session, anchor)
