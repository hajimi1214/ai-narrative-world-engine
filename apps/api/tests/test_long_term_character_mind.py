"""Phase 9 deterministic mind retrieval and explicit cognition provenance."""
import json

import pytest
from sqlalchemy import select

from app.character_mind import (
    ActiveCharacterCognitionReader,
    ActorPerceptionSanitizer,
    CharacterBeliefViewBuilder,
    CharacterContextBuilder,
    CharacterDecisionConstraintChecker,
    CharacterMemoryRetriever,
    CharacterMindViewBuilder,
    CognitionFactIdentityParser,
    HeuristicCharacterActor,
    MAX_CHARACTER_KNOWLEDGE,
    MAX_CHARACTER_MEMORIES,
    StructuredActorCueExtractor,
)
from app.llm_actor import CharacterDecisionPayload
from app.llm_actor import LLMCharacterActor
from app.ai.fake import FakeModelProvider
from app.models import (
    CausalEdgeKind, CausalLink, CausalRelationType, CausalResourceType,
    CanonFact, CanonType, CharacterDecision, CharacterKnowledge, CharacterMemory, KnowledgeStatus,
    Scene, SceneStatus,
)
from test_character_mind import client_for, context, decision, seed, session


def knowledge(session, actor, proposition, status=KnowledgeStatus.KNOWN, confidence=1.0):
    row = CharacterKnowledge(character_id=actor.id, proposition=proposition, status=status, confidence=confidence)
    session.add(row); session.commit(); return row


def memory(session, actor, content, **values):
    row = CharacterMemory(character_id=actor.id, content=content, importance=values.get("importance", 0.5), emotional_weight=values.get("emotional_weight", 0), confidence=values.get("confidence", 1), distortion=values.get("distortion", {}), source_scene=values.get("source_scene"))
    session.add(row); session.commit(); return row


def mind(session, project, actor, proposal):
    return CharacterMindViewBuilder().build(session, project.id, actor.id, proposal)


def current_scene(session, project, sequence, location, participants, threads=()):
    row = Scene(project_id=project.id, sequence=sequence, location=location, participants=list(participants), story_threads=list(threads), status=SceneStatus.OCCURRED, history_status="ACTIVE")
    session.add(row); session.commit(); return row


def use_link(session, project, resource_type, resource_id, relation, sequence, active=True):
    row = CausalLink(project_id=project.id, cause_type=resource_type, cause_id=resource_id, effect_type=CausalResourceType.CHARACTER_DECISION, effect_id=f"decision-{resource_id}-{sequence}-{active}", edge_kind=CausalEdgeKind.CAUSAL, relation_type=relation, scene_id=None, sequence=sequence, evidence={}, active=active, source_key=f"mind:{resource_id}:{sequence}:{active}:{relation.value}", link_fingerprint=f"fingerprint:{resource_id}:{sequence}:{active}")
    session.add(row); session.commit(); return row


def recalled_ids(view, key):
    field = "knowledge_id" if key == "knowledge" else "memory_id"
    return {row[field] for row in view[key]}


def invalidation_application(session, project):
    from app.models import RetconApplication, RetconApplicationStatus
    row = RetconApplication(project_id=project.id, retcon_request_id="test-request", retcon_plan_id="test-plan", source_revision_id="test-revision", status=RetconApplicationStatus.APPLIED_PENDING_REPLAY, plan_basis_fingerprint="test", pre_apply_world_fingerprint="test")
    session.add(row); session.commit(); return row


def test_parser_reads_canonical_boolean():
    assert CognitionFactIdentityParser().parse("ENTITY door: opened = true") == {"subject_type": "ENTITY", "subject_id": "door", "predicate": "opened", "value": True}


def test_parser_reads_canonical_string():
    assert CognitionFactIdentityParser().parse('ENTITY door: color = "red"')["value"] == "red"


def test_parser_reads_canonical_container():
    assert CognitionFactIdentityParser().parse('ENTITY door: marks = {"a": 1}')["value"] == {"a": 1}


def test_parser_rejects_legacy_prose_without_guessing():
    assert CognitionFactIdentityParser().parse("The door is probably open") is None


