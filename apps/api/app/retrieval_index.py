"""Phase 16C1 rebuildable retrieval projections.

The index rows are never consulted as authority.  They only make PostgreSQL
candidate selection cheap when their complete derived state is READY; all
missing, dirty, or unsupported cases deliberately fall back to frozen readers.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import Float, and_, case, cast, delete, exists, func, literal, or_, select, text
from sqlalchemy.orm import Session

from .character_mind import CognitionFactIdentityParser, memory_source_bucket
from .embeddings import memory_content_fingerprint
from .execution_trace import stable_fingerprint
from .models import (
    CausalLink, CausalRelationType, CausalResourceType, Character, CharacterKnowledge,
    CharacterMemory, CharacterMemoryCueRef, CharacterMemorySearchIndex,
    CharacterKnowledgeSearchIndex, CognitionUsageHead, ProjectCognitionRetrievalIndex,
    ResearchChunk, ResearchChunkLexicalIndex, ResearchDocument, ResearchDocumentRevision,
    ResearchLexicalIndexState, ResearchTermPosting, ResearchTermStat, RetrievalIndexStatus,
    Scene, Project,
    KnowledgeStatus, RetconCognitionInvalidation, RetconCognitionInvalidationStatus,
    CharacterMemoryEmbedding, EmbeddingStatus, MemoryVectorSearchMode, ProjectModelConfig,
)


COGNITION_PROTOCOL = "character-cognition-search-v1"
RESEARCH_PROTOCOL = "research-inverted-index-v1"
ANN_VECTOR_DIMS = (384, 512, 768, 1024, 1536)
ANN_HALFVEC_DIMS = (3072,)
ANN_CANDIDATE_HARD_LIMIT = 512


class PgvectorANNCaps:
    """Fixed, schema-owned ANN capabilities. Unsupported dimensions stay exact."""

    @staticmethod
    def index_spec(dimension: int) -> tuple[str, str] | None:
        if dimension in ANN_VECTOR_DIMS:
            return ("VECTOR", f"ix_memory_embedding_hnsw_cosine_{dimension}")
        if dimension in ANN_HALFVEC_DIMS:
            return ("HALFVEC", f"ix_memory_embedding_hnsw_halfvec_cosine_{dimension}")
        return None

    def version(self, db: Session) -> str | None:
        if db.bind is None or db.bind.dialect.name != "postgresql":
            return None
        return db.scalar(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))

    def physical_index(self, db: Session, dimension: int) -> dict[str, Any]:
        spec = self.index_spec(dimension)
        if db.bind is None or db.bind.dialect.name != "postgresql":
            return {"supported": False, "index_kind": "NONE", "physical_index_name": None, "physical_index_valid": False, "fallback_reason": "POSTGRESQL_REQUIRED", "pgvector_version": None}
        version = self.version(db)
        try:
            version_ok = tuple(int(part) for part in (version or "0").split(".")[:2]) >= (0, 8)
        except ValueError:
            version_ok = False
        if not version_ok:
            return {"supported": False, "index_kind": "NONE", "physical_index_name": None, "physical_index_valid": False, "fallback_reason": "PGVECTOR_VERSION_UNSUPPORTED", "pgvector_version": version}
        if not spec:
            return {"supported": False, "index_kind": "NONE", "physical_index_name": None, "physical_index_valid": False, "fallback_reason": "UNSUPPORTED_DIMENSION", "pgvector_version": version}
        kind, name = spec
        row = db.execute(text("SELECT i.indisvalid, i.indisready, pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :name"), {"name": name}).first()
        expected_cast = f"{kind.lower()}({dimension})"
        definition = row[2].lower() if row else ""
        valid = bool(row and row[0] and row[1] and "using hnsw" in definition and expected_cast in definition and "cosine_ops" in definition and "status" in definition and "ready" in definition and f"dimension = {dimension}" in definition)
        return {"supported": True, "index_kind": kind, "physical_index_name": name, "physical_index_valid": valid, "fallback_reason": None if valid else "ANN_INDEX_UNAVAILABLE", "pgvector_version": self.version(db)}


class MemoryANNIndexStatusService:
    def status(self, db: Session, project_id: str) -> dict[str, Any]:
        config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == project_id))
        requested = _value(config.memory_vector_search_mode) if config else MemoryVectorSearchMode.EXACT.value
        dimension = config.embedding_dimension if config else None
        physical = PgvectorANNCaps().physical_index(db, dimension or 0)
        effective = MemoryVectorSearchMode.ANN.value if requested == MemoryVectorSearchMode.ANN.value and physical["physical_index_valid"] else MemoryVectorSearchMode.EXACT.value
        return {"requested_mode": requested, "effective_mode": effective, "dimension": dimension, **physical}


class MemoryANNPhysicalIndexAudit:
    def audit(self, db: Session, project_id: str) -> dict[str, Any]:
        status = MemoryANNIndexStatusService().status(db, project_id)
        if status["requested_mode"] == MemoryVectorSearchMode.ANN.value and not status["physical_index_valid"]:
            raise ValueError("MEMORY_ANN_PHYSICAL_INDEX_INVALID")
        return {"valid": True, **status}


class MemoryANNCertificationService:
    """Diagnostic ANN-vs-exact comparison; never an eligibility authority."""

    def certify(self, db: Session, project_id: str, character_id: str, query_vector: list[float], config_fingerprint: str, top_k: int, min_similarity: float | None, config) -> dict[str, Any]:
        hidden = CurrentCharacterCognitionFastRetriever()._hidden(project_id, character_id, "MEMORY", CharacterMemory.id)
        ann = CharacterMemoryANNSemanticRetriever().retrieve(db, project_id, character_id, query_vector, config_fingerprint, top_k, min_similarity, config, hidden)
        distance = cast(CharacterMemoryEmbedding.embedding.op("<=>")(query_vector), Float)
        filters = CharacterMemoryANNSemanticRetriever._filters(project_id, character_id, len(query_vector), config_fingerprint, hidden)
        exact_rows = db.execute(
            select(CharacterMemoryEmbedding.memory_id, distance.label("distance"))
            .select_from(CharacterMemoryEmbedding)
            .join(CharacterMemorySearchIndex, and_(CharacterMemorySearchIndex.memory_id == CharacterMemoryEmbedding.memory_id, CharacterMemorySearchIndex.content_fingerprint == CharacterMemoryEmbedding.content_fingerprint))
            .join(CharacterMemory, CharacterMemory.id == CharacterMemoryEmbedding.memory_id)
            .join(Character, Character.id == CharacterMemory.character_id)
            .where(*filters, (literal(1.0) - distance) >= min_similarity if min_similarity is not None else literal(True))
            .order_by(distance.asc(), CharacterMemoryEmbedding.memory_id).limit(top_k)
        ).all()
        exact = [row[0] for row in exact_rows]
        ann_ids = ann or []
        overlap = len(set(ann_ids).intersection(exact))
        return {
            "diagnostic_only": True,
            "ann_available": ann is not None,
            "recall_at_k": overlap / len(exact) if exact else 1.0,
            "rank_overlap": overlap,
            "exact_top_k_match": ann_ids == exact,
            "fallback_count": 0 if ann is not None else 1,
        }


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _fingerprint(value: Any, protocol: str) -> str:
    return stable_fingerprint(value, protocol)


def _acquire_cognition_index_lock(db: Session, project_id: str) -> None:
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"cognition-index:{project_id}"))))


def _acquire_research_index_lock(db: Session, project_id: str) -> None:
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"research-index:{project_id}"))))


class CognitionRetrievalProjectionService:
    """Builds current cognition search rows from immutable formal records."""

    parser = CognitionFactIdentityParser()

    def _state(self, db: Session, project_id: str) -> ProjectCognitionRetrievalIndex | None:
        return db.scalar(select(ProjectCognitionRetrievalIndex).where(ProjectCognitionRetrievalIndex.project_id == project_id))

    def mark_dirty(self, db: Session, project_id: str, sequence: int | None = None) -> None:
        state = self._state(db, project_id)
        if state is None:
            state = ProjectCognitionRetrievalIndex(project_id=project_id, protocol_version=COGNITION_PROTOCOL, status=RetrievalIndexStatus.DIRTY, dirty_from_sequence=sequence)
            db.add(state)
        else:
            state.status = RetrievalIndexStatus.DIRTY
            if sequence is not None:
                state.dirty_from_sequence = min(state.dirty_from_sequence, sequence) if state.dirty_from_sequence is not None else sequence
        db.flush()

    def _source_fingerprint(self, knowledge: list[CharacterKnowledge], memories: list[CharacterMemory], links: list[CausalLink]) -> str:
        return _fingerprint({
            "knowledge": [(row.id, row.character_id, _value(row.status), row.proposition, row.confidence, row.source, row.acquired_at) for row in knowledge],
            "memories": [(row.id, row.character_id, row.content, row.importance, row.emotional_weight, row.confidence, row.distortion, row.source_scene, row.happened_at) for row in memories],
            "usage": [(row.id, _value(row.cause_type), row.cause_id, _value(row.relation_type), row.active, row.sequence) for row in links],
        }, "character-cognition-source-v1")

    @staticmethod
    def _state_fingerprint(source: str | None, knowledge_count: int, memory_count: int, usage_count: int, built_through: int) -> str:
        return _fingerprint({"source": source, "knowledge": knowledge_count, "memory": memory_count, "usage": usage_count, "built_through": built_through}, COGNITION_PROTOCOL)

    @staticmethod
    def _cue_rows(memory: CharacterMemory, project_id: str, source_scene: Scene | None) -> list[tuple[str, str, str]]:
        distortion = memory.distortion if isinstance(memory.distortion, dict) else {}
        rows: set[tuple[str, str, str]] = set()
        mapping = {"entity_ids": "ENTITY", "participant_ids": "PARTICIPANT", "thread_ids": "THREAD", "item_ids": "ITEM"}
        for field, cue_type in mapping.items():
            for value in distortion.get(field, []) if isinstance(distortion.get(field), list) else []:
                if isinstance(value, str) and value:
                    rows.add((cue_type, value, "DISTORTION"))
        location = distortion.get("location_id")
        if isinstance(location, str) and location:
            rows.add(("LOCATION", location, "DISTORTION"))
        if source_scene and source_scene.history_status == "ACTIVE":
            if source_scene.location:
                rows.add(("LOCATION", str(source_scene.location), "SCENE")); rows.add(("ENTITY", str(source_scene.location), "SCENE"))
            for value in source_scene.participants or []:
                if isinstance(value, str): rows.add(("PARTICIPANT", value, "SCENE"))
            for value in source_scene.story_threads or []:
                if isinstance(value, str): rows.add(("THREAD", value, "SCENE"))
        return sorted(rows)

    def rebuild(self, db: Session, project_id: str) -> ProjectCognitionRetrievalIndex:
        _acquire_cognition_index_lock(db, project_id)
        state = self._state(db, project_id)
        if state is None:
            state = ProjectCognitionRetrievalIndex(project_id=project_id, protocol_version=COGNITION_PROTOCOL, status=RetrievalIndexStatus.REBUILDING)
            db.add(state); db.flush()
        else:
            state.status = RetrievalIndexStatus.REBUILDING
        knowledge = db.scalars(select(CharacterKnowledge).join(Character).where(Character.project_id == project_id).order_by(CharacterKnowledge.id)).all()
        memories = db.scalars(select(CharacterMemory).join(Character).where(Character.project_id == project_id).order_by(CharacterMemory.id)).all()
        links = db.scalars(select(CausalLink).where(CausalLink.project_id == project_id, CausalLink.active.is_(True), CausalLink.cause_type.in_([CausalResourceType.CHARACTER_KNOWLEDGE, CausalResourceType.CHARACTER_MEMORY]), CausalLink.relation_type.in_([CausalRelationType.KNOWLEDGE_INFORMED_DECISION, CausalRelationType.MEMORY_INFORMED_DECISION])).order_by(CausalLink.id)).all()
        db.execute(delete(CharacterMemoryCueRef).where(CharacterMemoryCueRef.project_id == project_id))
        db.execute(delete(CharacterMemorySearchIndex).where(CharacterMemorySearchIndex.project_id == project_id))
        db.execute(delete(CharacterKnowledgeSearchIndex).where(CharacterKnowledgeSearchIndex.project_id == project_id))
        db.execute(delete(CognitionUsageHead).where(CognitionUsageHead.project_id == project_id))
        scenes = {row.id: row for row in db.scalars(select(Scene).where(Scene.project_id == project_id)).all()}
        for row in knowledge:
            identity = self.parser.parse(row.proposition)
            value_fp = _fingerprint(identity["value"], "cognition-fact-value-v1") if identity else None
            payload = {"knowledge": row.id, "character": row.character_id, "status": _value(row.status), "confidence": row.confidence, "identity": identity, "source": row.source, "acquired": row.acquired_at}
            db.add(CharacterKnowledgeSearchIndex(project_id=project_id, character_id=row.character_id, knowledge_id=row.id, knowledge_status=_value(row.status), confidence=row.confidence, subject_type=identity["subject_type"] if identity else None, subject_id=identity["subject_id"] if identity else None, predicate=identity["predicate"] if identity else None, value_fingerprint=value_fp, proposition_fingerprint=_fingerprint(row.proposition, "character-knowledge-proposition-v1"), source_scene_id=row.source if row.source in scenes else None, acquired_at=row.acquired_at, index_fingerprint=_fingerprint(payload, COGNITION_PROTOCOL)))
        for row in memories:
            source = scenes.get(row.source_scene) if row.source_scene else None
            # Source-scene structured metadata participates only while its
            # formal scene remains current. The id is still retained in the
            # diversity bucket, exactly as the legacy memory payload does.
            sequence = source.sequence if source and source.history_status == "ACTIVE" else None
            item = {"memory_id": row.id, "source_scene_id": row.source_scene, "source_scene_sequence": sequence}
            payload = {"memory": row.id, "character": row.character_id, "importance": row.importance, "emotional": row.emotional_weight, "confidence": row.confidence, "happened": row.happened_at, "source": row.source_scene, "sequence": sequence, "content": memory_content_fingerprint(row.content)}
            db.add(CharacterMemorySearchIndex(project_id=project_id, character_id=row.character_id, memory_id=row.id, importance=row.importance, emotional_weight=row.emotional_weight, confidence=row.confidence, happened_at=row.happened_at, source_scene_id=row.source_scene, source_sequence=sequence, source_bucket=memory_source_bucket(item), content_fingerprint=memory_content_fingerprint(row.content), index_fingerprint=_fingerprint(payload, COGNITION_PROTOCOL)))
            for cue_type, cue_value, source_kind in self._cue_rows(row, project_id, source):
                db.add(CharacterMemoryCueRef(project_id=project_id, character_id=row.character_id, memory_id=row.id, cue_type=cue_type, cue_value=cue_value, source=source_kind))
        usage: dict[tuple[str, str], tuple[int, int]] = {}
        for link in links:
            kind = _value(link.cause_type); key = (kind, link.cause_id); count, latest = usage.get(key, (0, -1))
            usage[key] = (count + 1, max(latest, link.sequence if link.sequence is not None else -1))
        for (resource_type, resource_id), (count, latest) in usage.items():
            fingerprint = _fingerprint({"type": resource_type, "id": resource_id, "count": count, "latest": latest}, "cognition-usage-head-v1")
            db.add(CognitionUsageHead(project_id=project_id, resource_type=resource_type, resource_id=resource_id, usage_count=count, latest_sequence=latest, usage_fingerprint=fingerprint))
        db.flush()
        max_sequence = db.scalar(select(func.max(Scene.sequence)).where(Scene.project_id == project_id, Scene.history_status == "ACTIVE")) or 0
        state.protocol_version, state.status, state.built_through_sequence = COGNITION_PROTOCOL, RetrievalIndexStatus.READY, max_sequence
        state.indexed_knowledge_count, state.indexed_memory_count, state.usage_head_count = len(knowledge), len(memories), len(usage)
        state.source_fingerprint = self._source_fingerprint(knowledge, memories, links)
        state.index_fingerprint = self._state_fingerprint(state.source_fingerprint, len(knowledge), len(memories), len(usage), max_sequence)
        state.dirty_from_sequence, state.last_rebuilt_at = None, datetime.utcnow()
        db.flush()
        return state

    def _refresh_usage_heads(self, db: Session, project_id: str, resource_ids: set[tuple[str, str]]) -> None:
        for resource_type, resource_id in resource_ids:
            relation = CausalRelationType.KNOWLEDGE_INFORMED_DECISION if resource_type == CausalResourceType.CHARACTER_KNOWLEDGE.value else CausalRelationType.MEMORY_INFORMED_DECISION
            count, latest = db.execute(select(func.count(CausalLink.id), func.coalesce(func.max(CausalLink.sequence), -1)).where(
                CausalLink.project_id == project_id, CausalLink.active.is_(True), CausalLink.cause_type == resource_type,
                CausalLink.cause_id == resource_id, CausalLink.relation_type == relation,
            )).one()
            head = db.scalar(select(CognitionUsageHead).where(CognitionUsageHead.project_id == project_id, CognitionUsageHead.resource_type == resource_type, CognitionUsageHead.resource_id == resource_id))
            if not count:
                if head: db.delete(head)
                continue
            fingerprint = _fingerprint({"type": resource_type, "id": resource_id, "count": count, "latest": latest}, "cognition-usage-head-v1")
            if head is None:
                db.add(CognitionUsageHead(project_id=project_id, resource_type=resource_type, resource_id=resource_id, usage_count=count, latest_sequence=latest, usage_fingerprint=fingerprint))
            else:
                head.usage_count, head.latest_sequence, head.usage_fingerprint = count, latest, fingerprint

    def sync_after_scene_commit(self, db: Session, project_id: str, scene_id: str, sequence: int | None = None) -> None:
        """Append only current-scene cognition and causal usage projections.

        An index missing a historical prefix is marked DIRTY.  It is never
        silently rebuilt during a scene commit, because that turns a normal
        O(new-scene) write into an unbounded history scan.
        """
        _acquire_cognition_index_lock(db, project_id)
        state = self._state(db, project_id)
        if state is None and (sequence or 0) <= 1:
            # Cold start has no historical prefix to conceal, so a one-time
            # materialization is both exact and bounded by that first scene.
            self.rebuild(db, project_id)
            return
        if not state or state.status != RetrievalIndexStatus.READY:
            self.mark_dirty(db, project_id, sequence)
            return
        scene = db.get(Scene, scene_id)
        if not scene or scene.project_id != project_id:
            raise ValueError("COGNITION_RETRIEVAL_INDEX_SCENE_INVALID")
        knowledge = db.scalars(select(CharacterKnowledge).join(Character).where(Character.project_id == project_id, CharacterKnowledge.source == scene_id)).all()
        memories = db.scalars(select(CharacterMemory).join(Character).where(Character.project_id == project_id, CharacterMemory.source_scene == scene_id)).all()
        inserted_knowledge = 0
        inserted_memories = 0
        for row in knowledge:
            identity = self.parser.parse(row.proposition)
            value_fp = _fingerprint(identity["value"], "cognition-fact-value-v1") if identity else None
            index = db.scalar(select(CharacterKnowledgeSearchIndex).where(CharacterKnowledgeSearchIndex.knowledge_id == row.id))
            payload = {"knowledge": row.id, "character": row.character_id, "status": _value(row.status), "confidence": row.confidence, "identity": identity, "source": row.source, "acquired": row.acquired_at}
            values = dict(project_id=project_id, character_id=row.character_id, knowledge_id=row.id, knowledge_status=_value(row.status), confidence=row.confidence, subject_type=identity["subject_type"] if identity else None, subject_id=identity["subject_id"] if identity else None, predicate=identity["predicate"] if identity else None, value_fingerprint=value_fp, proposition_fingerprint=_fingerprint(row.proposition, "character-knowledge-proposition-v1"), source_scene_id=scene_id, acquired_at=row.acquired_at, index_fingerprint=_fingerprint(payload, COGNITION_PROTOCOL))
            if index is None:
                db.add(CharacterKnowledgeSearchIndex(**values)); inserted_knowledge += 1
            else:
                for key, value in values.items(): setattr(index, key, value)
        for row in memories:
            index = db.scalar(select(CharacterMemorySearchIndex).where(CharacterMemorySearchIndex.memory_id == row.id))
            payload = {"memory": row.id, "character": row.character_id, "importance": row.importance, "emotional": row.emotional_weight, "confidence": row.confidence, "happened": row.happened_at, "source": row.source_scene, "sequence": scene.sequence, "content": memory_content_fingerprint(row.content)}
            values = dict(project_id=project_id, character_id=row.character_id, memory_id=row.id, importance=row.importance, emotional_weight=row.emotional_weight, confidence=row.confidence, happened_at=row.happened_at, source_scene_id=row.source_scene, source_sequence=scene.sequence, source_bucket=memory_source_bucket({"memory_id": row.id, "source_scene_id": row.source_scene, "source_scene_sequence": scene.sequence}), content_fingerprint=memory_content_fingerprint(row.content), index_fingerprint=_fingerprint(payload, COGNITION_PROTOCOL))
            if index is None:
                db.add(CharacterMemorySearchIndex(**values)); inserted_memories += 1
            else:
                for key, value in values.items(): setattr(index, key, value)
            db.execute(delete(CharacterMemoryCueRef).where(CharacterMemoryCueRef.memory_id == row.id))
            for cue_type, cue_value, source_kind in self._cue_rows(row, project_id, scene):
                db.add(CharacterMemoryCueRef(project_id=project_id, character_id=row.character_id, memory_id=row.id, cue_type=cue_type, cue_value=cue_value, source=source_kind))
        links = db.scalars(select(CausalLink).where(CausalLink.project_id == project_id, CausalLink.scene_id == scene_id, CausalLink.cause_type.in_([CausalResourceType.CHARACTER_KNOWLEDGE, CausalResourceType.CHARACTER_MEMORY]), CausalLink.relation_type.in_([CausalRelationType.KNOWLEDGE_INFORMED_DECISION, CausalRelationType.MEMORY_INFORMED_DECISION]))).all()
        resource_ids = {(_value(link.cause_type), link.cause_id) for link in links}
        before_heads = {
            (row.resource_type, row.resource_id): (row.usage_count, row.latest_sequence)
            for row in db.scalars(select(CognitionUsageHead).where(
                CognitionUsageHead.project_id == project_id,
                CognitionUsageHead.resource_type.in_([kind for kind, _ in resource_ids]) if resource_ids else literal(False),
                CognitionUsageHead.resource_id.in_([ident for _, ident in resource_ids]) if resource_ids else literal(False),
            )).all()
        }
        self._refresh_usage_heads(db, project_id, resource_ids)
        db.flush()
        state.built_through_sequence = max(state.built_through_sequence, scene.sequence)
        state.indexed_knowledge_count += inserted_knowledge
        state.indexed_memory_count += inserted_memories
        after_heads = {
            (row.resource_type, row.resource_id): row
            for row in db.scalars(select(CognitionUsageHead).where(
                CognitionUsageHead.project_id == project_id,
                CognitionUsageHead.resource_type.in_([kind for kind, _ in resource_ids]) if resource_ids else literal(False),
                CognitionUsageHead.resource_id.in_([ident for _, ident in resource_ids]) if resource_ids else literal(False),
            )).all()
        }
        state.usage_head_count += sum((1 if key in after_heads else 0) - (1 if key in before_heads else 0) for key in resource_ids)
        # Full source hashes are intentionally rebuild/audit-only. Marking the
        # value unknown avoids advertising a stale whole-history hash after a
        # bounded append.
        state.source_fingerprint = None
        state.index_fingerprint = self._state_fingerprint(None, state.indexed_knowledge_count, state.indexed_memory_count, state.usage_head_count, state.built_through_sequence)
        db.flush()

    def fast_path_available(self, db: Session, project_id: str) -> bool:
        state = self._state(db, project_id)
        latest = db.scalar(select(Scene.sequence).where(Scene.project_id == project_id, Scene.history_status == "ACTIVE").order_by(Scene.sequence.desc()).limit(1)) or 0
        return bool(db.bind and db.bind.dialect.name == "postgresql" and state and state.status == RetrievalIndexStatus.READY and state.protocol_version == COGNITION_PROTOCOL and state.built_through_sequence == latest)

    def status(self, db: Session, project_id: str) -> dict[str, Any]:
        state = self._state(db, project_id)
        return {"status": _value(state.status) if state else "MISSING", "protocol": state.protocol_version if state else COGNITION_PROTOCOL, "built_through_sequence": state.built_through_sequence if state else 0, "knowledge_index_count": state.indexed_knowledge_count if state else 0, "memory_index_count": state.indexed_memory_count if state else 0, "usage_head_count": state.usage_head_count if state else 0, "fast_path_available": self.fast_path_available(db, project_id)}


class CognitionRetrievalIndexAudit:
    def audit(self, db: Session, project_id: str) -> dict[str, Any]:
        service = CognitionRetrievalProjectionService()
        state = service._state(db, project_id)
        if not state or state.status != RetrievalIndexStatus.READY:
            raise ValueError("COGNITION_RETRIEVAL_INDEX_INTEGRITY_INVALID")
        knowledge = db.scalars(select(CharacterKnowledge).join(Character).where(Character.project_id == project_id).order_by(CharacterKnowledge.id)).all()
        memories = db.scalars(select(CharacterMemory).join(Character).where(Character.project_id == project_id).order_by(CharacterMemory.id)).all()
        indexed_knowledge = {row.knowledge_id: row for row in db.scalars(select(CharacterKnowledgeSearchIndex).where(CharacterKnowledgeSearchIndex.project_id == project_id)).all()}
        indexed_memories = {row.memory_id: row for row in db.scalars(select(CharacterMemorySearchIndex).where(CharacterMemorySearchIndex.project_id == project_id)).all()}
        if set(indexed_knowledge) != {row.id for row in knowledge} or set(indexed_memories) != {row.id for row in memories}:
            raise ValueError("COGNITION_RETRIEVAL_INDEX_INTEGRITY_INVALID")
        for row in knowledge:
            index = indexed_knowledge[row.id]; identity = service.parser.parse(row.proposition)
            value_fp = _fingerprint(identity["value"], "cognition-fact-value-v1") if identity else None
            payload = {"knowledge": row.id, "character": row.character_id, "status": _value(row.status), "confidence": row.confidence, "identity": identity, "source": row.source, "acquired": row.acquired_at}
            if (index.project_id != project_id or index.character_id != row.character_id or index.knowledge_status != _value(row.status) or index.confidence != row.confidence or index.subject_type != (identity["subject_type"] if identity else None) or index.subject_id != (identity["subject_id"] if identity else None) or index.predicate != (identity["predicate"] if identity else None) or index.value_fingerprint != value_fp or index.proposition_fingerprint != _fingerprint(row.proposition, "character-knowledge-proposition-v1") or index.source_scene_id != (row.source if row.source in {scene.id for scene in db.scalars(select(Scene).where(Scene.project_id == project_id)).all()} else None) or index.acquired_at != row.acquired_at or index.index_fingerprint != _fingerprint(payload, COGNITION_PROTOCOL)):
                raise ValueError("COGNITION_RETRIEVAL_INDEX_INTEGRITY_INVALID")
        scenes = {row.id: row for row in db.scalars(select(Scene).where(Scene.project_id == project_id)).all()}
        for row in memories:
            index = indexed_memories[row.id]
            source = scenes.get(row.source_scene) if row.source_scene else None
            sequence = source.sequence if source and source.history_status == "ACTIVE" else None
            payload = {"memory": row.id, "character": row.character_id, "importance": row.importance, "emotional": row.emotional_weight, "confidence": row.confidence, "happened": row.happened_at, "source": row.source_scene, "sequence": sequence, "content": memory_content_fingerprint(row.content)}
            bucket = memory_source_bucket({"memory_id": row.id, "source_scene_id": row.source_scene, "source_scene_sequence": sequence})
            if (index.project_id != project_id or index.character_id != row.character_id or index.importance != row.importance or index.emotional_weight != row.emotional_weight or index.confidence != row.confidence or index.happened_at != row.happened_at or index.source_scene_id != row.source_scene or index.source_sequence != sequence or index.source_bucket != bucket or index.content_fingerprint != memory_content_fingerprint(row.content) or index.index_fingerprint != _fingerprint(payload, COGNITION_PROTOCOL)):
                raise ValueError("COGNITION_RETRIEVAL_INDEX_INTEGRITY_INVALID")
        actual_cues = {(row.project_id, row.character_id, row.memory_id, row.cue_type, row.cue_value, row.source) for row in db.scalars(select(CharacterMemoryCueRef).where(CharacterMemoryCueRef.project_id == project_id)).all()}
        expected_cues = {(project_id, memory.character_id, memory.id, cue_type, cue_value, source) for memory in memories for cue_type, cue_value, source in service._cue_rows(memory, project_id, scenes.get(memory.source_scene))}
        if actual_cues != expected_cues:
            raise ValueError("COGNITION_RETRIEVAL_INDEX_INTEGRITY_INVALID")
        links = db.scalars(select(CausalLink).where(CausalLink.project_id == project_id, CausalLink.active.is_(True), CausalLink.cause_type.in_([CausalResourceType.CHARACTER_KNOWLEDGE, CausalResourceType.CHARACTER_MEMORY]), CausalLink.relation_type.in_([CausalRelationType.KNOWLEDGE_INFORMED_DECISION, CausalRelationType.MEMORY_INFORMED_DECISION]))).all()
        expected_usage: dict[tuple[str, str], tuple[int, int]] = {}
        for link in links:
            key = (_value(link.cause_type), link.cause_id); count, latest = expected_usage.get(key, (0, -1)); expected_usage[key] = (count + 1, max(latest, link.sequence if link.sequence is not None else -1))
        actual_usage = {(row.resource_type, row.resource_id): (row.usage_count, row.latest_sequence, row.usage_fingerprint) for row in db.scalars(select(CognitionUsageHead).where(CognitionUsageHead.project_id == project_id)).all()}
        expected_usage_with_fp = {key: (count, latest, _fingerprint({"type": key[0], "id": key[1], "count": count, "latest": latest}, "cognition-usage-head-v1")) for key, (count, latest) in expected_usage.items()}
        latest_sequence = db.scalar(select(Scene.sequence).where(Scene.project_id == project_id, Scene.history_status == "ACTIVE").order_by(Scene.sequence.desc()).limit(1)) or 0
        expected_state_fp = service._state_fingerprint(state.source_fingerprint, len(knowledge), len(memories), len(expected_usage), latest_sequence)
        if actual_usage != expected_usage_with_fp or state.indexed_knowledge_count != len(knowledge) or state.indexed_memory_count != len(memories) or state.usage_head_count != len(expected_usage) or state.built_through_sequence != latest_sequence or state.protocol_version != COGNITION_PROTOCOL or state.index_fingerprint != expected_state_fp:
            raise ValueError("COGNITION_RETRIEVAL_INDEX_INTEGRITY_INVALID")
        if state.source_fingerprint and state.source_fingerprint != service._source_fingerprint(knowledge, memories, links):
            raise ValueError("COGNITION_RETRIEVAL_INDEX_INTEGRITY_INVALID")
        return {"valid": True, "project_id": project_id, "index_fingerprint": state.index_fingerprint}


class CharacterMemoryANNSemanticRetriever:
    """Explicit HNSW candidate discovery with full-precision exact reranking.

    This class owns no cognition authority. Every query joins formal ownership
    rows and a returned candidate list is bounded before it reaches Python.
    ``None`` means the caller must use the frozen exact pgvector path.
    """

    def __init__(self, caps: PgvectorANNCaps | None = None):
        self.caps = caps or PgvectorANNCaps()

    @staticmethod
    def _filters(project_id: str, character_id: str, query_dimension: int, config_fingerprint: str, hidden):
        return (
            CharacterMemoryEmbedding.project_id == project_id,
            CharacterMemoryEmbedding.character_id == character_id,
            CharacterMemorySearchIndex.project_id == project_id,
            CharacterMemorySearchIndex.character_id == character_id,
            Character.project_id == project_id,
            CharacterMemory.character_id == character_id,
            CharacterMemoryEmbedding.embedding_config_fingerprint == config_fingerprint,
            CharacterMemoryEmbedding.status == EmbeddingStatus.READY,
            CharacterMemoryEmbedding.dimension == query_dimension,
            CharacterMemorySearchIndex.content_fingerprint == CharacterMemoryEmbedding.content_fingerprint,
            ~hidden,
        )

    @staticmethod
    def _base(filters):
        return select(CharacterMemoryEmbedding.memory_id).join(
            CharacterMemorySearchIndex,
            and_(
                CharacterMemorySearchIndex.memory_id == CharacterMemoryEmbedding.memory_id,
                CharacterMemorySearchIndex.content_fingerprint == CharacterMemoryEmbedding.content_fingerprint,
            ),
        ).join(CharacterMemory, CharacterMemory.id == CharacterMemoryEmbedding.memory_id).join(
            Character, Character.id == CharacterMemory.character_id,
        ).where(*filters)

    def retrieve(self, db: Session, project_id: str, character_id: str, query_vector: list[float], config_fingerprint: str, top_k: int, min_similarity: float | None, config, hidden) -> list[str] | None:
        dimension = len(query_vector)
        physical = self.caps.physical_index(db, dimension)
        if not physical["physical_index_valid"]:
            return None
        filters = self._filters(project_id, character_id, dimension, config_fingerprint, hidden)
        # A bounded probe distinguishes genuinely small eligible sets from ANN
        # post-filter starvation without recounting a character's corpus.
        probe = db.scalars(self._base(filters).limit(top_k + 1)).all()
        if len(probe) <= top_k:
            return None
        candidate_limit = min(ANN_CANDIDATE_HARD_LIMIT, max(top_k, top_k * config.memory_ann_candidate_multiplier))
        try:
            # An ANN failure must leave the outer read transaction usable for
            # the exact pgvector fallback below.
            with db.begin_nested():
                db.execute(select(func.set_config("hnsw.ef_search", str(config.memory_ann_ef_search), True)))
                db.execute(select(func.set_config("hnsw.iterative_scan", "strict_order", True)))
                if physical["index_kind"] == "VECTOR":
                    from pgvector.sqlalchemy import Vector
                    vector_type = Vector(dimension)
                else:
                    from pgvector.sqlalchemy import HALFVEC
                    vector_type = HALFVEC(dimension)
                ann_distance = cast(cast(CharacterMemoryEmbedding.embedding, vector_type).op("<=>")(cast(literal(query_vector), vector_type)), Float)
                discovered = db.scalars(self._base(filters).order_by(ann_distance.asc(), CharacterMemoryEmbedding.memory_id).limit(candidate_limit)).all()
        except Exception:
            return None
        if len(discovered) < top_k:
            return None
        # The HNSW result is only a bounded discovery set. Exact original
        # vectors decide both similarity eligibility and the final rank.
        exact_distance = cast(CharacterMemoryEmbedding.embedding.op("<=>")(query_vector), Float)
        rows = db.execute(
            select(CharacterMemoryEmbedding.memory_id, exact_distance.label("distance"))
            .select_from(CharacterMemoryEmbedding)
            .join(CharacterMemorySearchIndex, and_(CharacterMemorySearchIndex.memory_id == CharacterMemoryEmbedding.memory_id, CharacterMemorySearchIndex.content_fingerprint == CharacterMemoryEmbedding.content_fingerprint))
            .join(CharacterMemory, CharacterMemory.id == CharacterMemoryEmbedding.memory_id)
            .join(Character, Character.id == CharacterMemory.character_id)
            .where(*filters, CharacterMemoryEmbedding.memory_id.in_(discovered))
            .order_by(exact_distance.asc(), CharacterMemoryEmbedding.memory_id)
        ).all()
        return [memory_id for memory_id, distance in rows if min_similarity is None or 1.0 - float(distance) >= min_similarity][:top_k]


class CurrentCharacterCognitionFastRetriever:
    """Bounded PostgreSQL read implementation for frozen deterministic mind.

    The class intentionally returns payload dictionaries, never an index-owned
    cognition representation.  Formal rows remain joined into every result.
    """

    STATUS_WEIGHT = {"KNOWN": 3, "SUSPECTED": 2, "FALSE_BELIEF": 1}

    @staticmethod
    def _hidden(project_id: str, character_id: str, resource_type: str, resource_id_column):
        return exists(select(literal(1)).where(
            RetconCognitionInvalidation.project_id == project_id,
            RetconCognitionInvalidation.character_id == character_id,
            RetconCognitionInvalidation.resource_type == resource_type,
            RetconCognitionInvalidation.resource_id == resource_id_column,
            RetconCognitionInvalidation.status.in_([RetconCognitionInvalidationStatus.ACTIVE, RetconCognitionInvalidationStatus.RESOLVED]),
        ))

    @staticmethod
    def _current_sequence(db: Session, project_id: str) -> int:
        return db.scalar(select(func.max(Scene.sequence)).where(Scene.project_id == project_id, Scene.history_status == "ACTIVE")) or 0

    def knowledge(self, db: Session, project_id: str, character_id: str, cues: dict[str, tuple[str, ...]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        current_sequence = self._current_sequence(db, project_id)
        all_cues = sorted({str(value) for values in cues.values() for value in values})
        usage = CognitionUsageHead
        cue = case((CharacterKnowledgeSearchIndex.subject_id.in_(all_cues), 1), else_=0).label("cue") if all_cues else literal(0).label("cue")
        count = func.coalesce(usage.usage_count, 0).label("usage_count")
        latest = func.coalesce(usage.latest_sequence, -1).label("latest_sequence")
        status_weight = case(
            (CharacterKnowledge.status == KnowledgeStatus.KNOWN, 3),
            (CharacterKnowledge.status == KnowledgeStatus.SUSPECTED, 2),
            (CharacterKnowledge.status == KnowledgeStatus.FALSE_BELIEF, 1), else_=0,
        )
        recency = case((latest >= 0, latest / max(1, current_sequence)), else_=0.0)
        score = (cue * 8 + status_weight * 2 + CharacterKnowledge.confidence + func.least(count, 4) + recency).label("score")
        base = select(CharacterKnowledge, cue, count, latest, score).join(Character, Character.id == CharacterKnowledge.character_id).join(CharacterKnowledgeSearchIndex, CharacterKnowledgeSearchIndex.knowledge_id == CharacterKnowledge.id).outerjoin(usage, and_(usage.project_id == project_id, usage.resource_type == CausalResourceType.CHARACTER_KNOWLEDGE.value, usage.resource_id == CharacterKnowledge.id)).where(
            CharacterKnowledgeSearchIndex.project_id == project_id,
            CharacterKnowledgeSearchIndex.character_id == character_id,
            Character.project_id == project_id,
            CharacterKnowledge.character_id == character_id,
            CharacterKnowledge.status.in_([KnowledgeStatus.KNOWN, KnowledgeStatus.SUSPECTED, KnowledgeStatus.FALSE_BELIEF]),
            ~self._hidden(project_id, character_id, "KNOWLEDGE", CharacterKnowledge.id),
        ).order_by(score.desc(), cue.desc(), count.desc(), latest.desc(), CharacterKnowledge.id).limit(32)
        rows = db.execute(base).all()
        parser = CognitionFactIdentityParser()
        recalled = [{"knowledge_id": row[0].id, "proposition": row[0].proposition, "status": _value(row[0].status), "confidence": row[0].confidence, "fact_identity": parser.parse(row[0].proposition)} for row in rows]

        # Conflict groups are selected with index SQL first, then only rows in
        # those groups are hydrated. This never restricts conflict detection to
        # the recalled top 32.
        indexed = CharacterKnowledgeSearchIndex
        eligible = select(indexed.subject_type, indexed.subject_id, indexed.predicate).join(CharacterKnowledge, CharacterKnowledge.id == indexed.knowledge_id).join(Character, Character.id == CharacterKnowledge.character_id).where(
            indexed.project_id == project_id, indexed.character_id == character_id,
            CharacterKnowledge.character_id == character_id,
            Character.project_id == project_id,
            indexed.subject_type.is_not(None), indexed.subject_id.is_not(None), indexed.predicate.is_not(None),
            CharacterKnowledge.status.in_([KnowledgeStatus.KNOWN, KnowledgeStatus.SUSPECTED, KnowledgeStatus.FALSE_BELIEF]),
            ~self._hidden(project_id, character_id, "KNOWLEDGE", CharacterKnowledge.id),
        ).group_by(indexed.subject_type, indexed.subject_id, indexed.predicate).having(func.count(func.distinct(indexed.value_fingerprint)) > 1).subquery()
        conflict_rows = db.execute(select(indexed, CharacterKnowledge).join(CharacterKnowledge, CharacterKnowledge.id == indexed.knowledge_id).join(Character, Character.id == CharacterKnowledge.character_id).join(eligible, and_(eligible.c.subject_type == indexed.subject_type, eligible.c.subject_id == indexed.subject_id, eligible.c.predicate == indexed.predicate)).where(
            indexed.project_id == project_id, indexed.character_id == character_id,
            CharacterKnowledge.character_id == character_id,
            Character.project_id == project_id,
            CharacterKnowledge.status.in_([KnowledgeStatus.KNOWN, KnowledgeStatus.SUSPECTED, KnowledgeStatus.FALSE_BELIEF]),
            ~self._hidden(project_id, character_id, "KNOWLEDGE", CharacterKnowledge.id),
        ).order_by(indexed.subject_type, indexed.subject_id, indexed.predicate, CharacterKnowledge.id)).all()
        groups: dict[tuple[str, str, str], list[str]] = {}
        for index, knowledge in conflict_rows:
            key = (index.subject_type, index.subject_id, index.predicate)
            groups.setdefault(key, []).append(knowledge.id)
        conflicts = [{"subject_type": key[0], "subject_id": key[1], "predicate": key[2], "knowledge_ids": ids} for key, ids in sorted(groups.items())]
        return recalled, conflicts

    def memories(self, db: Session, project_id: str, character_id: str, cues: dict[str, tuple[str, ...]]) -> list[dict[str, Any]]:
        current_sequence = self._current_sequence(db, project_id)
        memory_index, cue_ref, usage = CharacterMemorySearchIndex, CharacterMemoryCueRef, CognitionUsageHead
        def matched(cue_type: str, values: tuple[str, ...], distinct: bool = False):
            if not values:
                return literal(0)
            aggregate = func.count(func.distinct(cue_ref.cue_value)) if distinct else func.count(cue_ref.id)
            return func.coalesce(select(aggregate).where(cue_ref.project_id == project_id, cue_ref.character_id == character_id, cue_ref.memory_id == CharacterMemory.id, cue_ref.cue_type == cue_type, cue_ref.cue_value.in_(list(values))).scalar_subquery(), 0)
        location = case((matched("LOCATION", cues.get("location_ids", ())) > 0, 1), else_=0)
        participants = matched("PARTICIPANT", cues.get("participant_ids", ()), True)
        threads = case((matched("THREAD", cues.get("thread_ids", ())) > 0, 1), else_=0)
        entities = matched("ENTITY", cues.get("entity_ids", ()), True)
        items = matched("ITEM", cues.get("item_ids", ()), True)
        cue = (location * 4 + participants * 3 + threads * 3 + (entities + items) * 2).label("cue")
        count = func.coalesce(usage.usage_count, 0).label("usage_count")
        sequence = func.coalesce(memory_index.source_sequence, -1).label("source_sequence")
        project_time = select(Project.current_world_time).where(Project.id == project_id).scalar_subquery()
        happened_recency = case((and_(CharacterMemory.happened_at.is_not(None), project_time.is_not(None)), 1.0 / (1 + func.abs(func.extract("epoch", project_time - CharacterMemory.happened_at)) / 86400.0)), else_=0.0)
        recency = case((memory_index.source_sequence.is_not(None), 2.0 / (1 + func.greatest(0, current_sequence - memory_index.source_sequence))), else_=happened_recency)
        score = (cue + 2 * func.greatest(0.0, memory_index.importance) + func.abs(memory_index.emotional_weight) + func.greatest(0.0, memory_index.confidence) + func.least(count, 4) + recency).label("score")
        # A window captures frozen source diversity: strong memories bypass the
        # first-three cap but still consume their bucket position.
        ranked = select(CharacterMemory, memory_index.source_bucket, memory_index.source_sequence, cue, count, score, func.row_number().over(partition_by=memory_index.source_bucket, order_by=(score.desc(), cue.desc(), count.desc(), sequence.desc(), CharacterMemory.happened_at.nullsfirst(), CharacterMemory.id)).label("bucket_position")).join(Character, Character.id == CharacterMemory.character_id).join(memory_index, memory_index.memory_id == CharacterMemory.id).outerjoin(usage, and_(usage.project_id == project_id, usage.resource_type == CausalResourceType.CHARACTER_MEMORY.value, usage.resource_id == CharacterMemory.id)).where(
            memory_index.project_id == project_id, memory_index.character_id == character_id,
            Character.project_id == project_id,
            CharacterMemory.character_id == character_id,
            ~self._hidden(project_id, character_id, "MEMORY", CharacterMemory.id),
        ).subquery()
        rows = db.execute(select(ranked).where(or_(ranked.c.cue >= 4, ranked.c.bucket_position <= 3)).order_by(ranked.c.score.desc(), ranked.c.cue.desc(), ranked.c.usage_count.desc(), ranked.c.source_sequence.desc(), ranked.c.happened_at.nullsfirst(), ranked.c.id).limit(12)).mappings().all()
        result = []
        for row in rows:
            item = {"memory_id": row["id"], "content": row["content"], "importance": row["importance"], "emotional_weight": row["emotional_weight"], "confidence": row["confidence"], "distortion": row["distortion"] or {}, "happened_at": row["happened_at"].isoformat() if row["happened_at"] else None, "source_scene_id": row["source_scene"]}
            result.append(item)
        return result

    def hybrid_memories(self, db: Session, project_id: str, character_id: str, cues: dict[str, tuple[str, ...]], query_vector: list[float], config_fingerprint: str, config) -> list[dict[str, Any]]:
        """Current-only exact vector + deterministic RRF query.

        The CTEs rank the complete eligible set in PostgreSQL; Python only
        hydrates the final bounded formal rows.
        """
        if db.bind is None or db.bind.dialect.name != "postgresql":
            return self.memories(db, project_id, character_id, cues)
        memory_index, cue_ref, usage = CharacterMemorySearchIndex, CharacterMemoryCueRef, CognitionUsageHead
        current_sequence = self._current_sequence(db, project_id)
        def matched(cue_type: str, values: tuple[str, ...], distinct: bool = False):
            if not values:
                return literal(0)
            aggregate = func.count(func.distinct(cue_ref.cue_value)) if distinct else func.count(cue_ref.id)
            return func.coalesce(select(aggregate).where(cue_ref.project_id == project_id, cue_ref.character_id == character_id, cue_ref.memory_id == CharacterMemory.id, cue_ref.cue_type == cue_type, cue_ref.cue_value.in_(list(values))).scalar_subquery(), 0)
        cue = (case((matched("LOCATION", cues.get("location_ids", ())) > 0, 1), else_=0) * 4 + matched("PARTICIPANT", cues.get("participant_ids", ()), True) * 3 + case((matched("THREAD", cues.get("thread_ids", ())) > 0, 1), else_=0) * 3 + (matched("ENTITY", cues.get("entity_ids", ()), True) + matched("ITEM", cues.get("item_ids", ()), True)) * 2).label("cue")
        usage_count = func.coalesce(usage.usage_count, 0).label("usage_count")
        source_sequence = func.coalesce(memory_index.source_sequence, -1).label("source_sequence")
        project_time = select(Project.current_world_time).where(Project.id == project_id).scalar_subquery()
        happened_recency = case((and_(CharacterMemory.happened_at.is_not(None), project_time.is_not(None)), 1.0 / (1 + func.abs(func.extract("epoch", project_time - CharacterMemory.happened_at)) / 86400.0)), else_=0.0)
        recency = case((memory_index.source_sequence.is_not(None), 2.0 / (1 + func.greatest(0, current_sequence - memory_index.source_sequence))), else_=happened_recency)
        score = (cue + 2 * func.greatest(0.0, memory_index.importance) + func.abs(memory_index.emotional_weight) + func.greatest(0.0, memory_index.confidence) + func.least(usage_count, 4) + recency)
        hidden = self._hidden(project_id, character_id, "MEMORY", CharacterMemory.id)
        order = (score.desc(), cue.desc(), usage_count.desc(), source_sequence.desc(), CharacterMemory.happened_at.nullsfirst(), CharacterMemory.id)
        deterministic = select(CharacterMemory.id.label("memory_id"), score.label("score"), cue.label("cue"), usage_count.label("usage_count"), source_sequence.label("source_sequence"), memory_index.source_bucket.label("source_bucket"), func.row_number().over(order_by=order).label("det_rank"), func.row_number().over(partition_by=memory_index.source_bucket, order_by=order).label("bucket_position")).join(Character, Character.id == CharacterMemory.character_id).join(memory_index, memory_index.memory_id == CharacterMemory.id).outerjoin(usage, and_(usage.project_id == project_id, usage.resource_type == CausalResourceType.CHARACTER_MEMORY.value, usage.resource_id == CharacterMemory.id)).where(memory_index.project_id == project_id, memory_index.character_id == character_id, Character.project_id == project_id, CharacterMemory.character_id == character_id, ~hidden).subquery("deterministic_ranked")
        distance = cast(CharacterMemoryEmbedding.embedding.op("<=>")(query_vector), Float)
        semantic_filters = (
            CharacterMemoryEmbedding.project_id == project_id,
            CharacterMemoryEmbedding.character_id == character_id,
            CharacterMemorySearchIndex.character_id == character_id,
            Character.project_id == project_id,
            CharacterMemory.character_id == character_id,
            CharacterMemoryEmbedding.embedding_config_fingerprint == config_fingerprint,
            CharacterMemoryEmbedding.status == EmbeddingStatus.READY,
            CharacterMemoryEmbedding.dimension == len(query_vector),
            ~hidden,
        )
        ann_ids = None
        if _value(getattr(config, "memory_vector_search_mode", MemoryVectorSearchMode.EXACT)) == MemoryVectorSearchMode.ANN.value:
            ann_ids = CharacterMemoryANNSemanticRetriever().retrieve(
                db, project_id, character_id, query_vector, config_fingerprint,
                config.memory_vector_top_k, config.memory_semantic_min_similarity,
                config, hidden,
            )
        if ann_ids:
            # ``ann_ids`` already has an exact full-vector rerank. A CASE rank
            # retains that order in the frozen SQL RRF calculation.
            rank = case({memory_id: position for position, memory_id in enumerate(ann_ids, 1)}, value=CharacterMemoryEmbedding.memory_id).label("sem_rank")
            semantic_ranked = select(CharacterMemoryEmbedding.memory_id.label("memory_id"), rank).join(CharacterMemorySearchIndex, and_(CharacterMemorySearchIndex.memory_id == CharacterMemoryEmbedding.memory_id, CharacterMemorySearchIndex.project_id == project_id, CharacterMemorySearchIndex.content_fingerprint == CharacterMemoryEmbedding.content_fingerprint)).join(CharacterMemory, CharacterMemory.id == CharacterMemoryEmbedding.memory_id).join(Character, Character.id == CharacterMemory.character_id).where(*semantic_filters, CharacterMemoryEmbedding.memory_id.in_(ann_ids)).subquery("semantic_ranked_all")
            semantic = select(semantic_ranked).subquery("semantic_ranked")
        else:
            semantic_ranked = select(CharacterMemoryEmbedding.memory_id.label("memory_id"), func.row_number().over(order_by=(distance.asc(), CharacterMemoryEmbedding.memory_id)).label("sem_rank")).join(CharacterMemorySearchIndex, and_(CharacterMemorySearchIndex.memory_id == CharacterMemoryEmbedding.memory_id, CharacterMemorySearchIndex.project_id == project_id, CharacterMemorySearchIndex.content_fingerprint == CharacterMemoryEmbedding.content_fingerprint)).join(CharacterMemory, CharacterMemory.id == CharacterMemoryEmbedding.memory_id).join(Character, Character.id == CharacterMemory.character_id).where(*semantic_filters, (literal(1.0) - distance) >= config.memory_semantic_min_similarity if config.memory_semantic_min_similarity is not None else literal(True)).subquery("semantic_ranked_all")
            semantic = select(semantic_ranked).where(semantic_ranked.c.sem_rank <= config.memory_vector_top_k).subquery("semantic_ranked")
        # semantic eligibility is intentionally a subset of deterministic
        # current eligibility, so every vector candidate already has a source
        # bucket and deterministic rank. No semantic-only Python hydration is
        # needed.
        fused_raw = select(deterministic.c.memory_id, (1.0 / (config.memory_rrf_k + deterministic.c.det_rank) + func.coalesce(1.0 / (config.memory_rrf_k + semantic.c.sem_rank), 0.0)).label("rrf_score"), deterministic.c.cue, deterministic.c.source_bucket).outerjoin(semantic, semantic.c.memory_id == deterministic.c.memory_id).subquery("fused_raw")
        fused = select(fused_raw, func.row_number().over(partition_by=fused_raw.c.source_bucket, order_by=(fused_raw.c.rrf_score.desc(), fused_raw.c.memory_id)).label("bucket_position")).subquery("fused")
        rows = db.execute(select(fused).where(or_(fused.c.cue >= 4, fused.c.bucket_position <= 3)).order_by(fused.c.rrf_score.desc(), fused.c.memory_id).limit(12)).mappings().all()
        ids = [row["memory_id"] for row in rows]
        if not ids:
            return []
        formal = {row.id: row for row in db.scalars(select(CharacterMemory).join(Character, Character.id == CharacterMemory.character_id).where(CharacterMemory.id.in_(ids), CharacterMemory.character_id == character_id, Character.project_id == project_id)).all()}
        result = []
        for memory_id in ids:
            row = formal[memory_id]
            result.append({"memory_id": row.id, "content": row.content, "importance": row.importance, "emotional_weight": row.emotional_weight, "confidence": row.confidence, "distortion": row.distortion or {}, "happened_at": row.happened_at.isoformat() if row.happened_at else None, "source_scene_id": row.source_scene})
        return result


class ResearchLexicalIndexService:
    @staticmethod
    def _state_fingerprint(corpus: str, chunks: int, tokens: int, postings: int, terms: int) -> str:
        return _fingerprint({"corpus": corpus, "chunks": chunks, "tokens": tokens, "postings": postings, "terms": terms}, RESEARCH_PROTOCOL)
    def _state(self, db: Session, project_id: str) -> ResearchLexicalIndexState | None:
        return db.scalar(select(ResearchLexicalIndexState).where(ResearchLexicalIndexState.project_id == project_id))

    def mark_dirty(self, db: Session, project_id: str) -> None:
        state = self._state(db, project_id)
        if state is None:
            db.add(ResearchLexicalIndexState(project_id=project_id, status=RetrievalIndexStatus.DIRTY, protocol_version=RESEARCH_PROTOCOL))
        else:
            state.status = RetrievalIndexStatus.DIRTY
        db.flush()

    def rebuild(self, db: Session, project_id: str) -> ResearchLexicalIndexState:
        from .research import KnowledgeTokenizer, ResearchCorpusFingerprintBuilder
        _acquire_research_index_lock(db, project_id)
        state = self._state(db, project_id)
        if state is None:
            state = ResearchLexicalIndexState(project_id=project_id, status=RetrievalIndexStatus.REBUILDING, protocol_version=RESEARCH_PROTOCOL)
            db.add(state); db.flush()
        else:
            state.status = RetrievalIndexStatus.REBUILDING
        db.execute(delete(ResearchTermPosting).where(ResearchTermPosting.project_id == project_id))
        db.execute(delete(ResearchTermStat).where(ResearchTermStat.project_id == project_id))
        db.execute(delete(ResearchChunkLexicalIndex).where(ResearchChunkLexicalIndex.project_id == project_id))
        rows = db.execute(select(ResearchDocument, ResearchDocumentRevision, ResearchChunk).join(ResearchDocumentRevision, ResearchDocumentRevision.document_id == ResearchDocument.id).join(ResearchChunk, ResearchChunk.revision_id == ResearchDocumentRevision.id).where(ResearchDocument.project_id == project_id, ResearchDocument.active.is_(True), ResearchDocumentRevision.active.is_(True), ResearchChunk.active.is_(True)).order_by(ResearchChunk.id)).all()
        tokenizer, dfs, total = KnowledgeTokenizer(), Counter(), 0
        for document, revision, chunk in rows:
            tokens = tokenizer.tokenize(chunk.content); counts = Counter(tokens); total += len(tokens)
            payload = {"chunk": chunk.id, "content": chunk.content_fingerprint, "tokens": len(tokens)}
            db.add(ResearchChunkLexicalIndex(project_id=project_id, document_id=document.id, revision_id=revision.id, chunk_id=chunk.id, content_fingerprint=chunk.content_fingerprint, token_count=len(tokens), index_fingerprint=_fingerprint(payload, RESEARCH_PROTOCOL)))
            for term, frequency in sorted(counts.items()):
                db.add(ResearchTermPosting(project_id=project_id, chunk_id=chunk.id, term=term, term_frequency=frequency)); dfs[term] += 1
        for term, df in sorted(dfs.items()):
            db.add(ResearchTermStat(project_id=project_id, term=term, document_frequency=df))
        db.flush()
        count = len(rows); corpus = ResearchCorpusFingerprintBuilder().build(db, project_id)
        state.status, state.protocol_version, state.corpus_fingerprint = RetrievalIndexStatus.READY, RESEARCH_PROTOCOL, corpus
        state.active_chunk_count, state.total_token_count, state.average_document_length = count, total, total / count if count else 1.0
        state.posting_count, state.term_count = sum(dfs.values()), len(dfs)
        state.index_fingerprint = self._state_fingerprint(corpus, count, total, sum(dfs.values()), len(dfs))
        state.last_rebuilt_at = datetime.utcnow(); db.flush()
        return state

    def sync_after_ingestion(self, db: Session, project_id: str, document_id: str | None = None) -> None:
        """Replace one document's postings and update global stats in SQL."""
        from .research import KnowledgeTokenizer, ResearchCorpusFingerprintBuilder
        _acquire_research_index_lock(db, project_id)
        state = self._state(db, project_id)
        if state is None or state.status != RetrievalIndexStatus.READY or not document_id:
            self.rebuild(db, project_id)
            return
        document = db.get(ResearchDocument, document_id)
        if not document or document.project_id != project_id:
            raise ValueError("RESEARCH_LEXICAL_INDEX_DOCUMENT_INVALID")
        old_indexes = db.scalars(select(ResearchChunkLexicalIndex).where(ResearchChunkLexicalIndex.project_id == project_id, ResearchChunkLexicalIndex.document_id == document_id)).all()
        old_chunk_ids = [row.chunk_id for row in old_indexes]
        old_term_counts = Counter()
        old_posting_count = 0
        if old_chunk_ids:
            for term, count in db.execute(select(ResearchTermPosting.term, func.count(func.distinct(ResearchTermPosting.chunk_id))).where(ResearchTermPosting.project_id == project_id, ResearchTermPosting.chunk_id.in_(old_chunk_ids)).group_by(ResearchTermPosting.term)).all():
                old_term_counts[term] = count
            old_posting_count = db.scalar(select(func.count(ResearchTermPosting.id)).where(ResearchTermPosting.project_id == project_id, ResearchTermPosting.chunk_id.in_(old_chunk_ids))) or 0
        old_chunk_count, old_token_total = len(old_indexes), sum(row.token_count for row in old_indexes)
        stale_chunks = select(ResearchChunkLexicalIndex.chunk_id).where(ResearchChunkLexicalIndex.project_id == project_id, ResearchChunkLexicalIndex.document_id == document_id)
        db.execute(delete(ResearchTermPosting).where(ResearchTermPosting.project_id == project_id, ResearchTermPosting.chunk_id.in_(stale_chunks)))
        db.execute(delete(ResearchChunkLexicalIndex).where(ResearchChunkLexicalIndex.project_id == project_id, ResearchChunkLexicalIndex.document_id == document_id))
        rows = db.execute(select(ResearchDocumentRevision, ResearchChunk).join(ResearchChunk, ResearchChunk.revision_id == ResearchDocumentRevision.id).where(ResearchDocumentRevision.document_id == document_id, ResearchDocumentRevision.active.is_(True), ResearchChunk.active.is_(True)).order_by(ResearchChunk.id)).all()
        tokenizer = KnowledgeTokenizer(); new_term_counts = Counter(); new_posting_count = 0; new_token_total = 0
        for revision, chunk in rows:
            tokens = tokenizer.tokenize(chunk.content); counts = Counter(tokens); token_count = len(tokens)
            new_token_total += token_count; new_posting_count += len(counts); new_term_counts.update(counts.keys())
            db.add(ResearchChunkLexicalIndex(project_id=project_id, document_id=document_id, revision_id=revision.id, chunk_id=chunk.id, content_fingerprint=chunk.content_fingerprint, token_count=token_count, index_fingerprint=_fingerprint({"chunk": chunk.id, "content": chunk.content_fingerprint, "tokens": token_count}, RESEARCH_PROTOCOL)))
            for term, frequency in sorted(counts.items()):
                db.add(ResearchTermPosting(project_id=project_id, chunk_id=chunk.id, term=term, term_frequency=frequency))
        db.flush()
        affected_terms = sorted(set(old_term_counts) | set(new_term_counts))
        existing_stats = {row.term: row for row in db.scalars(select(ResearchTermStat).where(ResearchTermStat.project_id == project_id, ResearchTermStat.term.in_(affected_terms))).all()} if affected_terms else {}
        term_delta = 0
        for term in affected_terms:
            previous = existing_stats.get(term)
            global_df = (previous.document_frequency if previous else 0) - old_term_counts[term] + new_term_counts[term]
            if global_df <= 0:
                if previous:
                    db.delete(previous); term_delta -= 1
            elif previous:
                previous.document_frequency = global_df
            else:
                db.add(ResearchTermStat(project_id=project_id, term=term, document_frequency=global_df)); term_delta += 1
        state.status = RetrievalIndexStatus.READY
        state.corpus_fingerprint = ResearchCorpusFingerprintBuilder().build(db, project_id)
        state.active_chunk_count += len(rows) - old_chunk_count
        state.total_token_count += new_token_total - old_token_total
        state.average_document_length = state.total_token_count / state.active_chunk_count if state.active_chunk_count else 1.0
        state.posting_count += new_posting_count - old_posting_count
        state.term_count += term_delta
        state.index_fingerprint = self._state_fingerprint(state.corpus_fingerprint, state.active_chunk_count, state.total_token_count, state.posting_count, state.term_count)
        db.flush()

    def fast_path_available(self, db: Session, project_id: str) -> bool:
        state = self._state(db, project_id)
        return bool(db.bind and db.bind.dialect.name == "postgresql" and state and state.status == RetrievalIndexStatus.READY and state.protocol_version == RESEARCH_PROTOCOL)

    def status(self, db: Session, project_id: str) -> dict[str, Any]:
        state = self._state(db, project_id)
        return {"status": _value(state.status) if state else "MISSING", "protocol": state.protocol_version if state else RESEARCH_PROTOCOL, "active_chunk_count": state.active_chunk_count if state else 0, "posting_count": state.posting_count if state else 0, "term_count": state.term_count if state else 0, "corpus_fingerprint": state.corpus_fingerprint if state else None, "fast_path_available": self.fast_path_available(db, project_id)}


