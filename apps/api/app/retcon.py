"""Deterministic, preview-only historical impact planning."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    CanonFact, Character, CharacterDecision, CharacterKnowledge, CharacterMemory,
    Scene, ScenePerformanceTurn, StoryThread, WorldEntity, WorldResolution,
    RetconImpactItem, RetconImpactPlan, RetconRequest,
)
from .revision import RevisionStateFingerprintBuilder, StructuredReferenceScanner

CLASSIFICATION_LABELS = {
    "UNCHANGED": "可以保留", "REVALIDATE": "需要重新验证", "REBUILD_COGNITION": "需要重建认知",
    "REPLAY_REQUIRED": "需要重新演出", "INVALIDATED": "修改后将失效",
}

def _value(record: Any, field: str) -> Any:
    value = getattr(record, field, None)
    return getattr(value, "value", value)

def semantic_fingerprint(value: Any) -> str:
    stable = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return "retcon-basis-v1:" + hashlib.sha256(stable.encode()).hexdigest()

@dataclass(frozen=True)
class DependencyEdge:
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    edge_type: str
    reason: str
    path: list[dict[str, str]]

class HistoricalDependencyGraphBuilder:
    """Builds only explicit database/JSON lineage; never reads prose for causality."""
    def __init__(self, max_nodes: int = 10000):
        self.scanner = StructuredReferenceScanner()
        self.max_nodes = max_nodes
        self.limit_reached = False

    def build(self, session: Session, project_id: str, target_ids: set[str], old_values: set[str]) -> list[DependencyEdge]:
        edges: list[DependencyEdge] = []; seen: set[tuple[str, str, str, str, str]] = set()
        def add(source_type: str, source_id: str, target_type: str, target_id: str, edge_type: str, reason: str, path: list[str] | None = None):
            key = (source_type, source_id, target_type, target_id, edge_type)
            if key in seen: return
            if len(edges) >= self.max_nodes:
                self.limit_reached = True
                return
            seen.add(key)
            edges.append(DependencyEdge(source_type, source_id, target_type, target_id, edge_type, reason, [{"type": source_type, "id": source_id}, *([{"type": target_type, "id": target_id}] if not path else [{"type": target_type, "id": target_id, "path": ".".join(path)}])]))
        canon_ids = set(target_ids)
        characters = session.scalars(select(Character).where(Character.project_id == project_id)).all()
        for fact in session.scalars(select(CanonFact).where(CanonFact.project_id == project_id)).all():
            if fact.id in target_ids: continue
            for target in target_ids:
                paths = self.scanner.paths(fact.data, target)
                for path in paths: add("CANON_FACT", fact.id, "CANON_FACT", target, "CANON_STRUCTURED_REFERENCE", "结构化世界事实引用", [path])
        for knowledge in session.scalars(select(CharacterKnowledge).where(CharacterKnowledge.character_id.in_([c.id for c in characters]))).all() if characters else []:
            for target in target_ids:
                if knowledge.source == target or knowledge.proposition in old_values:
                    add("CANON_FACT" if knowledge.source == target else "CANON_FACT", target, "CHARACTER_KNOWLEDGE", knowledge.id, "CANON_TO_KNOWLEDGE", "人物认知直接来自待修改世界事实")
        for decision in session.scalars(select(CharacterDecision).where(CharacterDecision.project_id == project_id)).all():
            for knowledge in decision.knowledge_used or []:
                if isinstance(knowledge, dict) and (knowledge.get("knowledge_id") in {e.target_id for e in edges if e.target_type == "CHARACTER_KNOWLEDGE"} or knowledge.get("proposition") in old_values):
                    add("CHARACTER_KNOWLEDGE", str(knowledge.get("knowledge_id", "")), "CHARACTER_DECISION", decision.id, "KNOWLEDGE_TO_DECISION", "角色决策使用受影响认知")
            for target in target_ids:
                paths = self.scanner.paths({"memory_refs": decision.memory_refs, "ability_refs": decision.ability_refs, "inventory_refs": decision.inventory_refs, "relationship_factors": decision.relationship_factors}, target)
                for path in paths: add("CHARACTER_MEMORY", target, "CHARACTER_DECISION", decision.id, "MEMORY_TO_DECISION", "角色决策引用结构化记忆或目标", [path])
        for memory in session.scalars(select(CharacterMemory).where(CharacterMemory.character_id.in_([c.id for c in characters]))).all() if characters else []:
            for target in target_ids:
                paths = self.scanner.paths(memory.distortion, target)
                if paths or memory.content in old_values: add("CANON_FACT", target, "CHARACTER_MEMORY", memory.id, "CANON_TO_MEMORY", "记忆包含结构化事实依赖")
        for turn in session.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.project_id == project_id)).all():
            decision = session.get(CharacterDecision, turn.character_decision_id)
            if decision and any(e.target_type == "CHARACTER_DECISION" and e.target_id == decision.id for e in edges): add("CHARACTER_DECISION", decision.id, "SCENE_PERFORMANCE_TURN", turn.id, "DECISION_TO_TURN", "演出回合使用受影响角色决策")
        for resolution in session.scalars(select(WorldResolution).where(WorldResolution.project_id == project_id)).all():
            if any(e.target_type == "SCENE_PERFORMANCE_TURN" and e.target_id == resolution.performance_turn_id for e in edges): add("SCENE_PERFORMANCE_TURN", resolution.performance_turn_id, "WORLD_RESOLUTION", resolution.id, "TURN_TO_RESOLUTION", "世界响应属于受影响演出回合")
        for scene in session.scalars(select(Scene).where(Scene.project_id == project_id)).all():
            for target in target_ids:
                paths = self.scanner.paths({"participants": scene.participants, "facts": scene.facts, "result": scene.result, "story_threads": scene.story_threads}, target)
                for old in old_values:
                    paths.extend(self.scanner.paths({"participants": scene.participants, "facts": scene.facts, "result": scene.result, "story_threads": scene.story_threads}, old))
                if paths: add("RETCON_TARGET", target, "SCENE", scene.id, "EXPLICIT_SCENE_REFERENCE", "场景结构化事实引用待修改目标", paths)
        affected_scene_ids = {edge.target_id for edge in edges if edge.target_type == "SCENE"}
        for knowledge in session.scalars(select(CharacterKnowledge).where(CharacterKnowledge.character_id.in_([c.id for c in characters]))).all() if characters else []:
            if knowledge.source in affected_scene_ids:
                add("SCENE", knowledge.source, "CHARACTER_KNOWLEDGE", knowledge.id, "SCENE_TO_KNOWLEDGE", "人物认知来源于受影响场景")
        for memory in session.scalars(select(CharacterMemory).where(CharacterMemory.character_id.in_([c.id for c in characters]))).all() if characters else []:
            if memory.source_scene in affected_scene_ids:
                add("SCENE", memory.source_scene, "CHARACTER_MEMORY", memory.id, "SCENE_TO_MEMORY", "人物记忆来源于受影响场景")
        affected_knowledge_ids = {edge.target_id for edge in edges if edge.target_type == "CHARACTER_KNOWLEDGE"}
        affected_memory_ids = {edge.target_id for edge in edges if edge.target_type == "CHARACTER_MEMORY"}
        for decision in session.scalars(select(CharacterDecision).where(CharacterDecision.project_id == project_id)).all():
            knowledge_refs = {str(item.get("knowledge_id")) for item in (decision.knowledge_used or []) if isinstance(item, dict)}
            if knowledge_refs & affected_knowledge_ids:
                add("CHARACTER_KNOWLEDGE", sorted(knowledge_refs & affected_knowledge_ids)[0], "CHARACTER_DECISION", decision.id, "KNOWLEDGE_TO_DECISION", "角色决策使用受影响认知")
            if set(decision.memory_refs or []) & affected_memory_ids:
                add("CHARACTER_MEMORY", sorted(set(decision.memory_refs or []) & affected_memory_ids)[0], "CHARACTER_DECISION", decision.id, "MEMORY_TO_DECISION", "角色决策使用受影响记忆")
        for turn in session.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.project_id == project_id)).all():
            if any(edge.target_type == "CHARACTER_DECISION" and edge.target_id == turn.character_decision_id for edge in edges):
                add("CHARACTER_DECISION", turn.character_decision_id, "SCENE_PERFORMANCE_TURN", turn.id, "DECISION_TO_TURN", "演出回合使用受影响角色决策")
        for resolution in session.scalars(select(WorldResolution).where(WorldResolution.project_id == project_id)).all():
            if any(edge.target_type == "SCENE_PERFORMANCE_TURN" and edge.target_id == resolution.performance_turn_id for edge in edges):
                add("SCENE_PERFORMANCE_TURN", resolution.performance_turn_id, "WORLD_RESOLUTION", resolution.id, "TURN_TO_RESOLUTION", "世界响应属于受影响演出回合")
        return edges

class ImpactClassificationResolver:
    def classify(self, edge: DependencyEdge) -> tuple[str, str]:
        if edge.target_type == "CHARACTER_KNOWLEDGE": return "REBUILD_COGNITION", "该人物认知直接依赖被修改的世界事实。"
        if edge.target_type == "CHARACTER_MEMORY": return "REBUILD_COGNITION", "该人物记忆包含被修改事实的结构化依赖。"
        if edge.target_type == "CHARACTER_DECISION" or edge.target_type == "SCENE_PERFORMANCE_TURN": return "REPLAY_REQUIRED", "演出使用了需要重新建立的认知或决策。"
        if edge.target_type == "WORLD_RESOLUTION": return "INVALIDATED", "世界响应属于需要重新演出的回合。"
        if edge.target_type == "SCENE": return "REPLAY_REQUIRED", "场景存在明确的结构化历史依赖。"
        return "REVALIDATE", "存在明确引用，需要重新验证。"

class ReplayBoundaryFinder:
    def find(self, scenes: list[Scene], affected_scene_ids: set[str]) -> tuple[str | None, int | None, dict[str, Any]]:
        affected = sorted((s for s in scenes if s.id in affected_scene_ids), key=lambda s: (s.sequence, s.id))
        if not affected: return None, None, {"issues": []}
        first = affected[0]
        return first.id, first.sequence, {"issues": []}

class CharacterCognitionImpactPlanner:
    def plan(self, session: Session, project_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        character_ids = {item["character_id"] for item in items if item.get("character_id")}
        return [{"character_id": cid, "affected_knowledge_ids": [item["resource_id"] for item in items if item.get("character_id") == cid and item["resource_type"] == "CHARACTER_KNOWLEDGE"], "affected_memory_ids": [item["resource_id"] for item in items if item.get("character_id") == cid and item["resource_type"] == "CHARACTER_MEMORY"], "reason": "来自受影响世界事实的明确认知依赖"} for cid in sorted(character_ids)]

class RetconImpactPlanner:
    def analyze(self, session: Session, request: RetconRequest, revision: Any) -> tuple[RetconImpactPlan, list[RetconImpactItem]]:
        normalized = revision.normalized_changes or []
        target_ids = {item["target_id"] for item in normalized}
        old_values = {str(item["before_value"]) for item in normalized if item.get("path") == "/proposition" and item.get("before_value") is not None}
        graph_builder = HistoricalDependencyGraphBuilder(); edges = graph_builder.build(session, request.project_id, target_ids, old_values)
        resolver = ImpactClassificationResolver(); scene_rows = session.scalars(select(Scene).where(Scene.project_id == request.project_id)).all()
        items: list[RetconImpactItem] = []; affected_scene_ids: set[str] = set()
        for edge in edges:
            classification, reason = resolver.classify(edge)
            character_id = None
            if edge.target_type in {"CHARACTER_KNOWLEDGE", "CHARACTER_MEMORY"}:
                record = session.get(CharacterKnowledge if edge.target_type == "CHARACTER_KNOWLEDGE" else CharacterMemory, edge.target_id); character_id = record.character_id if record else None
            scene_id = edge.target_id if edge.target_type == "SCENE" else None
            if scene_id: affected_scene_ids.add(scene_id)
            items.append(RetconImpactItem(plan_id="", resource_type=edge.target_type, resource_id=edge.target_id, classification=classification, reason_code=edge.edge_type, reason_summary=reason, character_id=character_id, scene_id=scene_id, dependency_path=edge.path))
        for scene in scene_rows:
            if scene.id not in affected_scene_ids:
                items.append(RetconImpactItem(plan_id="", resource_type="SCENE", resource_id=scene.id, classification="UNCHANGED", reason_code="NO_EXPLICIT_DEPENDENCY", reason_summary="没有发现连接待修改目标的结构化历史引用。", scene_id=scene.id, dependency_path=[]))
        earliest_id, earliest_sequence, validation = ReplayBoundaryFinder().find(scene_rows, affected_scene_ids)
        counts = {key: sum(item.classification == key for item in items) for key in ("UNCHANGED", "REVALIDATE", "REBUILD_COGNITION", "REPLAY_REQUIRED", "INVALIDATED")}
        summary = {**counts, "total_impacts": len(items), "affected_characters": len({item.character_id for item in items if item.character_id}), "replay_scene_count": sum(item.resource_type == "SCENE" and item.classification == "REPLAY_REQUIRED" for item in items), "preserved_scene_count": sum(item.resource_type == "SCENE" and item.classification == "UNCHANGED" for item in items)}
        basis = RevisionStateFingerprintBuilder().build(session, request.project_id)
        parent = session.scalar(select(RetconImpactPlan).where(RetconImpactPlan.retcon_request_id == request.id).order_by(RetconImpactPlan.version.desc()))
        if graph_builder.limit_reached:
            validation["issues"].append({"code": "PLAN_GRAPH_LIMIT_REACHED", "severity": "BLOCKING", "resource_type": "RETCON_GRAPH", "resource_id": request.id, "message": "依赖图超过规划节点上限。"})
        plan = RetconImpactPlan(project_id=request.project_id, retcon_request_id=request.id, version=request.current_plan_version + 1, parent_plan_id=parent.id if parent else None, basis_fingerprint=basis, status="BLOCKED" if graph_builder.limit_reached else "READY", earliest_affected_scene_id=earliest_id, earliest_affected_sequence=earliest_sequence, impact_summary=summary, validation_report=validation)
        return plan, items