def test_cue_extractor_uses_explicit_ids_only(session):
    project, location, actor, other, _, proposal = seed(session)
    proposal.entry_state["visible_context"] = {"entity_ids": ["door"], "scene_goal": f"Find {other.id}", "free_text": location.id}
    assert set(StructuredActorCueExtractor().extract(proposal, actor.id)["entity_ids"]) == {location.id, "door"}
    assert other.id in StructuredActorCueExtractor().extract(proposal, actor.id)["participant_ids"]


def test_cue_extractor_reads_actor_private_structured_ids(session):
    project, _, actor, _, _, proposal = seed(session)
    proposal.entry_state = {"actor_visible_context": {actor.id: {"item_id": "key-1", "thread_ids": ["thread-x"]}}}
    cues = StructuredActorCueExtractor().extract(proposal, actor.id)
    assert cues["item_ids"] == ("key-1",) and cues["thread_ids"] == (proposal.primary_thread_id, "thread-x")


def test_belief_view_preserves_false_belief(session):
    project, _, actor, _, _, proposal = seed(session)
    row = knowledge(session, actor, "ENTITY door: locked = true", KnowledgeStatus.FALSE_BELIEF)
    view = mind(session, project, actor, proposal)
    assert next(item for item in view["knowledge"] if item["knowledge_id"] == row.id)["status"] == "FALSE_BELIEF"


def test_belief_view_reports_conflicting_fact_values(session):
    project, _, actor, _, _, proposal = seed(session)
    left = knowledge(session, actor, "ENTITY door: locked = true", KnowledgeStatus.FALSE_BELIEF)
    right = knowledge(session, actor, "ENTITY door: locked = false", KnowledgeStatus.SUSPECTED)
    view = mind(session, project, actor, proposal)
    assert view["belief_conflicts"] == [{"subject_type": "ENTITY", "subject_id": "door", "predicate": "locked", "knowledge_ids": sorted([left.id, right.id])}]


def test_knowledge_budget_is_bounded(session):
    project, _, actor, _, _, proposal = seed(session)
    for index in range(MAX_CHARACTER_KNOWLEDGE + 4): knowledge(session, actor, f"ENTITY e{index}: open = true")
    assert len(mind(session, project, actor, proposal)["knowledge"]) == MAX_CHARACTER_KNOWLEDGE


def test_high_cue_false_belief_can_be_recalled(session):
    project, location, actor, _, _, proposal = seed(session)
    row = knowledge(session, actor, f"ENTITY {location.id}: locked = true", KnowledgeStatus.FALSE_BELIEF)
    assert row.id in recalled_ids(mind(session, project, actor, proposal), "knowledge")


def test_memory_budget_is_bounded(session):
    project, _, actor, _, _, proposal = seed(session)
    for index in range(MAX_CHARACTER_MEMORIES + 4): memory(session, actor, f"memory {index}", importance=index)
    assert len(mind(session, project, actor, proposal)["memories"]) == MAX_CHARACTER_MEMORIES


def test_source_scene_location_is_memory_cue_fallback(session):
    project, location, actor, other, _, proposal = seed(session)
    scene = current_scene(session, project, 1, "unrelated-location", [])
    row = memory(session, actor, "archive memory", importance=0, source_scene=scene.id)
    assert mind(session, project, actor, proposal)["memories"][0]["memory_id"] == row.id


def test_memory_distortion_is_preserved_subjectively(session):
    project, _, actor, _, _, proposal = seed(session)
    row = memory(session, actor, "wrong recollection", distortion={"false_memory": True})
    assert next(item for item in mind(session, project, actor, proposal)["memories"] if item["memory_id"] == row.id)["distortion"] == {"false_memory": True}


def test_memory_diversity_limits_same_source_scene(session):
    project, location, actor, other, _, proposal = seed(session)
    scene = current_scene(session, project, 1, "unrelated-location", [])
    for index in range(5): memory(session, actor, f"same {index}", importance=10, source_scene=scene.id)
    other_scene = current_scene(session, project, 2, "other", [actor.id])
    for index in range(10): memory(session, actor, f"other {index}", importance=9, source_scene=other_scene.id)
    selected = mind(session, project, actor, proposal)["memories"]
    assert sum(item["source_scene_id"] == scene.id for item in selected) <= 3