class ResearchLexicalIndexAudit:
    def audit(self, db: Session, project_id: str) -> dict[str, Any]:
        from .research import KnowledgeTokenizer, ResearchCorpusFingerprintBuilder
        service = ResearchLexicalIndexService(); state = service._state(db, project_id)
        if not state or state.status != RetrievalIndexStatus.READY:
            raise ValueError("RESEARCH_LEXICAL_INDEX_INTEGRITY_INVALID")
        chunks = db.scalars(select(ResearchChunk).join(ResearchDocumentRevision).join(ResearchDocument).where(ResearchChunk.project_id == project_id, ResearchChunk.active.is_(True), ResearchDocumentRevision.active.is_(True), ResearchDocument.active.is_(True))).all()
        indexes = {row.chunk_id: row for row in db.scalars(select(ResearchChunkLexicalIndex).where(ResearchChunkLexicalIndex.project_id == project_id)).all()}
        if set(indexes) != {row.id for row in chunks}:
            raise ValueError("RESEARCH_LEXICAL_INDEX_INTEGRITY_INVALID")
        expected = Counter(); tokenizer = KnowledgeTokenizer()
        for chunk in chunks:
            index = indexes[chunk.id]
            token_count = len(tokenizer.tokenize(chunk.content))
            expected_index_fp = _fingerprint({"chunk": chunk.id, "content": chunk.content_fingerprint, "tokens": token_count}, RESEARCH_PROTOCOL)
            if index.project_id != project_id or index.document_id != chunk.document_id or index.revision_id != chunk.revision_id or index.content_fingerprint != chunk.content_fingerprint or index.token_count != token_count or index.index_fingerprint != expected_index_fp:
                raise ValueError("RESEARCH_LEXICAL_INDEX_INTEGRITY_INVALID")
            expected.update(set(tokenizer.tokenize(chunk.content)))
        actual = {row.term: row.document_frequency for row in db.scalars(select(ResearchTermStat).where(ResearchTermStat.project_id == project_id)).all()}
        postings = {(row.chunk_id, row.term): row.term_frequency for row in db.scalars(select(ResearchTermPosting).where(ResearchTermPosting.project_id == project_id)).all()}
        expected_postings = {(chunk.id, term): count for chunk in chunks for term, count in Counter(tokenizer.tokenize(chunk.content)).items()}
        corpus = ResearchCorpusFingerprintBuilder().build(db, project_id)
        total_tokens = sum(index.token_count for index in indexes.values())
        expected_state_fp = ResearchLexicalIndexService._state_fingerprint(corpus, len(chunks), total_tokens, len(postings), len(expected))
        if actual != dict(expected) or postings != expected_postings or state.corpus_fingerprint != corpus or state.active_chunk_count != len(chunks) or state.total_token_count != total_tokens or state.posting_count != len(postings) or state.term_count != len(expected) or state.index_fingerprint != expected_state_fp:
            raise ValueError("RESEARCH_LEXICAL_INDEX_INTEGRITY_INVALID")
        return {"valid": True, "project_id": project_id, "index_fingerprint": state.index_fingerprint}


