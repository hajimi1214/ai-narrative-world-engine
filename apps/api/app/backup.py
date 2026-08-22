"""Author-facing project backup and restore.

Archives contain authored story material and configuration metadata, never
provider credentials or derived search indexes. Restores always create a new
project so an import cannot overwrite the author's current book.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, select
from sqlalchemy.orm import Session

from .db import Base
from .models import Character, CharacterKnowledge, CharacterMemory, Project, SnapshotType

ARCHIVE_FORMAT = "nwe-author-project-v1"
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
AUTHOR_TABLES = {
    "projects", "project_model_configs", "writing_bibles", "anti_ai_bibles",
    "canon_facts", "world_entities", "characters", "character_knowledge",
    "character_memories", "reveal_constraints", "story_threads", "story_arcs",
    "scenes", "chapters", "story_plans", "story_plan_volumes", "story_plan_arcs",
    "story_plan_chapters", "research_documents", "research_document_revisions",
    "research_chunks",
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
    return {column.name: _json(getattr(item, column.name)) for column in mapper.local_table.columns}


class ProjectBackupService:
    def export(self, db: Session, project_id: str) -> dict[str, Any]:
        project = db.get(Project, project_id)
        if not project:
            raise ValueError("PROJECT_NOT_FOUND")
        mappers = _mapper_by_table()
        tables: dict[str, list[dict[str, Any]]] = {}
        character_ids = db.scalars(select(Character.id).where(Character.project_id == project_id)).all()
        # Direct project ownership is the authoritative inclusion rule.
        for table_name in sorted(AUTHOR_TABLES):
            mapper = mappers.get(table_name)
            if not mapper:
                continue
            model = mapper.class_
            if table_name == "projects":
                rows = [project]
            elif "project_id" in mapper.local_table.c:
                rows = db.scalars(select(model).where(model.project_id == project_id).order_by(model.id)).all()
            elif model in (CharacterKnowledge, CharacterMemory):
                rows = db.scalars(select(model).where(model.character_id.in_(character_ids)).order_by(model.id)).all() if character_ids else []
            elif table_name in {"story_plan_volumes", "story_plan_arcs"}:
                plan_ids = db.scalars(select(mappers["story_plans"].class_.id).where(mappers["story_plans"].class_.project_id == project_id)).all()
                rows = db.scalars(select(model).where(model.plan_id.in_(plan_ids)).order_by(model.id)).all() if plan_ids else []
            else:
                continue
            if rows:
                tables[table_name] = [_row(mapper, item) for item in rows]
        archive = {
            "format": ARCHIVE_FORMAT,
            "schema_version": 1,
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "project_id": project_id,
            "project_name": project.name,
            "tables": tables,
        }
        archive["fingerprint"] = _fingerprint(archive)
        return archive

    @staticmethod
    def _remap_json(value: Any, id_map: dict[str, str]) -> Any:
        if isinstance(value, str):
            return id_map.get(value, value)
        if isinstance(value, list):
            return [ProjectBackupService._remap_json(item, id_map) for item in value]
        if isinstance(value, dict):
            return {key: ProjectBackupService._remap_json(item, id_map) for key, item in value.items()}
        return value

    def restore(self, db: Session, archive: dict[str, Any], *, name: str | None = None) -> Project:
        if not isinstance(archive, dict) or archive.get("format") != ARCHIVE_FORMAT or not isinstance(archive.get("tables"), dict):
            raise ValueError("BACKUP_FORMAT_INVALID")
        supplied = archive.get("fingerprint")
        unsigned = copy.deepcopy(archive); unsigned.pop("fingerprint", None)
        if not isinstance(supplied, str) or supplied != _fingerprint(unsigned):
            raise ValueError("BACKUP_FINGERPRINT_INVALID")
        tables = {str(key): value for key, value in archive["tables"].items() if str(key) in AUTHOR_TABLES and isinstance(value, list)}
        if "projects" not in tables or len(tables["projects"]) != 1:
            raise ValueError("BACKUP_PROJECT_MISSING")
        if sum(len(rows) for rows in tables.values()) > 200_000:
            raise ValueError("BACKUP_TOO_LARGE")
        mappers = _mapper_by_table()
        id_map: dict[str, str] = {}
        for rows in tables.values():
            for item in rows:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    id_map.setdefault(item["id"], str(uuid.uuid4()))
        old_project = tables["projects"][0]
        new_project_id = id_map.get(old_project.get("id"), str(uuid.uuid4()))
        id_map[old_project.get("id", "")] = new_project_id
        # Metadata.sorted_tables gives parent-before-child insertion order.
        ordered = [table.name for table in Base.metadata.sorted_tables if table.name in tables]
        ordered.extend(table for table in tables if table not in ordered)
        inserted: dict[str, int] = {}
        for table_name in ordered:
            mapper = mappers.get(table_name)
            if not mapper:
                continue
            model = mapper.class_
            for source in tables[table_name]:
                if not isinstance(source, dict):
                    raise ValueError("BACKUP_ROW_INVALID")
                values = {}
                for column in mapper.local_table.columns:
                    if column.name not in source:
                        continue
                    value = copy.deepcopy(source[column.name])
                    if column.name == "id":
                        value = id_map.get(str(value), str(uuid.uuid4()))
                    elif column.name == "project_id":
                        value = new_project_id
                    elif column.foreign_keys:
                        target_table = next(iter(column.foreign_keys)).column.table.name
                        value = id_map.get(str(value), value) if target_table in tables else None
                    elif isinstance(column.type, DateTime) and isinstance(value, str):
                        value = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
                    elif isinstance(value, (dict, list)):
                        value = self._remap_json(value, id_map)
                    values[column.name] = value
                if table_name == "projects":
                    values["id"] = new_project_id
                    values["name"] = (name or values.get("name") or "导入的小说").strip()[:200]
                try:
                    db.add(model(**values)); db.flush()
                except Exception as exc:
                    db.rollback()
                    raise ValueError("BACKUP_RESTORE_INVALID") from exc
                inserted[table_name] = inserted.get(table_name, 0) + 1
        from .versioning import WorldSnapshotBuilder
        from .snapshot_storage import ProjectWorldSnapshotHeadService
        snapshot = WorldSnapshotBuilder().create(db, new_project_id, SnapshotType.BASELINE)
        ProjectWorldSnapshotHeadService().update(db, new_project_id, snapshot, source_type="AUTHOR_BACKUP_RESTORE")
        db.flush()
        return db.get(Project, new_project_id)