def test_strong_cue_can_exceed_memory_diversity_cap(session):
    project, location, actor, _, _, proposal = seed(session)
    for index in range(4): memory(session, actor, f"strong {index}", importance=10, distortion={"location_id": location.id}, source_scene="shared")
    assert len(mind(session, project, actor, proposal)["memories"]) == 4


def test_active_cognition_reader_hides_invalidated_knowledge(session):
    from app.models import RetconCognitionInvalidation, RetconCognitionInvalidationStatus
    project, _, actor, _, _, proposal = seed(session); row = knowledge(session, actor, "ENTITY door: opened = true"); application = invalidation_application(session, project)
    session.add(RetconCognitionInvalidation(project_id=project.id, retcon_application_id=application.id, character_id=actor.id, resource_type="KNOWLEDGE", resource_id=row.id, status=RetconCognitionInvalidationStatus.ACTIVE, reason="test", original_semantic_fingerprint="f")); session.commit()
    assert row.id not in recalled_ids(mind(session, project, actor, proposal), "knowledge")


def test_active_cognition_reader_hides_invalidated_memory(session):
    from app.models import RetconCognitionInvalidation, RetconCognitionInvalidationStatus
    project, _, actor, _, _, proposal = seed(session); row = memory(session, actor, "obsolete"); application = invalidation_application(session, project)
    session.add(RetconCognitionInvalidation(project_id=project.id, retcon_application_id=application.id, character_id=actor.id, resource_type="MEMORY", resource_id=row.id, status=RetconCognitionInvalidationStatus.RESOLVED, reason="test", original_semantic_fingerprint="f")); session.commit()
    assert row.id not in recalled_ids(mind(session, project, actor, proposal), "memories")


def test_active_causal_usage_reinforces_knowledge(session):
    project, _, actor, _, _, proposal = seed(session)
    weak = knowledge(session, actor, "ENTITY a: x = true", confidence=0); used = knowledge(session, actor, "ENTITY b: x = true", confidence=0)
    use_link(session, project, CausalResourceType.CHARACTER_KNOWLEDGE, used.id, CausalRelationType.KNOWLEDGE_INFORMED_DECISION, 7)
    rows = mind(session, project, actor, proposal)["knowledge"]
    assert rows.index(next(item for item in rows if item["knowledge_id"] == used.id)) < rows.index(next(item for item in rows if item["knowledge_id"] == weak.id))


def test_inactive_causal_usage_does_not_reinforce_memory(session):
    project, _, actor, _, _, proposal = seed(session)
    first = memory(session, actor, "first", importance=0, confidence=0); old = memory(session, actor, "old", importance=0, confidence=0)
    use_link(session, project, CausalResourceType.CHARACTER_MEMORY, old.id, CausalRelationType.MEMORY_INFORMED_DECISION, 9, active=False)
    rows = mind(session, project, actor, proposal)["memories"]
    assert rows[0]["memory_id"] == min(first.id, old.id)


def test_active_causal_usage_reinforces_memory(session):
    project, _, actor, _, _, proposal = seed(session)
    first = memory(session, actor, "first", importance=0, confidence=0); used = memory(session, actor, "used", importance=0, confidence=0)
    use_link(session, project, CausalResourceType.CHARACTER_MEMORY, used.id, CausalRelationType.MEMORY_INFORMED_DECISION, 9)
    assert mind(session, project, actor, proposal)["memories"][0]["memory_id"] == used.id


def test_mind_fingerprint_is_stable(session):
    project, _, actor, _, _, proposal = seed(session)
    assert mind(session, project, actor, proposal)["mind_fingerprint"] == mind(session, project, actor, proposal)["mind_fingerprint"]


def test_irrelevant_dormant_memory_does_not_change_context_fingerprint(session):
    project, _, actor, _, _, proposal = seed(session)
    for index in range(MAX_CHARACTER_MEMORIES): memory(session, actor, f"important {index}", importance=10)
    before = context(session, project, actor, proposal)["fingerprint"]
    memory(session, actor, "dormant", importance=0, confidence=0)
    assert context(session, project, actor, proposal)["fingerprint"] == before