class ResearchIndexedBM25Retriever:
    """Posting-list BM25 for the normal PostgreSQL search path.

    The exact tokenizer remains authoritative.  Only query terms are
    tokenized here; document-length and document-frequency values come from
    the derived posting tables constrained to the same filtered corpus.
    """

    def search(self, db: Session, project_id: str, query_text: str, *, filters: dict[str, Any], config) -> list[Any]:
        from .research import (
            KnowledgeAuthorityResolver, KnowledgeTokenizer, ResearchDiversifier,
            ResearchDomainError, ResearchFilters, ResearchHit, ResearchSourcePolicy,
            _safe_metadata, _safe_source_uri, _value,
        )
        tokens = KnowledgeTokenizer().tokenize(query_text or "")
        if not tokens:
            raise ResearchDomainError("RESEARCH_QUERY_EMPTY")
        if filters.get("tags"):
            from sqlalchemy.dialects.postgresql import JSONB
            requested_tags = list(filters["tags"])
            doc_tag_values = func.jsonb_array_elements_text(cast(ResearchDocument.source_metadata["tags"], JSONB)).table_valued("value").alias("doc_tag_values")
            chunk_tag_values = func.jsonb_array_elements_text(cast(ResearchChunk.chunk_metadata["tags"], JSONB)).table_valued("value").alias("chunk_tag_values")
            tag_match = or_(
                exists(select(literal(1)).select_from(doc_tag_values).where(doc_tag_values.c.value.in_(requested_tags))),
                exists(select(literal(1)).select_from(chunk_tag_values).where(chunk_tag_values.c.value.in_(requested_tags))),
            )
        else:
            tag_match = None
        clauses = [
            ResearchDocument.project_id == project_id, ResearchDocument.active.is_(True),
            ResearchDocumentRevision.active.is_(True), ResearchChunk.active.is_(True),
        ]
        if filters.get("document_ids"):
            clauses.append(ResearchDocument.id.in_(filters["document_ids"]))
        if filters.get("source_tiers"):
            clauses.append(ResearchDocument.source_tier.in_(filters["source_tiers"]))
        if filters.get("source_kinds"):
            clauses.append(ResearchDocument.source_kind.in_(filters["source_kinds"]))
        if tag_match is not None:
            clauses.append(tag_match)
        terms = sorted(set(tokens))
        has_corpus_filters = any(filters.get(key) for key in ("document_ids", "source_tiers", "source_kinds", "tags"))
        if not has_corpus_filters:
            # READY state and term stats are the synchronized current-corpus
            # authority for the unfiltered path.  This avoids recounting every
            # active chunk on ordinary queries while candidate SQL still
            # validates formal active document/revision/chunk ownership.
            state = db.scalar(select(ResearchLexicalIndexState).where(
                ResearchLexicalIndexState.project_id == project_id,
                ResearchLexicalIndexState.status == RetrievalIndexStatus.READY,
                ResearchLexicalIndexState.protocol_version == RESEARCH_PROTOCOL,
            ))
            if state is None:
                raise ResearchDomainError("RESEARCH_LEXICAL_INDEX_NOT_READY")
            n, avgdl = state.active_chunk_count, state.average_document_length
            if not n:
                return []
            df_rows = db.execute(select(ResearchTermStat.term, ResearchTermStat.document_frequency).where(
                ResearchTermStat.project_id == project_id,
                ResearchTermStat.term.in_(terms),
            )).all()
        else:
            base = select(ResearchChunk.id.label("chunk_id"), ResearchChunk.token_count.label("token_count")).join(ResearchDocumentRevision, ResearchDocumentRevision.id == ResearchChunk.revision_id).join(ResearchDocument, ResearchDocument.id == ResearchChunk.document_id).where(*clauses).subquery()
            n, total = db.execute(select(func.count(base.c.chunk_id), func.coalesce(func.sum(base.c.token_count), 0))).one()
            if not n:
                return []
            avgdl = total / n
            df_rows = db.execute(select(ResearchTermPosting.term, func.count(func.distinct(ResearchTermPosting.chunk_id))).join(base, base.c.chunk_id == ResearchTermPosting.chunk_id).where(ResearchTermPosting.project_id == project_id, ResearchTermPosting.term.in_(terms)).group_by(ResearchTermPosting.term)).all()
        df = {term: count for term, count in df_rows}
        posting_rows = db.execute(select(ResearchDocument, ResearchDocumentRevision, ResearchChunk, ResearchTermPosting.term, ResearchTermPosting.term_frequency).join(ResearchDocumentRevision, ResearchDocumentRevision.id == ResearchChunk.revision_id).join(ResearchDocument, ResearchDocument.id == ResearchChunk.document_id).join(ResearchTermPosting, ResearchTermPosting.chunk_id == ResearchChunk.id).where(*clauses, ResearchTermPosting.project_id == project_id, ResearchTermPosting.term.in_(terms)).order_by(ResearchChunk.id, ResearchTermPosting.term)).all()
        grouped: dict[str, dict[str, Any]] = {}
        for document, revision, chunk, term, frequency in posting_rows:
            item = grouped.setdefault(chunk.id, {"document": document, "revision": revision, "chunk": chunk, "counts": {}})
            item["counts"][term] = frequency
        authority = KnowledgeAuthorityResolver(); candidates = []
        for item in grouped.values():
            document, chunk, counts = item["document"], item["chunk"], item["counts"]
            ResearchSourcePolicy().validate(document.source_tier, document.source_kind); _safe_source_uri(document.source_uri); _safe_metadata(document.source_metadata)
            score = 0.0
            for term in terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                term_df = df.get(term, 0)
                idf = __import__("math").log(1 + (n - term_df + 0.5) / (term_df + 0.5))
                score += idf * (frequency * (config.bm25_k1 + 1)) / (frequency + config.bm25_k1 * (1 - config.bm25_b + config.bm25_b * chunk.token_count / avgdl))
            if score > 0:
                candidates.append({"document": document, "revision": item["revision"], "chunk": chunk, "document_id": document.id, "ordinal": chunk.ordinal, "content": chunk.content, "content_fingerprint": chunk.content_fingerprint, "score": score, "authority_rank": authority.rank(authority.for_research(document.source_tier))})
        candidates.sort(key=lambda item: (-item["score"], item["authority_rank"], item["content_fingerprint"], item["document_id"], item["ordinal"]))
        if config.deduplicate_exact:
            seen: set[str] = set(); candidates = [item for item in candidates if not (item["content_fingerprint"] in seen or seen.add(item["content_fingerprint"]))]
        # Candidate token sets are reconstructed from postings rather than by
        # re-tokenizing text during every search. This preserves the frozen
        # Jaccard semantics while keeping normal search corpus-independent.
        candidate_ids = [item["chunk"].id for item in candidates]
        token_sets: dict[str, set[str]] = {ident: set() for ident in candidate_ids}
        if candidate_ids:
            for chunk_id, term in db.execute(select(ResearchTermPosting.chunk_id, ResearchTermPosting.term).where(ResearchTermPosting.project_id == project_id, ResearchTermPosting.chunk_id.in_(candidate_ids))).all():
                token_sets[chunk_id].add(term)
        remaining, selected, per_document = list(candidates), [], {}
        while remaining and len(selected) < config.top_k:
            allowed = [item for item in remaining if per_document.get(item["document_id"], 0) < config.per_document_limit]
            if not allowed:
                break
            if not selected:
                best = allowed[0]
            else:
                scored = []
                for item in allowed:
                    source = token_sets.get(item["chunk"].id, set())
                    similarity = max((len(source & token_sets.get(other["chunk"].id, set())) / len(source | token_sets.get(other["chunk"].id, set())) if source | token_sets.get(other["chunk"].id, set()) else 0.0 for other in selected), default=0.0)
                    mmr = config.diversity_lambda * item["score"] - (1 - config.diversity_lambda) * similarity
                    scored.append((mmr, item["score"], item["authority_rank"], item["content_fingerprint"], item["document_id"], item["ordinal"], item))
                best = min(scored, key=lambda row: (-row[0], -row[1], row[2], "" if row[3] is None else row[3], row[4], row[5]))[-1]
            selected.append(best); remaining.remove(best); per_document[best["document_id"]] = per_document.get(best["document_id"], 0) + 1
        hits, total_chars = [], 0
        for rank, item in enumerate(selected, 1):
            remaining = config.max_context_chars - total_chars
            if remaining <= 0:
                break
            content, truncated = item["content"], False
            if len(content) > remaining:
                content, truncated = content[:remaining], True
            total_chars += len(content); document, revision, chunk = item["document"], item["revision"], item["chunk"]
            hits.append(ResearchHit(chunk.id, document.id, revision.id, document.title, _value(document.source_tier), _value(document.source_kind), float(item["score"]), rank, content, item["content_fingerprint"], document.source_uri, _safe_metadata(document.source_metadata, reject=False), item["authority_rank"], truncated))
        return hits
