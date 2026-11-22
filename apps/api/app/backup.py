"""Author-facing project backup and restore.

Archives contain authored story material and configuration metadata, never
provider credentials or derived search indexes. Restores always create a new
project so an import cannot overwrite the author's current book.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, select
from sqlalchemy.orm import Session

from .db import Base
from .models import Project

ARCHIVE_FORMAT = "nwe-author-project-v1"
SCHEMA_VERSION = 2
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
AUTHOR_TABLES = {
    "projects", "project_model_configs", "writing_bibles", "anti_ai_bibles",
    "canon_facts", "world_entities", "characters", "character_knowledge",
    "character_memories", "reveal_constraints", "story_threads", "story_arcs",
    "scenes", "chapters", "story_plans", "story_plan_volumes", "story_plan_arcs",
    "story_plan_chapters", "research_documents", "research_document_revisions",
    "research_chunks",
    "book_contracts", "volume_contracts", "chapter_planning_windows",
    "volume_continuity_snapshots", "foreshadowing_ledger", "author_guidance",
    "book_completion_proposals", "chapter_writer_drafts",
    "chapter_quality_assessments", "chapter_quality_findings",
    "narrative_structure_revisions", "chapter_scene_bindings", "narrative_arcs",
    "narrative_arc_chapter_bindings", "narrative_volumes",
    "narrative_volume_arc_bindings",
    # Persist the evidence used to form chapters and construct a Writer's safe
    # context. Search projections and runnable worker queues are not archives.
    "scene_proposals", "character_decisions", "scene_performances",
    "scene_performance_turns", "world_resolutions", "scene_execution_bindings",
    "timeline_events", "causal_links", "world_snapshots", "scene_state_checkpoints",
    "scene_commits", "state_delta_batches", "state_delta_items",
}

# These rows have no project_id. Their parent is the ownership boundary; joining
# them through that parent prevents another project's history entering an export.
OWNERSHIP_PARENTS = {
    "character_knowledge": ("character_id", "characters"),
    "character_memories": ("character_id", "characters"),
    "story_plan_volumes": ("plan_id", "story_plans"),
    "story_plan_arcs": ("plan_id", "story_plans"),
    "chapter_scene_bindings": ("chapter_id", "chapters"),
    "narrative_arc_chapter_bindings": ("narrative_arc_id", "narrative_arcs"),
    "narrative_volume_arc_bindings": ("volume_id", "narrative_volumes"),
    "chapter_quality_findings": ("assessment_id", "chapter_quality_assessments"),
}

# The schema also stores a few references without SQL foreign keys. Unlike
# descriptive prose, these must never keep pointing at the source project.
SOFT_REFERENCES = {
    ("volume_contracts", "sealed_snapshot_id"): "volume_continuity_snapshots",
    ("chapter_planning_windows", "source_volume_snapshot_id"): "volume_continuity_snapshots",
    ("character_memories", "source_scene"): "scenes",
    ("scenes", "superseded_by_scene_id"): "scenes",
    ("scene_state_checkpoints", "current_scene_id"): "scenes",
    ("scene_proposals", "location_id"): "world_entities",
    ("character_decisions", "target_character_id"): "characters",
    ("character_decisions", "target_entity_id"): "world_entities",
}
JSON_REFERENCES = {
    "character_id": {"characters"}, "actor_character_id": {"characters"},
    "pov_character_id": {"characters"}, "target_character_id": {"characters"},
    "character_ids": {"characters"}, "related_character_ids": {"characters"},
    "participant_ids": {"characters"}, "recipient_character_ids": {"characters"},
    "participants": {"characters"}, "cast_refs": {"characters"},
    "location_id": {"world_entities"}, "entity_id": {"world_entities"},
    "scene_id": {"scenes"}, "source_scene_id": {"scenes"},
    "source_scene_ids": {"scenes"}, "scene_coverage": {"scenes"},
    "chapter_id": {"chapters"}, "source_chapter_id": {"chapters"},
    "evidence_chapter_ids": {"chapters"}, "chapter_ids": {"chapters"},
    "book_contract_id": {"book_contracts"},
    "volume_id": {"volume_contracts", "narrative_volumes"},
    "source_volume_id": {"volume_contracts"},
    "window_id": {"chapter_planning_windows"}, "author_window_id": {"chapter_planning_windows"},
    "source_volume_snapshot_id": {"volume_continuity_snapshots"},
    "knowledge_id": {"character_knowledge"}, "memory_id": {"character_memories"},
    "fact_id": {"canon_facts"}, "related_fact_ids": {"canon_facts"},
    "canon_fact_ids_used": {"canon_facts"}, "world_entity_ids_used": {"world_entities"},
    "participant_order": {"characters"}, "active_participant_ids": {"characters"},
}


def _json(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _fingerprint(payload: dict[str, Any]) -> str:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "nwe-author-backup-v1:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _mapper_by_table() -> dict[str, Any]:
    return {mapper.local_table.name: mapper for mapper in Base.registry.mappers}


def _row(mapper: Any, item: Any) -> dict[str, Any]:
    return {column.name: _json(getattr(item, mapper.get_property_by_column(column).key)) for column in mapper.local_table.columns}


def _uuid(value: Any) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


class ProjectBackupService:
    def export(self, db: Session, project_id: str) -> dict[str, Any]:
        # SQLite's legacy transaction mode does not start a read transaction for
        # SELECT alone. A savepoint gives every table the same read snapshot.
        with db.begin_nested():
            return self._export(db, project_id)

    def _export(self, db: Session, project_id: str) -> dict[str, Any]:
        from .models import ChapterWorldBranch, ChapterWorldBranchStatus
        project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
        if not project:
            raise ValueError("PROJECT_NOT_FOUND")
        mappers = _mapper_by_table()
        tables: dict[str, list[dict[str, Any]]] = {}
        candidate_branches = db.scalars(select(ChapterWorldBranch).where(
            ChapterWorldBranch.project_id == project_id,
            ChapterWorldBranch.status != ChapterWorldBranchStatus.ADOPTED,
        )).all()
        candidate_branch_ids = {item.id for item in candidate_branches}
        candidate_chapter_ids = {item.chapter_id for item in candidate_branches if item.chapter_id}
        candidate_scene_ids = {scene_id for item in candidate_branches for scene_id in (item.scene_ids or [])}
        candidate_performance_ids = {performance_id for item in candidate_branches for performance_id in (item.performance_ids or [])}
        candidate_proposal_ids = set(db.scalars(select(mappers["scene_performances"].class_.scene_proposal_id).where(
            mappers["scene_performances"].class_.id.in_(candidate_performance_ids),
        )).all()) if candidate_performance_ids else set()
        candidate_turn_ids = set(db.scalars(select(mappers["scene_performance_turns"].class_.id).where(
            mappers["scene_performance_turns"].class_.performance_id.in_(candidate_performance_ids),
        )).all()) if candidate_performance_ids else set()
        candidate_resolution_ids = set(db.scalars(select(mappers["world_resolutions"].class_.id).where(
            mappers["world_resolutions"].class_.performance_id.in_(candidate_performance_ids),
        )).all()) if candidate_performance_ids else set()
        candidate_batch_ids = set(db.scalars(select(mappers["state_delta_batches"].class_.id).where(
            mappers["state_delta_batches"].class_.source_performance_id.in_(candidate_performance_ids),
        )).all()) if candidate_performance_ids else set()
        if candidate_resolution_ids:
            candidate_batch_ids.update(db.scalars(select(mappers["state_delta_batches"].class_.id).where(
                mappers["state_delta_batches"].class_.source_resolution_id.in_(candidate_resolution_ids),
            )).all())
        candidate_draft_ids = set()
        for draft in db.scalars(select(mappers["chapter_writer_drafts"].class_).where(
            mappers["chapter_writer_drafts"].class_.project_id == project_id,
        )).all():
            if draft.chapter_id in candidate_chapter_ids or (draft.source_manifest or {}).get("world_branch_id") in candidate_branch_ids:
                candidate_draft_ids.add(draft.id)
        candidate_assessment_ids = set(db.scalars(select(mappers["chapter_quality_assessments"].class_.id).where(
            mappers["chapter_quality_assessments"].class_.writer_draft_id.in_(candidate_draft_ids),
        )).all()) if candidate_draft_ids else set()
        # Direct project ownership is the authoritative inclusion rule.
        for table_name in sorted(AUTHOR_TABLES):
            mapper = mappers.get(table_name)
            if not mapper:
                continue
            model = mapper.class_
            if table_name == "projects":
                rows = [project]
            elif "project_id" in mapper.local_table.c:
                query = select(model).where(model.project_id == project_id)
                if table_name in {"scenes", "chapters"} and "world_branch_id" in mapper.local_table.c:
                    query = query.where(model.world_branch_id.is_(None))
                if table_name == "scene_proposals" and candidate_proposal_ids:
                    query = query.where(model.id.not_in(candidate_proposal_ids))
                if table_name == "scene_performances" and candidate_performance_ids:
                    query = query.where(model.id.not_in(candidate_performance_ids))
                if table_name == "scene_performance_turns" and candidate_performance_ids:
                    query = query.where(model.performance_id.not_in(candidate_performance_ids))
                if table_name == "character_decisions" and candidate_proposal_ids:
                    query = query.where(model.scene_proposal_id.not_in(candidate_proposal_ids))
                if table_name == "world_resolutions" and candidate_performance_ids:
                    query = query.where(model.performance_id.not_in(candidate_performance_ids))
                if table_name == "scene_execution_bindings" and candidate_scene_ids:
                    query = query.where(model.scene_id.not_in(candidate_scene_ids))
                if table_name == "state_delta_batches" and candidate_batch_ids:
                    query = query.where(model.id.not_in(candidate_batch_ids))
                if table_name == "state_delta_items" and candidate_batch_ids:
                    query = query.where(model.batch_id.not_in(candidate_batch_ids))
                if table_name == "chapter_writer_drafts" and candidate_draft_ids:
                    query = query.where(model.id.not_in(candidate_draft_ids))
                if table_name == "chapter_quality_assessments" and candidate_assessment_ids:
                    query = query.where(model.id.not_in(candidate_assessment_ids))
                if table_name == "chapter_quality_findings" and candidate_assessment_ids:
                    query = query.where(model.assessment_id.not_in(candidate_assessment_ids))
                rows = db.scalars(query.order_by(model.id)).all()
            elif table_name in OWNERSHIP_PARENTS:
                column_name, parent_name = OWNERSHIP_PARENTS[table_name]
                parent = mappers[parent_name].class_
                parent_ids = select(parent.id).where(parent.project_id == project_id)
                rows = db.scalars(select(model).where(getattr(model, column_name).in_(parent_ids)).order_by(model.id)).all()
            else:
                continue
            if rows:
                serialized = [_row(mapper, item) for item in rows]
                if table_name == "chapters":
                    for item in serialized:
                        item["pending_world_branch_id"] = None
                        item["source_world_branch_id"] = None
                tables[table_name] = serialized
        archive = {
            "format": ARCHIVE_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "project_id": project_id,
            "project_name": project.name,
            "tables": tables,
        }
        self._validate_tables(tables, project_id, SCHEMA_VERSION, mappers)
        archive["fingerprint"] = _fingerprint(archive)
        return archive

    @staticmethod
    def _remap_json(value: Any, id_map: dict[str, str]) -> Any:
        if isinstance(value, str):
            return id_map.get(value, value)
        if isinstance(value, list):
            return [ProjectBackupService._remap_json(item, id_map) for item in value]
        if isinstance(value, dict):
            return {id_map.get(key, key): ProjectBackupService._remap_json(item, id_map) for key, item in value.items()}
        return value

    @staticmethod
    def _remap_source_key(value: str, id_map: dict[str, str]) -> str:
        return re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", lambda match: id_map.get(match.group(), match.group()), value)

    @staticmethod
    def _validate_tables(tables: dict[str, list[dict[str, Any]]], project_id: str, version: int, mappers: dict[str, Any]) -> dict[str, str]:
        identities: dict[str, str] = {}
        for table_name, rows in tables.items():
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or len(row["id"]) > 36:
                    raise ValueError("BACKUP_ROW_INVALID")
                if row["id"] in identities:
                    raise ValueError("BACKUP_DUPLICATE_ID")
                identities[row["id"]] = table_name
                if "project_id" in mappers[table_name].local_table.c and row.get("project_id") != project_id:
                    raise ValueError("BACKUP_PROJECT_REFERENCE_INVALID")

        # Historical snapshots may contain rows deleted from the current world.
        # They need fresh identities too, without resurrecting those rows into
        # current tables. Validate ownership before accepting any such identity.
        from .snapshot_storage import COLLECTIONS
        for snapshot in tables.get("world_snapshots", []):
            payload = snapshot.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("BACKUP_ROW_INVALID")
            historical_project = payload.get("project")
            if historical_project and (not isinstance(historical_project, dict) or historical_project.get("id") != project_id):
                raise ValueError("BACKUP_PROJECT_REFERENCE_INVALID")
            collections = payload.get("collections") or {}
            if not isinstance(collections, dict):
                raise ValueError("BACKUP_ROW_INVALID")
            for collection in COLLECTIONS:
                historical_rows = payload.get(collection) or []
                changes = collections.get(collection) or {}
                if not isinstance(historical_rows, list) or not isinstance(changes, dict) or any(not isinstance(changes.get(key, []), list) for key in ("upsert", "delete")):
                    raise ValueError("BACKUP_ROW_INVALID")
                historical_rows = [*historical_rows, *(changes.get("upsert") or [])]
                historical_ids = [row.get("id") for row in historical_rows if isinstance(row, dict)] + list(changes.get("delete") or [])
                for row in historical_rows:
                    if not isinstance(row, dict) or ("project_id" in row and row["project_id"] != project_id):
                        raise ValueError("BACKUP_PROJECT_REFERENCE_INVALID")
                for historical_id in historical_ids:
                    if not isinstance(historical_id, str) or not historical_id or len(historical_id) > 36:
                        raise ValueError("BACKUP_ROW_INVALID")
                    if historical_id in identities and identities[historical_id] != collection:
                        raise ValueError("BACKUP_DUPLICATE_ID")
                    identities[historical_id] = collection

        def reference(value: Any, target_names: set[str], *, legacy_missing: bool = False, symbolic: bool = False):
            if value is None:
                return
            if not isinstance(value, str):
                raise ValueError("BACKUP_REFERENCE_INVALID")
            if symbolic and value not in identities and not _uuid(value):
                return
            if identities.get(value) not in target_names:
                if legacy_missing and version == 1 and not any(name in tables for name in target_names):
                    return
                raise ValueError("BACKUP_REFERENCE_INVALID")

        def json_references(value: Any, key: str | None = None):
            if isinstance(value, dict):
                for child_key, child in value.items():
                    # Sealed snapshots and relationship maps use entity IDs as keys.
                    if _uuid(child_key) and child_key not in identities:
                        raise ValueError("BACKUP_REFERENCE_INVALID")
                    json_references(child, child_key)
            elif isinstance(value, list):
                for child in value:
                    json_references(child, key)
            elif key in JSON_REFERENCES and isinstance(value, str):
                reference(value, JSON_REFERENCES[key], symbolic=True, legacy_missing=True)

        for table_name, rows in tables.items():
            for row in rows:
                for column in mappers[table_name].local_table.columns:
                    value = row.get(column.name)
                    if value is None:
                        continue
                    for fk in column.foreign_keys:
                        target = fk.column.table.name
                        if target in AUTHOR_TABLES:
                            reference(value, {target}, legacy_missing=column.nullable)
                    soft_target = SOFT_REFERENCES.get((table_name, column.name))
                    if soft_target:
                        reference(value, {soft_target}, legacy_missing=True)
                    if isinstance(value, (dict, list)):
                        if table_name != "world_snapshots":
                            json_references(value, column.name)
                    elif (table_name, column.name) in {("scenes", "location"), ("story_plan_chapters", "pov_character_ref"), ("story_plan_chapters", "location")}:
                        reference(value, {"characters"} if column.name == "pov_character_ref" else {"world_entities"}, symbolic=True)

        by_id = {row["id"]: row for rows in tables.values() for row in rows}
        for row in tables.get("chapter_quality_assessments", []):
            if by_id[row["writer_draft_id"]]["chapter_id"] != row["chapter_id"]:
                raise ValueError("BACKUP_REFERENCE_INVALID")
        for row in tables.get("chapters", []):
            for field in ("current_writer_draft_id", "current_quality_assessment_id"):
                target = by_id.get(row.get(field))
                if target and target["chapter_id"] != row["id"]:
                    raise ValueError("BACKUP_REFERENCE_INVALID")
        for row in tables.get("volume_contracts", []):
            snapshot = by_id.get(row.get("sealed_snapshot_id"))
            if snapshot and snapshot["volume_id"] != row["id"]:
                raise ValueError("BACKUP_REFERENCE_INVALID")
            if version >= 2 and row.get("status") == "SEALED" and not snapshot:
                raise ValueError("BACKUP_SEALED_SNAPSHOT_MISSING")
        for row in tables.get("author_guidance", []):
            window = by_id.get(row.get("window_id"))
            if window and row.get("volume_id") != window["volume_id"]:
                raise ValueError("BACKUP_REFERENCE_INVALID")
        if any(row.get("origin") == "REPLAY_COMMIT" for row in tables.get("scene_state_checkpoints", [])):
            raise ValueError("BACKUP_REPLAY_HISTORY_UNSUPPORTED")
        return identities

    def restore(self, db: Session, archive: dict[str, Any], *, name: str | None = None) -> Project:
        if not isinstance(archive, dict) or archive.get("format") != ARCHIVE_FORMAT or not isinstance(archive.get("tables"), dict):
            raise ValueError("BACKUP_FORMAT_INVALID")
        supplied = archive.get("fingerprint")
        unsigned = copy.deepcopy(archive); unsigned.pop("fingerprint", None)
        if not isinstance(supplied, str) or supplied != _fingerprint(unsigned):
            raise ValueError("BACKUP_FINGERPRINT_INVALID")
        if len(json.dumps(archive, ensure_ascii=False).encode("utf-8")) > MAX_ARCHIVE_BYTES:
            raise ValueError("BACKUP_TOO_LARGE")
        version = archive.get("schema_version", 1)
        if type(version) is not int or version not in {1, SCHEMA_VERSION}:
            raise ValueError("BACKUP_SCHEMA_UNSUPPORTED")
        if any(key in AUTHOR_TABLES and not isinstance(value, list) for key, value in archive["tables"].items()):
            raise ValueError("BACKUP_ROW_INVALID")
        tables = {str(key): value for key, value in archive["tables"].items() if str(key) in AUTHOR_TABLES and isinstance(value, list)}
        if "projects" not in tables or len(tables["projects"]) != 1:
            raise ValueError("BACKUP_PROJECT_MISSING")
        if sum(len(rows) for rows in tables.values()) > 200_000:
            raise ValueError("BACKUP_TOO_LARGE")
        old_project = tables["projects"][0]
        if not isinstance(old_project, dict) or not old_project.get("id") or archive.get("project_id", old_project["id"]) != old_project["id"]:
            raise ValueError("BACKUP_PROJECT_REFERENCE_INVALID")
        mappers = _mapper_by_table()
        identities = self._validate_tables(tables, old_project["id"], version, mappers)
        id_map = {old_id: str(uuid.uuid4()) for old_id in identities}
        new_project_id = id_map[old_project["id"]]
        # Break nullable FK cycles (Chapter -> current draft -> Chapter, repair
        # draft -> assessment -> draft, supersession). Required parents are
        # inserted first; deferred links are assigned only after every row exists.
        dependencies = {
            table_name: {fk.column.table.name for column in mappers[table_name].local_table.columns if not column.nullable for fk in column.foreign_keys if fk.column.table.name in tables}
            for table_name in tables
        }
        ordered: list[str] = []
        while len(ordered) < len(tables):
            ready = sorted(table for table, parents in dependencies.items() if table not in ordered and parents.issubset(ordered))
            if not ready:
                raise ValueError("BACKUP_REFERENCE_CYCLE")
            ordered.extend(ready)
        try:
            with db.begin_nested():
                deferred: list[tuple[Any, str, Any]] = []
                for table_name in ordered:
                    mapper = mappers[table_name]
                    for source in tables[table_name]:
                        values: dict[str, Any] = {}
                        links = []
                        for column in mapper.local_table.columns:
                            if column.name not in source:
                                continue
                            attribute = mapper.get_property_by_column(column).key
                            value = copy.deepcopy(source[column.name])
                            if column.name == "id":
                                value = id_map[value]
                            elif column.foreign_keys:
                                target_table = next(iter(column.foreign_keys)).column.table.name
                                value = id_map.get(value) if target_table in tables else None
                                if column.nullable:
                                    links.append((attribute, value)); value = None
                            elif isinstance(column.type, DateTime) and isinstance(value, str):
                                value = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
                            elif column.name in {"source_key", "source"} and isinstance(value, str):
                                value = self._remap_source_key(value, id_map)
                            else:
                                value = self._remap_json(value, id_map)
                            values[attribute] = value
                        if table_name == "projects":
                            values["name"] = (name or values.get("name") or "导入的小说").strip()[:200]
                        item = mapper.class_(**values)
                        db.add(item)
                        deferred.extend((item, attribute, value) for attribute, value in links)
                    db.flush()
                for item, attribute, value in deferred:
                    setattr(item, attribute, value)
                db.flush()
                self._finish_restore(db, new_project_id)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("BACKUP_RESTORE_INVALID") from exc
        return db.get(Project, new_project_id)

    def _finish_restore(self, db: Session, project_id: str) -> None:
        from .author_guided_volume import _fingerprint as author_fingerprint, _contract_payload, _volume_payload
        from .models import (BookContract, Chapter, ChapterPlanningWindow, ChapterQualityAssessment,
                             ChapterQualityFinding, ChapterWriterDraft, ForeshadowingLedger, StoryPlan,
                             VolumeContract, VolumeContinuitySnapshot, WriterDraftStatus)
        from .quality import finding_fingerprint

        self._rebase_execution_evidence(db, project_id)

        # Model reviews describe the source project's execution context. Retain
        # the history, but never certify new IDs with an old approval. Adopted
        # prose remains authored material and sealed volume snapshots stay frozen.
        restored_at = datetime.utcnow()
        chapters = db.scalars(select(Chapter).where(Chapter.project_id == project_id)).all()
        for chapter in chapters:
            chapter.current_quality_assessment_id = None
            chapter.quality_status = "STALE" if chapter.quality_status else None
            chapter.quality_content_fingerprint = None
            chapter.quality_approved_at = None
            if chapter.status == "QUALITY_APPROVED":
                chapter.status = "ADOPTED" if chapter.content else "DRAFT"
            chapter.quality_report = {**(chapter.quality_report or {}), "backup_restore": {"requires_reassessment": True, "reason": "PROJECT_ID_CHANGED"}}
        for draft in db.scalars(select(ChapterWriterDraft).where(ChapterWriterDraft.project_id == project_id)).all():
            if draft.status in {WriterDraftStatus.GENERATING, WriterDraftStatus.VALIDATED}:
                draft.status = WriterDraftStatus.STALE
            draft.stale_at = restored_at
        assessments = db.scalars(select(ChapterQualityAssessment).where(ChapterQualityAssessment.project_id == project_id)).all()
        for assessment in assessments:
            assessment.active = False
            assessment.stale_at = restored_at
            for finding in db.scalars(select(ChapterQualityFinding).where(ChapterQualityFinding.assessment_id == assessment.id)).all():
                finding.finding_fingerprint = finding_fingerprint({"source": _json(finding.source), "category": finding.category, "severity": _json(finding.severity), "rule_code": finding.rule_code, "message": finding.message, "start_offset": finding.start_offset, "end_offset": finding.end_offset, "excerpt": finding.excerpt, "source_refs": finding.source_refs, "metadata": finding.finding_metadata})

        self._rebase_structure(db, project_id)
        for contract in db.scalars(select(BookContract).where(BookContract.project_id == project_id)).all():
            payload = _contract_payload(contract); payload.pop("fingerprint", None)
            contract.fingerprint = author_fingerprint(payload)
        volumes = db.scalars(select(VolumeContract).where(VolumeContract.project_id == project_id)).all()
        for volume in volumes:
            payload = _volume_payload(volume); payload.pop("fingerprint", None)
            volume.fingerprint = author_fingerprint(payload)
        volume_by_id = {volume.id: volume for volume in volumes}
        for snapshot in db.scalars(select(VolumeContinuitySnapshot).where(VolumeContinuitySnapshot.project_id == project_id)).all():
            volume = volume_by_id[snapshot.volume_id]
            chapter_ids = [chapter.id for chapter in sorted(chapters, key=lambda item: item.number) if chapter.active and chapter.number >= (volume.actual_chapter_start or 1) and chapter.number <= (volume.actual_chapter_end or 2**31 - 1)]
            snapshot.source_fingerprint = author_fingerprint({"volume": volume.fingerprint, "chapters": chapter_ids, "characters": snapshot.character_states})
        plans = db.scalars(select(StoryPlan).where(StoryPlan.project_id == project_id)).all()
        for plan in plans:
            plan.source_fingerprint = author_fingerprint({key: value for key, value in _row(_mapper_by_table()["story_plans"], plan).items() if key not in {"source_fingerprint", "created_at", "updated_at"}})
        for window in db.scalars(select(ChapterPlanningWindow).where(ChapterPlanningWindow.project_id == project_id)).all():
            plan = next((plan for plan in plans if (plan.generation_report or {}).get("author_window_id") == window.id), None)
            window.plan_fingerprint = plan.source_fingerprint if plan else author_fingerprint({"volume": volume_by_id[window.volume_id].fingerprint, "start": window.start_chapter_number, "end": window.end_chapter_number, "author_note": window.author_note, "source_snapshot": window.source_volume_snapshot_id})
        for item in db.scalars(select(ForeshadowingLedger).where(ForeshadowingLedger.project_id == project_id)).all():
            item.fingerprint = author_fingerprint({key: value for key, value in _row(_mapper_by_table()["foreshadowing_ledger"], item).items() if key != "fingerprint"})
        db.flush()
        self._rebase_current_drafts(db, project_id, chapters, assessments)
        from .formal_state import FormalStateIdentityService
        FormalStateIdentityService().rebuild_and_anchor(db, project_id, source_type="AUTHOR_BACKUP_RESTORE")

    @staticmethod
    def _rebase_execution_evidence(db: Session, project_id: str) -> None:
        """Re-certify imported snapshot storage and scene boundary identities.

        No model call or runtime queue is involved. Original candidate context
        fingerprints remain historical evidence, never a runnable checkpoint.
        """
        from .formal_state import formal_world_state_v2_fingerprint
        from .historical import checkpoint_fingerprint, SceneCheckpointIntegrityValidator
        from .models import CausalLink, Scene, SceneCommit, ScenePerformance, ScenePerformanceTurn, SceneStateCheckpoint, StateDeltaBatch, StateDeltaItem, TimelineEvent, WorldResolution, WorldSnapshot
        from .snapshot_storage import SnapshotDeltaCodec, SnapshotPayloadResolver, snapshot_fingerprint
        from .execution_trace import stable_fingerprint
        from .state_delta import StateDeltaInputFingerprintBuilder, state_delta_item_fingerprint
        from .state_effect_contract import StateEffectPayload

        snapshots = db.scalars(select(WorldSnapshot).where(WorldSnapshot.project_id == project_id)).all()
        codec = SnapshotDeltaCodec()
        payloads: dict[str, Any] = {}
        fingerprints: dict[str, str] = {}
        processed: set[str] = set()
        remaining_children = Counter(snapshot.base_snapshot_id for snapshot in snapshots if snapshot.base_snapshot_id)
        pending = list(snapshots)
        while pending:
            ready = [snapshot for snapshot in pending if not snapshot.base_snapshot_id or snapshot.base_snapshot_id in processed]
            if not ready:
                raise ValueError("BACKUP_SNAPSHOT_CHAIN_INVALID")
            for snapshot in ready:
                mode = _json(snapshot.storage_mode)
                base = db.get(WorldSnapshot, snapshot.base_snapshot_id) if snapshot.base_snapshot_id else None
                if mode in {"LEGACY_FULL", "COMPACT_ANCHOR"}:
                    if mode == "COMPACT_ANCHOR":
                        snapshot.payload = codec.normalize(snapshot.payload)
                    payload = snapshot.payload
                elif mode == "REFERENCE":
                    payload = payloads[snapshot.base_snapshot_id]
                    snapshot.payload = {"protocol": "compact-world-snapshot-v1", "kind": "reference", "base_snapshot_id": snapshot.base_snapshot_id}
                elif mode == "COMPACT_DELTA":
                    payload = codec.apply(payloads[snapshot.base_snapshot_id], snapshot.payload)
                else:
                    raise ValueError("BACKUP_SNAPSHOT_CHAIN_INVALID")
                payloads[snapshot.id] = payload
                old_fingerprint = snapshot.state_fingerprint
                snapshot.state_fingerprint = formal_world_state_v2_fingerprint(payload) if snapshot.state_fingerprint_protocol == "formal-world-state-v2" else snapshot_fingerprint(payload)
                fingerprints[old_fingerprint] = snapshot.state_fingerprint
                if mode != "LEGACY_FULL":
                    snapshot.materialization_depth = base.materialization_depth + 1 if base else 0
                    snapshot.storage_fingerprint = codec.fingerprint(project_id=project_id, mode=mode, base_snapshot_id=snapshot.base_snapshot_id, base_state_fingerprint=base.state_fingerprint if base else None, payload=snapshot.payload, schema_version=snapshot.schema_version)
                processed.add(snapshot.id)
                if snapshot.base_snapshot_id:
                    remaining_children[snapshot.base_snapshot_id] -= 1
                    if not remaining_children[snapshot.base_snapshot_id]:
                        payloads.pop(snapshot.base_snapshot_id, None)
                if not remaining_children[snapshot.id]:
                    payloads.pop(snapshot.id, None)
                pending.remove(snapshot)
        db.flush()
        checkpoints = db.scalars(select(SceneStateCheckpoint).where(SceneStateCheckpoint.project_id == project_id)).all()
        for checkpoint in checkpoints:
            pre = db.get(WorldSnapshot, checkpoint.pre_snapshot_id)
            post = db.get(WorldSnapshot, checkpoint.post_snapshot_id)
            if checkpoint.origin == "REPLAY_COMMIT":
                raise ValueError("BACKUP_REPLAY_HISTORY_UNSUPPORTED")
            checkpoint.pre_state_fingerprint = pre.state_fingerprint
            checkpoint.post_state_fingerprint = post.state_fingerprint
            old_fingerprint = checkpoint.checkpoint_fingerprint
            checkpoint.checkpoint_fingerprint = checkpoint_fingerprint(checkpoint, db.get(Scene, checkpoint.scene_id), pre, post)
            if old_fingerprint:
                fingerprints[old_fingerprint] = checkpoint.checkpoint_fingerprint
        items = db.scalars(select(StateDeltaItem).where(StateDeltaItem.project_id == project_id)).all()
        for item in items:
            old_fingerprint = item.semantic_fingerprint
            item.evidence = ProjectBackupService._remap_json(item.evidence, fingerprints)
            effect = StateEffectPayload.model_validate(item.evidence["state_effect"])
            item.semantic_fingerprint = state_delta_item_fingerprint(project_id, item.source_resolution_id, item.source_turn_id, effect, item.before_value, item.after_value, item.evidence)
            fingerprints[old_fingerprint] = item.semantic_fingerprint
        batches = db.scalars(select(StateDeltaBatch).where(StateDeltaBatch.project_id == project_id)).all()
        for batch in batches:
            batch.base_world_fingerprint = fingerprints.get(batch.base_world_fingerprint, batch.base_world_fingerprint)
            batch.validated_world_fingerprint = fingerprints.get(batch.validated_world_fingerprint, batch.validated_world_fingerprint)
            resolution = db.get(WorldResolution, batch.source_resolution_id) if batch.source_resolution_id else None
            turn = db.get(ScenePerformanceTurn, batch.source_turn_id) if batch.source_turn_id else None
            performance = db.get(ScenePerformance, batch.source_performance_id) if batch.source_performance_id else None
            if resolution and turn and performance:
                batch.input_fingerprint = StateDeltaInputFingerprintBuilder().build(resolution, turn, performance, batch.base_world_fingerprint)
            if batch.validation_fingerprint:
                report = ProjectBackupService._remap_json(batch.validation_report, fingerprints)
                report["batch_input_fingerprint"] = batch.input_fingerprint
                report["basis_world_fingerprint"] = batch.validated_world_fingerprint
                old_fingerprint = batch.validation_fingerprint
                batch_items = sorted([item for item in items if item.batch_id == batch.id], key=lambda item: (item.ordinal, item.id))
                batch.validation_fingerprint = stable_fingerprint({"validation_version": batch.validation_version, "batch_input_fingerprint": batch.input_fingerprint, "basis_world_fingerprint": batch.validated_world_fingerprint, "item_fingerprints": [item.semantic_fingerprint for item in batch_items], "issues": report.get("issues", [])}, "state-delta-validation-v1")
                report["validation_fingerprint"] = batch.validation_fingerprint
                batch.validation_report = report
                fingerprints[old_fingerprint] = batch.validation_fingerprint
        for commit in db.scalars(select(SceneCommit).where(SceneCommit.project_id == project_id)).all():
            if commit.pre_snapshot_id:
                commit.pre_world_fingerprint = fingerprints.get(commit.pre_world_fingerprint, db.get(WorldSnapshot, commit.pre_snapshot_id).state_fingerprint)
            if commit.post_snapshot_id:
                commit.post_world_fingerprint = fingerprints.get(commit.post_world_fingerprint, db.get(WorldSnapshot, commit.post_snapshot_id).state_fingerprint)
            turns = db.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.performance_id == commit.performance_id).order_by(ScenePerformanceTurn.sequence, ScenePerformanceTurn.id)).all()
            turn_order = {turn.id: index for index, turn in enumerate(turns)}
            commit_batches = sorted([batch for batch in batches if batch.applied_commit_id == commit.id], key=lambda batch: turn_order.get(batch.source_turn_id, -1))
            commit_items = [item for batch in commit_batches for item in sorted([item for item in items if item.batch_id == batch.id], key=lambda item: (item.ordinal, item.id))]
            resolutions = [db.scalar(select(WorldResolution).where(WorldResolution.performance_turn_id == turn.id)) for turn in turns if turn.requires_world_resolution]
            commit.source_fingerprint = stable_fingerprint({"project_id": project_id, "proposal_id": commit.proposal_id, "performance_id": commit.performance_id, "decision_ids": [turn.character_decision_id for turn in turns], "turn_ids": [turn.id for turn in turns], "resolution_ids": [resolution.id for resolution in resolutions if resolution], "batch_input_fingerprints": [batch.input_fingerprint for batch in commit_batches], "validation_fingerprints": [batch.validation_fingerprint for batch in commit_batches], "pre_world_fingerprint": commit.pre_world_fingerprint}, "scene-commit-source-v1")
            old_fingerprint = commit.commit_fingerprint
            commit.commit_fingerprint = stable_fingerprint({"source_fingerprint": commit.source_fingerprint, "sequence": db.get(Scene, commit.scene_id).sequence, "item_fingerprints": [item.semantic_fingerprint for item in commit_items], "pre": commit.pre_world_fingerprint, "post": commit.post_world_fingerprint}, "scene-commit-v1")
            fingerprints[old_fingerprint] = commit.commit_fingerprint
        for item in db.scalars(select(TimelineEvent).where(TimelineEvent.project_id == project_id)).all():
            checkpoint = db.get(SceneStateCheckpoint, item.checkpoint_id) if item.checkpoint_id else None
            item.structured_payload = ProjectBackupService._remap_json(item.structured_payload, fingerprints)
            item.event_fingerprint = stable_fingerprint({"event_type": _json(item.event_type), "source_type": item.source_type, "source_id": item.source_id, "source_key": item.source_key, "scene_id": item.scene_id, "sequence": item.sequence, "ordinal": item.ordinal, "world_time": item.world_time, "origin": _json(item.origin), "checkpoint_id": item.checkpoint_id, "checkpoint_fingerprint": checkpoint.checkpoint_fingerprint if checkpoint else None, "target_type": item.target_type, "target_id": item.target_id, "path": item.path, "before": item.before_value, "after": item.after_value, "payload": item.structured_payload}, "timeline-event-v1")
        for link in db.scalars(select(CausalLink).where(CausalLink.project_id == project_id)).all():
            link.evidence = ProjectBackupService._remap_json(link.evidence, fingerprints)
            link.source_key = f"{_json(link.relation_type)}:{_json(link.cause_type)}:{link.cause_id}:{_json(link.effect_type)}:{link.effect_id}"
            link.link_fingerprint = stable_fingerprint({"cause_type": _json(link.cause_type), "cause_id": link.cause_id, "effect_type": _json(link.effect_type), "effect_id": link.effect_id, "edge_kind": _json(link.edge_kind), "relation_type": _json(link.relation_type), "evidence": link.evidence, "replay_session_id": link.replay_session_id}, "causal-link-v1")
        db.flush()
        resolver = SnapshotPayloadResolver()
        for snapshot in snapshots:
            resolver.validate_node_local(db, snapshot)
        for checkpoint in checkpoints:
            SceneCheckpointIntegrityValidator().validate_integrity(db, checkpoint, full_chain=False)

    @staticmethod
    def _rebase_current_drafts(db: Session, project_id: str, chapters: list[Any], assessments: list[Any]) -> None:
        from .models import ChapterWriterDraft, NarrativeStructureRevision
        from .quality import QualityContextBuilder
        from .writer import WriterChapterSourceBuilder, WriterContextBuilder, WriterProjectionAudit
        if not db.scalar(select(NarrativeStructureRevision.id).where(NarrativeStructureRevision.project_id == project_id, NarrativeStructureRevision.active.is_(True))):
            return
        for chapter in chapters:
            if not chapter.active or not chapter.current_writer_draft_id:
                continue
            draft = db.get(ChapterWriterDraft, chapter.current_writer_draft_id)
            WriterProjectionAudit().audit(db, chapter.id)
            source = WriterChapterSourceBuilder().build(db, chapter)
            context = WriterContextBuilder().build(db, source, QualityContextBuilder.writer_request(draft))
            original = {key: getattr(draft, key) for key in ("chapter_structure_fingerprint", "chapter_source_fingerprint", "writer_context_fingerprint", "writing_bible_fingerprint")}
            draft.validation_report = {**(draft.validation_report or {}), "backup_restore": {"original_fingerprints": original, "requires_reassessment": True}}
            draft.chapter_structure_fingerprint = source["structure_fingerprint"]
            draft.chapter_source_fingerprint = source["source_fingerprint"]
            draft.writer_context_fingerprint = context["writer_context_fingerprint"]
            draft.source_manifest = {**context["source_manifest"], "planning_task": context.get("planning_task")}
            draft.writing_bible_fingerprint = context["fingerprints"]["writing_bible"]
            chapter.writer_context_fingerprint = draft.writer_context_fingerprint
            # Historical decisions stay inactive/stale. Keep their linkage to
            # this imported draft consistent so they remain inspectable.
            for assessment in assessments:
                if assessment.writer_draft_id == draft.id:
                    assessment.writer_context_fingerprint = draft.writer_context_fingerprint
                    assessment.chapter_source_fingerprint = draft.chapter_source_fingerprint
                    assessment.writing_bible_fingerprint = draft.writing_bible_fingerprint
        db.flush()

    @staticmethod
    def _rebase_structure(db: Session, project_id: str) -> None:
        from .models import Chapter, NarrativeArc, NarrativeStructureRevision, NarrativeVolume
        from .narrative_structure import NarrativeStructureAudit, NarrativeStructureService
        revisions = db.scalars(select(NarrativeStructureRevision).where(NarrativeStructureRevision.project_id == project_id)).all()
        active = [revision for revision in revisions if revision.active]
        if not active:
            return  # Original v1 author archives did not contain structure history.
        if len(active) != 1:
            raise ValueError("BACKUP_STRUCTURE_INVALID")
        revision = active[0]
        preview = NarrativeStructureService().preview(db, project_id, revision.config)
        chapters = db.scalars(select(Chapter).where(Chapter.project_id == project_id, Chapter.active.is_(True)).order_by(Chapter.number)).all()
        if len(chapters) != len(preview["chapters"]) or any(chapter.source_scene_ids != planned["scene_ids"] for chapter, planned in zip(chapters, preview["chapters"])):
            raise ValueError("BACKUP_STRUCTURE_REBUILD_REQUIRED")
        revision.protocol_version = 1
        revision.source_history_fingerprint = preview["source_fingerprint"]
        revision.config_fingerprint = preview["config_fingerprint"]
        revision.structure_fingerprint = preview["structure_fingerprint"]
        for chapter, planned in zip(chapters, preview["chapters"]):
            chapter.structure_fingerprint = planned["structure_fingerprint"]
            chapter.structure_status = planned["status"]
            chapter.boundary_metadata = planned["boundary_metadata"]
        for model, key in ((NarrativeArc, "narrative_arcs"), (NarrativeVolume, "volumes")):
            rows = db.scalars(select(model).where(model.project_id == project_id, model.active.is_(True)).order_by(model.number)).all()
            if len(rows) != len(preview[key]):
                raise ValueError("BACKUP_STRUCTURE_REBUILD_REQUIRED")
            for row, planned in zip(rows, preview[key]):
                row.structure_revision_id = revision.id
                row.structure_fingerprint = planned["structure_fingerprint"]
        db.flush()
        NarrativeStructureAudit().audit(db, project_id)