def test_recalled_memory_changes_context_fingerprint(session):
    project, location, actor, _, _, proposal = seed(session)
    before = context(session, project, actor, proposal)["fingerprint"]
    memory(session, actor, "cue", importance=10, distortion={"location_id": location.id})
    assert context(session, project, actor, proposal)["fingerprint"] != before


def test_relationship_changes_context_fingerprint(session):
    project, _, actor, other, _, proposal = seed(session); before = context(session, project, actor, proposal)["fingerprint"]
    actor.relationships = {other.id: {"trust": 0.2}}; session.add(actor); session.commit()
    assert context(session, project, actor, proposal)["fingerprint"] != before


def test_context_knowledge_has_explicit_id_and_fact_identity(session):
    project, _, actor, _, _, proposal = seed(session); row = knowledge(session, actor, "ENTITY door: opened = true")
    item = next(item for item in context(session, project, actor, proposal)["knowledge"]["KNOWN"] if item["knowledge_id"] == row.id)
    assert item["fact_identity"] == {"subject_type": "ENTITY", "subject_id": "door", "predicate": "opened", "value": True}


def test_context_memory_has_required_source_metadata(session):
    project, _, actor, _, _, proposal = seed(session); row = memory(session, actor, "memory")
    item = next(item for item in context(session, project, actor, proposal)["memories"] if item["memory_id"] == row.id)
    assert {"content", "importance", "emotional_weight", "confidence", "distortion", "happened_at", "source_scene_id"}.issubset(item)


def test_recalled_knowledge_reference_is_allowed(session):
    project, _, actor, _, _, proposal = seed(session); row = knowledge(session, actor, "ENTITY door: opened = true"); ctx = context(session, project, actor, proposal)
    reference = {"knowledge_id": row.id, "proposition": row.proposition, "accepted_statuses": ["KNOWN"]}
    assert CharacterDecisionConstraintChecker().validate(session, ctx, decision(project, actor, proposal, ctx, knowledge_used=[reference])).valid


def test_dormant_knowledge_reference_is_blocked(session):
    project, _, actor, _, _, proposal = seed(session)
    for index in range(MAX_CHARACTER_KNOWLEDGE): knowledge(session, actor, f"ENTITY e{index}: x = true", confidence=10)
    dormant = knowledge(session, actor, "ENTITY dormant: x = true", confidence=0); ctx = context(session, project, actor, proposal)
    report = CharacterDecisionConstraintChecker().validate(session, ctx, decision(project, actor, proposal, ctx, knowledge_used=[{"knowledge_id": dormant.id, "proposition": dormant.proposition, "accepted_statuses": ["KNOWN"]}]))
    assert any(item.code == "KNOWLEDGE_NOT_RECALLED" for item in report.issues)


def test_dormant_memory_reference_is_blocked(session):
    project, _, actor, _, _, proposal = seed(session)
    for index in range(MAX_CHARACTER_MEMORIES): memory(session, actor, f"important {index}", importance=10)
    dormant = memory(session, actor, "dormant", importance=0, confidence=0); ctx = context(session, project, actor, proposal)
    report = CharacterDecisionConstraintChecker().validate(session, ctx, decision(project, actor, proposal, ctx, memory_refs=[dormant.id]))
    assert any(item.code == "MEMORY_NOT_RECALLED" for item in report.issues)


def test_heuristic_uses_exact_recalled_knowledge_reference(session):
    project, _, actor, _, _, proposal = seed(session); row = knowledge(session, actor, "ENTITY door: opened = true"); ctx = context(session, project, actor, proposal)
    assert HeuristicCharacterActor().decide(ctx)["knowledge_used"] == [{"knowledge_id": row.id, "proposition": row.proposition, "accepted_statuses": ["KNOWN"]}]


