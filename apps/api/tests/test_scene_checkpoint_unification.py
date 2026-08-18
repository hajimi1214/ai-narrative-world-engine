"""Independent Phase 7D semantic tests for pure checkpoint boundary logic."""
from types import SimpleNamespace
import pytest
from app.replay import PreservedSceneStateTransitionProjector, ReplayCheckpointStateBuilder
from app.historical import snapshot_fingerprint


def world():
    return {"project": {"id": "p", "current_world_time": "2040-01-01T00:00:00"}, "world_entities": [{"id": "door", "profile": {"color": "red", "locked": False}}], "characters": [{"id": "a", "inventory": []}], "character_knowledge": [], "character_memories": [], "canon_facts": [], "reveal_constraints": [], "story_threads": [], "story_arcs": [], "scenes": [], "chapters": []}

def project(pre, post, new=None, invalidated=()): return PreservedSceneStateTransitionProjector().project(pre, post, new or world(), invalidated)

def test_existing_nested_row_change_is_projected():
    old = world(); after = world(); after["world_entities"][0]["profile"]["locked"] = True; new = world(); assert project(old, after, new)["world_entities"][0]["profile"]["locked"] is True
def test_unrelated_replay_state_is_preserved():
    old = world(); after = world(); after["world_entities"][0]["profile"]["locked"] = True; new = world(); new["world_entities"][0]["profile"]["color"] = "blue"; out = project(old, after, new); assert out["world_entities"][0]["profile"] == {"color": "blue", "locked": True}
def test_added_scene_row_is_projected():
    old = world(); after = world(); after["scenes"] = [{"id": "s3", "status": "OCCURRED", "history_status": "ACTIVE"}]; assert project(old, after)["scenes"][0]["id"] == "s3"
def test_added_knowledge_row_is_projected():
    old = world(); after = world(); after["character_knowledge"] = [{"id": "k3", "source": "s3"}]; assert project(old, after)["character_knowledge"] == [{"id": "k3", "source": "s3"}]
def test_added_memory_row_is_projected():
    old = world(); after = world(); after["character_memories"] = [{"id": "m3", "source_scene": "s3"}]; assert project(old, after)["character_memories"] == [{"id": "m3", "source_scene": "s3"}]
def test_removed_row_is_removed():
    old = world(); old["characters"].append({"id": "removed"}); after = world(); assert not any(x["id"] == "removed" for x in project(old, after)["characters"])
def test_project_top_level_transition_is_projected():
    old = world(); after = world(); after["project"]["current_world_time"] = "2040-01-02T00:00:00"; assert project(old, after)["project"]["current_world_time"].endswith("02T00:00:00")
def test_project_unrelated_value_survives():
    old = world(); after = world(); after["project"]["current_world_time"] = "later"; new = world(); new["project"]["id"] = "p2"; assert project(old, after, new)["project"]["id"] == "p2"
def test_invalidated_added_knowledge_is_filtered():
    old = world(); after = world(); after["character_knowledge"] = [{"id": "k3"}]; assert not project(old, after, invalidated=("k3",))["character_knowledge"]
def test_invalidated_added_memory_is_filtered():
    old = world(); after = world(); after["character_memories"] = [{"id": "m3"}]; assert not project(old, after, invalidated=("m3",))["character_memories"]
def test_projection_does_not_mutate_inputs():
    old = world(); after = world(); new = world(); project(old, after, new); assert old["project"]["id"] == new["project"]["id"] == "p"
def test_projection_is_deterministic():
    old = world(); after = world(); after["world_entities"].append({"id": "z"}); assert project(old, after) == project(old, after)
def test_projection_preserves_canon_rows_not_touched():
    old = world(); old["canon_facts"] = [{"id": "c", "proposition": "x"}]; after = world(); after["canon_facts"] = old["canon_facts"]; new = world(); new["canon_facts"] = [{"id": "c", "proposition": "retcon"}]; assert project(old, after, new)["canon_facts"][0]["proposition"] == "retcon"
def test_projection_preserves_entity_added_by_replay():
    old = world(); after = world(); new = world(); new["world_entities"].append({"id": "replay"}); assert any(x["id"] == "replay" for x in project(old, after, new)["world_entities"])
def test_projection_handles_empty_tables():
    assert project({"project": {}}, {"project": {}}, {"project": {}, "scenes": []})["scenes"] == []

def test_checkpoint_payload_has_canonical_keys():
    session = SimpleNamespace(staged_world_state={"current_world": world(), "staged_facts": [], "staged_cognition": {}}); assert set(ReplayCheckpointStateBuilder().build(session)) == set(ReplayCheckpointStateBuilder.KEYS)
def test_checkpoint_fact_overlay_updates_entity_profile():
    session = SimpleNamespace(staged_world_state={"current_world": world(), "staged_facts": [{"subject_type": "ENTITY", "subject_id": "door", "predicate": "locked", "value": True}], "staged_cognition": {}}); assert ReplayCheckpointStateBuilder().build(session)["world_entities"][0]["profile"]["locked"] is True
def test_checkpoint_ignores_internal_queue():
    session = SimpleNamespace(staged_world_state={"current_world": world(), "queue": ["secret"], "cursor": 4}); assert "queue" not in ReplayCheckpointStateBuilder().build(session)
def test_checkpoint_staged_knowledge_uses_temp_id():
    session = SimpleNamespace(staged_world_state={"current_world": world(), "staged_cognition": {"knowledge": [{"temp_id": "tmp-k", "character_id": "a"}]}}); assert ReplayCheckpointStateBuilder().build(session)["character_knowledge"][0]["id"] == "tmp-k"
def test_checkpoint_staged_memory_uses_temp_id():
    session = SimpleNamespace(staged_world_state={"current_world": world(), "staged_cognition": {"memories": [{"temp_id": "tmp-m", "character_id": "a"}]}}); assert ReplayCheckpointStateBuilder().build(session)["character_memories"][0]["id"] == "tmp-m"
def test_checkpoint_missing_world_uses_schema_defaults():
    session = SimpleNamespace(staged_world_state={}); payload = ReplayCheckpointStateBuilder().build(session); assert payload["scenes"] == [] and payload["project"] == {}
def test_checkpoint_builder_is_pure():
    state = {"current_world": world(), "staged_facts": []}; session = SimpleNamespace(staged_world_state=state); ReplayCheckpointStateBuilder().build(session); assert state == session.staged_world_state
def test_snapshot_fingerprint_ignores_mapping_order():
    assert snapshot_fingerprint({"b": 2, "a": 1}) == snapshot_fingerprint({"a": 1, "b": 2})
def test_snapshot_fingerprint_changes_state():
    assert snapshot_fingerprint({"a": 1}) != snapshot_fingerprint({"a": 2})
def test_snapshot_fingerprint_is_string():
    assert snapshot_fingerprint({"a": 1}).startswith("world-snapshot-v1:")

@pytest.mark.parametrize("predicate,value", [("locked", True), ("opened", True), ("color", "blue"), ("count", 2), ("security", {"alarm": False})])
def test_checkpoint_fact_overlay_preserves_structured_value(predicate, value):
    session = SimpleNamespace(staged_world_state={"current_world": world(), "staged_facts": [{"subject_type": "ENTITY", "subject_id": "door", "predicate": predicate, "value": value}], "staged_cognition": {}})
    assert ReplayCheckpointStateBuilder().build(session)["world_entities"][0]["profile"][predicate] == value