def test_llm_payload_requires_knowledge_id():
    payload = {"decision_type": "WAIT", "intent": "wait", "chosen_action": "wait", "motivation": "wait", "target_character_id": None, "target_entity_id": None, "goal_refs": [], "knowledge_used": [{"proposition": "x", "accepted_statuses": ["KNOWN"]}], "memory_refs": [], "ability_refs": [], "inventory_refs": [], "relationship_factors": {}, "perceived_risk": None, "accepted_cost": None, "expected_personal_result": None, "uncertainties": [], "refused_options": [], "boundary_override_reason": None, "decision_summary": "wait"}
    with pytest.raises(Exception): CharacterDecisionPayload.model_validate(payload)


def test_actor_sanitizer_exposes_no_retrieval_internals(session):
    project, _, actor, _, _, proposal = seed(session); knowledge(session, actor, "ENTITY door: opened = true")
    rendered = json.dumps(ActorPerceptionSanitizer().sanitize(context(session, project, actor, proposal)))
    assert "mind_fingerprint" not in rendered and "score" not in rendered


def test_mind_api_is_read_only_and_project_isolated(session, monkeypatch):
    project, _, actor, _, outsider, proposal = seed(session); client = client_for(session, monkeypatch)
    before = len(session.identity_map)
    response = client.get(f"/projects/{project.id}/characters/{actor.id}/mind", params={"proposal_id": proposal.id})
    assert response.status_code == 200 and response.json()["proposal_id"] == proposal.id
    assert client.get(f"/projects/{project.id}/characters/{outsider.id}/mind", params={"proposal_id": proposal.id}).status_code == 409
    assert len(session.identity_map) >= before


def test_mind_api_does_not_expose_secret_canon(session, monkeypatch):
    project, _, actor, _, _, proposal = seed(session); client = client_for(session, monkeypatch)
    session.add(CanonFact(project_id=project.id, fact_type=CanonType.SECRET_CANON, proposition="secret-canon-proposition", data={}, locked=True)); session.commit()
    response = client.get(f"/projects/{project.id}/characters/{actor.id}/mind", params={"proposal_id": proposal.id})
    assert response.status_code == 200 and "secret-canon-proposition" not in response.text


def test_long_history_strong_cue_reactivates_old_memory(session):
    project, location, actor, _, _, proposal = seed(session)
    old = CharacterMemory(character_id=actor.id, content="old key memory", importance=0.2, emotional_weight=0, confidence=0.2, distortion={"location_id": location.id})
    session.add(old)
    session.add_all(CharacterMemory(character_id=actor.id, content=f"irrelevant {index}", importance=1, emotional_weight=0, confidence=1, distortion={}) for index in range(200))
    session.commit()
    assert old.id in recalled_ids(mind(session, project, actor, proposal), "memories")


def test_emotional_memory_outranks_equally_old_neutral_memory(session):
    project, _, actor, _, _, proposal = seed(session)
    neutral = memory(session, actor, "neutral", importance=0.5, emotional_weight=0, confidence=0.5)
    emotional = memory(session, actor, "emotional", importance=0.5, emotional_weight=4, confidence=0.5)
    rows = mind(session, project, actor, proposal)["memories"]
    assert rows.index(next(item for item in rows if item["memory_id"] == emotional.id)) < rows.index(next(item for item in rows if item["memory_id"] == neutral.id))


def test_heuristic_decision_naturally_creates_knowledge_causal_edge(session, monkeypatch):
    from sqlalchemy import select
    from test_scene_commit import prepared_commit
    project, _location, actor, _other, proposal, performance, _turn, _resolution, _batch, client = prepared_commit(session, monkeypatch)
    row = knowledge(session, actor, "ENTITY door: opened = true")
    remembered = memory(session, actor, "The door opened before.", importance=2)
    from app.director import DirectorContextBuilder
    proposal.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    performance.proposal_context_fingerprint = proposal.context_fingerprint
    session.add_all([proposal, performance]); session.commit()
    ctx = CharacterContextBuilder().build(session, project.id, actor.id, proposal)
    generated = HeuristicCharacterActor().decide(ctx)
    decision_row = session.scalar(select(CharacterDecision).where(CharacterDecision.scene_proposal_id == proposal.id))
    decision_row.knowledge_used = generated["knowledge_used"]; decision_row.memory_refs = generated["memory_refs"]; session.add(decision_row); session.commit()
    from app.state_delta import StateDeltaCandidateBuilder
    from app.state_delta_validation import StateDeltaValidator
    refreshed_batch = StateDeltaCandidateBuilder().derive(session, project.id, _resolution.id)[0]
    session.commit(); StateDeltaValidator().validate(session, project.id, refreshed_batch.id); session.commit()
    from app.scene_commit import SceneCommitService
    SceneCommitService().commit(session, project.id, performance.id); session.commit()
    assert session.scalar(select(CausalLink).where(CausalLink.cause_id == row.id, CausalLink.relation_type == CausalRelationType.KNOWLEDGE_INFORMED_DECISION, CausalLink.active.is_(True)))
    assert session.scalar(select(CausalLink).where(CausalLink.cause_id == remembered.id, CausalLink.relation_type == CausalRelationType.MEMORY_INFORMED_DECISION, CausalLink.active.is_(True)))


def test_fake_llm_explicit_recalled_knowledge_is_valid(session):
    project, _, actor, _, _, proposal = seed(session); row = knowledge(session, actor, "ENTITY door: opened = true"); ctx = context(session, project, actor, proposal)
    payload = {"decision_type": "INVESTIGATE", "intent": "check", "chosen_action": "check", "motivation": "check", "target_character_id": None, "target_entity_id": None, "goal_refs": [], "knowledge_used": [{"knowledge_id": row.id, "proposition": row.proposition, "accepted_statuses": ["KNOWN"]}], "memory_refs": [], "ability_refs": [], "inventory_refs": [], "relationship_factors": {}, "perceived_risk": None, "accepted_cost": None, "expected_personal_result": None, "uncertainties": [], "refused_options": [], "boundary_override_reason": None, "decision_summary": "check"}
    generated, _ = LLMCharacterActor(FakeModelProvider(json.dumps(payload)), "fake").decide(ActorPerceptionSanitizer().sanitize(ctx))
    assert CharacterDecisionConstraintChecker().validate(session, ctx, decision(project, actor, proposal, ctx, **generated)).valid


def test_fake_llm_explicit_knowledge_flows_to_phase8_causal_edge(session, monkeypatch):
    from app.director import DirectorContextBuilder
    from app.scene_commit import SceneCommitService
    from app.state_delta import StateDeltaCandidateBuilder
    from app.state_delta_validation import StateDeltaValidator
    from test_scene_commit import prepared_commit
    project, _location, actor, _other, proposal, performance, _turn, resolution, _batch, _client = prepared_commit(session, monkeypatch)
    row = knowledge(session, actor, "ENTITY door: opened = true")
    proposal.context_fingerprint = DirectorContextBuilder().build(session, project.id)["fingerprint"]
    performance.proposal_context_fingerprint = proposal.context_fingerprint; session.add_all([proposal, performance]); session.commit()
    ctx = context(session, project, actor, proposal)
    payload = {"decision_type": "INVESTIGATE", "intent": "check", "chosen_action": "check", "motivation": "check", "target_character_id": None, "target_entity_id": None, "goal_refs": [], "knowledge_used": [{"knowledge_id": row.id, "proposition": row.proposition, "accepted_statuses": ["KNOWN"]}], "memory_refs": [], "ability_refs": [], "inventory_refs": [], "relationship_factors": {}, "perceived_risk": None, "accepted_cost": None, "expected_personal_result": None, "uncertainties": [], "refused_options": [], "boundary_override_reason": None, "decision_summary": "check"}
    generated, _ = LLMCharacterActor(FakeModelProvider(json.dumps(payload)), "fake").decide(ActorPerceptionSanitizer().sanitize(ctx))
    decision_row = session.scalar(select(CharacterDecision).where(CharacterDecision.scene_proposal_id == proposal.id))
    decision_row.knowledge_used = generated["knowledge_used"]; session.add(decision_row); session.commit()
    batch = StateDeltaCandidateBuilder().derive(session, project.id, resolution.id)[0]
    session.commit(); StateDeltaValidator().validate(session, project.id, batch.id); session.commit()
    SceneCommitService().commit(session, project.id, performance.id); session.commit()
    assert session.scalar(select(CausalLink).where(CausalLink.cause_id == row.id, CausalLink.effect_id == decision_row.id, CausalLink.relation_type == CausalRelationType.KNOWLEDGE_INFORMED_DECISION, CausalLink.active.is_(True)))


def test_fake_llm_invented_or_dormant_knowledge_is_blocked(session):
    project, _, actor, _, _, proposal = seed(session)
    for index in range(MAX_CHARACTER_KNOWLEDGE): knowledge(session, actor, f"ENTITY e{index}: x = true", confidence=10)
    dormant = knowledge(session, actor, "ENTITY dormant: x = true", confidence=0); ctx = context(session, project, actor, proposal)
    payload = {"decision_type": "WAIT", "intent": "wait", "chosen_action": "wait", "motivation": "wait", "target_character_id": None, "target_entity_id": None, "goal_refs": [], "knowledge_used": [{"knowledge_id": dormant.id, "proposition": dormant.proposition, "accepted_statuses": ["KNOWN"]}], "memory_refs": [], "ability_refs": [], "inventory_refs": [], "relationship_factors": {}, "perceived_risk": None, "accepted_cost": None, "expected_personal_result": None, "uncertainties": [], "refused_options": [], "boundary_override_reason": None, "decision_summary": "wait"}
    generated, _ = LLMCharacterActor(FakeModelProvider(json.dumps(payload)), "fake").decide({})
    report = CharacterDecisionConstraintChecker().validate(session, ctx, decision(project, actor, proposal, ctx, **generated))
    assert any(item.code == "KNOWLEDGE_NOT_RECALLED" for item in report.issues)


def test_confidence_salience_orders_otherwise_equal_memories(session):
    project, _, actor, _, _, proposal = seed(session)
    low = memory(session, actor, "low", importance=0, emotional_weight=0, confidence=0)
    high = memory(session, actor, "high", importance=0, emotional_weight=0, confidence=2)
    rows = mind(session, project, actor, proposal)["memories"]
    assert rows.index(next(item for item in rows if item["memory_id"] == high.id)) < rows.index(next(item for item in rows if item["memory_id"] == low.id))


def test_scene_sequence_recency_orders_memory_without_wall_clock(session):
    project, _location, actor, _, _, proposal = seed(session)
    old_scene = current_scene(session, project, 1, "old", [])
    new_scene = current_scene(session, project, 20, "new", [])
    old = memory(session, actor, "old", importance=0, emotional_weight=0, confidence=0, source_scene=old_scene.id)
    fresh = memory(session, actor, "fresh", importance=0, emotional_weight=0, confidence=0, source_scene=new_scene.id)
    first = mind(session, project, actor, proposal)["memories"]
    second = mind(session, project, actor, proposal)["memories"]
    assert [item["memory_id"] for item in first] == [item["memory_id"] for item in second]
    assert first.index(next(item for item in first if item["memory_id"] == fresh.id)) < first.index(next(item for item in first if item["memory_id"] == old.id))


def test_mind_and_context_builders_do_not_mutate_formal_history(session):
    from sqlalchemy import func, select
    from app.models import TimelineEvent
    from app.versioning import WorldSnapshotBuilder
    project, _, actor, _, _, proposal = seed(session); knowledge(session, actor, "ENTITY door: opened = true"); memory(session, actor, "memory")
    before_payload, before_fingerprint = WorldSnapshotBuilder().build(session, project.id)
    before = (session.scalar(select(func.count(CharacterKnowledge.id))), session.scalar(select(func.count(CharacterMemory.id))), session.scalar(select(func.count(CausalLink.id))), session.scalar(select(func.count(TimelineEvent.id))))
    CharacterMindViewBuilder().build(session, project.id, actor.id, proposal); CharacterContextBuilder().build(session, project.id, actor.id, proposal)
    after_payload, after_fingerprint = WorldSnapshotBuilder().build(session, project.id)
    after = (session.scalar(select(func.count(CharacterKnowledge.id))), session.scalar(select(func.count(CharacterMemory.id))), session.scalar(select(func.count(CausalLink.id))), session.scalar(select(func.count(TimelineEvent.id))))
    assert before_fingerprint == after_fingerprint and before_payload == after_payload and before == after
