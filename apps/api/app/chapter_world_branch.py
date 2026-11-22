"""Persistent candidate world state for an automatically written chapter."""
from __future__ import annotations

import copy
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .execution_trace import stable_fingerprint
from .formal_state import FormalStateIdentityService
from .models import (
    Chapter, ChapterSceneBinding, ChapterStructureStatus, ChapterWorldBranch,
    ChapterWorldBranchStatus, Character, Project, Scene, SceneStatus,
    StoryThread, StateDeltaItem, StateDeltaTargetType, WorldEntity,
)
from .revision import _pointer, _record


BRANCH_MODE_INFO_KEY = "chapter_world_branch"


class ChapterWorldBranchService:
    collection_by_type = {
        StateDeltaTargetType.CHARACTER: "characters",
        StateDeltaTargetType.WORLD_ENTITY: "world_entities",
        StateDeltaTargetType.STORY_THREAD: "story_threads",
        StateDeltaTargetType.PROJECT: "project",
    }
    model_by_collection = {
        "characters": Character,
        "world_entities": WorldEntity,
        "story_threads": StoryThread,
        "project": Project,
    }

    def get_or_create(
        self,
        db: Session,
        project_id: str,
        run_id: str,
        chapter_number: int,
        *,
        history_fingerprint: str,
    ) -> ChapterWorldBranch:
        branch = db.scalar(select(ChapterWorldBranch).where(
            ChapterWorldBranch.run_id == run_id,
            ChapterWorldBranch.chapter_number == chapter_number,
        ).with_for_update())
        if branch:
            if branch.project_id != project_id:
                raise ValueError("CHAPTER_WORLD_BRANCH_SCOPE_MISMATCH")
            if branch.status not in {ChapterWorldBranchStatus.OPEN, ChapterWorldBranchStatus.READY}:
                raise ValueError("CHAPTER_WORLD_BRANCH_NOT_OPEN")
            if not branch.chapter_id:
                branch.chapter_id = self._chapter_shell(db, project_id, chapter_number, branch).id
            return branch

        project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
        if not project:
            raise LookupError("CHAPTER_WORLD_BRANCH_PROJECT_NOT_FOUND")
        base_fingerprint, _ = FormalStateIdentityService().current(db, project_id)
        branch = ChapterWorldBranch(
            project_id=project_id,
            run_id=run_id,
            chapter_number=chapter_number,
            status=ChapterWorldBranchStatus.OPEN,
            base_world_fingerprint=base_fingerprint,
            base_history_fingerprint=history_fingerprint,
            current_fingerprint=base_fingerprint,
            state_overrides={},
            scene_ids=[],
            performance_ids=[],
        )
        db.add(branch)
        db.flush()
        branch.chapter_id = self._chapter_shell(db, project_id, chapter_number, branch).id
        db.flush()
        return branch

    def _chapter_shell(self, db: Session, project_id: str, number: int, branch: ChapterWorldBranch) -> Chapter:
        chapter = db.scalar(select(Chapter).where(
            Chapter.project_id == project_id,
            Chapter.number == number,
            Chapter.active.is_(True),
        ).with_for_update())
        if chapter:
            if chapter.world_branch_id and chapter.world_branch_id != branch.id:
                raise ValueError("CHAPTER_WORLD_BRANCH_CHAPTER_OWNED")
            if chapter.pending_world_branch_id and chapter.pending_world_branch_id != branch.id:
                raise ValueError("CHAPTER_WORLD_BRANCH_CHAPTER_OWNED")
            if not chapter.pending_world_branch_id:
                chapter.pending_world_branch_id = branch.id
                db.flush()
            return chapter
        chapter = Chapter(
            project_id=project_id,
            world_branch_id=branch.id,
            pending_world_branch_id=branch.id,
            number=number,
            source_scene_ids=[],
            content=None,
            word_count=0,
            quality_report={},
            status="DRAFT",
            active=True,
            structure_status=ChapterStructureStatus.PROVISIONAL,
            start_sequence=None,
            end_sequence=None,
            structure_fingerprint=stable_fingerprint(
                {"branch_id": branch.id, "chapter_number": number},
                "chapter-world-branch-shell-v1",
            ),
            boundary_metadata={"world_branch_id": branch.id},
        )
        previous_sync = db.info.get("formal_state_sync_in_progress")
        db.info["formal_state_sync_in_progress"] = True
        try:
            db.add(chapter)
            db.flush()
        finally:
            if previous_sync is None:
                db.info.pop("formal_state_sync_in_progress", None)
            else:
                db.info["formal_state_sync_in_progress"] = previous_sync
        return chapter

    def activate(self, db: Session, branch: ChapterWorldBranch) -> None:
        if branch.status not in {ChapterWorldBranchStatus.OPEN, ChapterWorldBranchStatus.READY}:
            raise ValueError("CHAPTER_WORLD_BRANCH_NOT_OPEN")
        db.info[BRANCH_MODE_INFO_KEY] = branch

    @staticmethod
    def active(db: Session) -> ChapterWorldBranch | None:
        branch = db.info.get(BRANCH_MODE_INFO_KEY)
        if branch is not None and branch.status in {ChapterWorldBranchStatus.OPEN, ChapterWorldBranchStatus.READY}:
            return branch
        return None

    def for_chapter(self, db: Session, chapter: Chapter) -> ChapterWorldBranch | None:
        active = self.active(db)
        if active and active.chapter_id == chapter.id:
            return active
        branch_id = chapter.pending_world_branch_id or chapter.source_world_branch_id or chapter.world_branch_id
        return db.get(ChapterWorldBranch, branch_id) if branch_id else None

    def clear(self, db: Session) -> None:
        db.info.pop(BRANCH_MODE_INFO_KEY, None)

    def fingerprint(self, branch: ChapterWorldBranch) -> str:
        return stable_fingerprint({
            "base_world_fingerprint": branch.base_world_fingerprint,
            "state_overrides": branch.state_overrides or {},
            "staged_cognition": branch.staged_cognition or {},
            "state_changes": branch.state_changes or [],
            "scene_ids": branch.scene_ids or [],
        }, "chapter-world-branch-state-v1")

    def effective_record(self, db: Session, row: Any) -> dict[str, Any]:
        branch = self.active(db)
        record = _record(row)
        if not branch:
            return record
        collection = None
        if isinstance(row, Character):
            collection = "characters"
        elif isinstance(row, WorldEntity):
            collection = "world_entities"
        elif isinstance(row, StoryThread):
            collection = "story_threads"
        elif isinstance(row, Project):
            collection = "project"
        if not collection:
            return record
        override = (branch.state_overrides or {}).get(collection, {}).get(row.id)
        return {**record, **copy.deepcopy(override or {})}

    def effective_object(self, db: Session, row: Any) -> Any:
        if row is None:
            return None
        branch = self.active(db)
        values = _record(row)
        collection = None
        if isinstance(row, Character):
            collection = "characters"
        elif isinstance(row, WorldEntity):
            collection = "world_entities"
        elif isinstance(row, StoryThread):
            collection = "story_threads"
        elif isinstance(row, Project):
            collection = "project"
        override = (branch.state_overrides or {}).get(collection, {}).get(row.id) if branch and collection else None
        return SimpleNamespace(**{**values, **copy.deepcopy(override or {})})

    def read_path(self, db: Session, target_type: StateDeltaTargetType, target_id: str, path: str) -> tuple[Any, bool]:
        model = self.model_by_collection[self.collection_by_type[target_type]]
        row = db.get(model, target_id)
        if not row:
            raise ValueError("STATE_DELTA_TARGET_NOT_FOUND")
        branch = self.active(db)
        collection = self.collection_by_type[target_type]
        document = self.effective_record(db, row)
        current: Any = document
        try:
            for part in _pointer(path):
                if isinstance(current, dict) and part in current:
                    current = current[part]
                elif isinstance(current, list) and part.isdigit() and 0 <= int(part) < len(current):
                    current = current[int(part)]
                else:
                    return None, False
        except ValueError as exc:
            raise ValueError("STATE_DELTA_PATH_UNRESOLVED") from exc
        return copy.deepcopy(current), True

    def apply_items(self, db: Session, branch: ChapterWorldBranch, items: list[StateDeltaItem]) -> None:
        overrides = copy.deepcopy(branch.state_overrides or {})
        for item in items:
            collection = self.collection_by_type[item.target_type]
            rows = overrides.setdefault(collection, {})
            if item.target_id not in rows:
                model = self.model_by_collection[collection]
                target = db.get(model, item.target_id)
                if not target or (target.id if collection == "project" else target.project_id) != branch.project_id:
                    raise ValueError("CHAPTER_WORLD_BRANCH_TARGET_NOT_LOADED")
                rows[item.target_id] = _record(target)
            parts = _pointer(item.path)
            if not parts:
                raise ValueError("CHAPTER_WORLD_BRANCH_PATH_INVALID")
            document = rows[item.target_id]
            current: Any = document
            for part in parts[:-1]:
                if not isinstance(current, dict):
                    raise ValueError("CHAPTER_WORLD_BRANCH_PATH_INVALID")
                if part not in current:
                    current[part] = {}
                current = current[part]
            if not isinstance(current, dict):
                raise ValueError("CHAPTER_WORLD_BRANCH_PATH_INVALID")
            current[parts[-1]] = copy.deepcopy(item.after_value)
        branch.state_overrides = overrides
        changes = list(branch.state_changes or [])
        changes.extend({
            "id": item.id,
            "batch_id": item.batch_id,
            "ordinal": item.ordinal,
            "target_type": getattr(item.target_type, "value", item.target_type),
            "target_id": item.target_id,
            "path": item.path,
            "before_value": item.before_value,
            "after_value": item.after_value,
            "event_fingerprint": item.semantic_fingerprint,
        } for item in items)
        branch.state_changes = changes
        branch.current_fingerprint = self.fingerprint(branch)

    @staticmethod
    def stage_cognition(branch: ChapterWorldBranch, scene_id: str, knowledge: list[Any], memories: list[Any]) -> None:
        staged = copy.deepcopy(branch.staged_cognition or {})
        scenes = staged.setdefault("scenes", {})
        scenes[scene_id] = {
            "knowledge": [dict(item) for item in knowledge],
            "memories": [dict(item) for item in memories],
        }
        branch.staged_cognition = staged

    def cognition_rows(self, db: Session, character_id: str, resource_type: str) -> list[Any]:
        branch = self.active(db)
        if not branch:
            return []
        key = "knowledge" if resource_type == "KNOWLEDGE" else "memories"
        rows = []
        for scene_id in branch.scene_ids or []:
            staged = (branch.staged_cognition or {}).get("scenes", {}).get(scene_id, {})
            for values in staged.get(key, []):
                if values.get("character_id") == character_id:
                    rows.append(SimpleNamespace(**copy.deepcopy(values)))
        return rows

    def scene_ids(self, db: Session, branch: ChapterWorldBranch | None = None) -> list[str]:
        branch = branch or self.active(db)
        return list(branch.scene_ids or []) if branch else []

    def branch_scene_rows(self, db: Session, branch: ChapterWorldBranch | None = None) -> list[Scene]:
        branch = branch or self.active(db)
        if not branch:
            return []
        return self.staged_scenes(db, branch)

    def add_scene(self, db: Session, branch: ChapterWorldBranch, scene: Scene, performance_id: str, items: list[StateDeltaItem]) -> None:
        scenes = list(branch.scene_ids or [])
        performances = list(branch.performance_ids or [])
        if scene.id not in scenes:
            scenes.append(scene.id)
        if performance_id not in performances:
            performances.append(performance_id)
        changes = list(branch.state_changes or [])
        item_ids = {item.id for item in items}
        for change in changes:
            if change.get("id") in item_ids:
                change["scene_id"] = scene.id
                change["sequence"] = scene.sequence
        branch.state_changes = changes
        branch.scene_ids = scenes
        branch.performance_ids = performances
        branch.current_fingerprint = self.fingerprint(branch)
        db.flush()

    def scene_context(self, db: Session, branch: ChapterWorldBranch | None = None) -> list[dict[str, Any]]:
        rows = self.branch_scene_rows(db, branch)
        return [{
            "id": row.id,
            "sequence": row.sequence,
            "location": row.location,
            "participants": list(row.participants or []),
            "intent": row.intent,
            "story_threads": list(row.story_threads or []),
            "facts": list(row.facts or []),
            "result": dict(row.result or {}),
        } for row in rows]

    def staged_scenes(self, db: Session, branch: ChapterWorldBranch) -> list[Scene]:
        if not branch.scene_ids:
            return []
        rows = {row.id: row for row in db.scalars(select(Scene).where(
            Scene.project_id == branch.project_id,
            Scene.id.in_(branch.scene_ids),
            or_(Scene.world_branch_id == branch.id, Scene.history_status == "ACTIVE"),
        )).all()}
        return [rows[scene_id] for scene_id in branch.scene_ids if scene_id in rows]

    def record_history_change(self, branch: ChapterWorldBranch) -> None:
        branch.current_fingerprint = self.fingerprint(branch)
        branch.updated_at = datetime.utcnow()

    def publish(self, db: Session, branch: ChapterWorldBranch) -> list[Scene]:
        from .director import DirectorContextBuilder
        from .scene_commit import SceneCommitService

        branch = db.scalar(select(ChapterWorldBranch).where(
            ChapterWorldBranch.id == branch.id,
        ).with_for_update())
        if not branch or branch.status != ChapterWorldBranchStatus.READY:
            raise ValueError("CHAPTER_WORLD_BRANCH_NOT_READY")
        if not db.scalar(select(Project).where(Project.id == branch.project_id).with_for_update()):
            raise ValueError("CHAPTER_WORLD_BRANCH_PROJECT_NOT_FOUND")

        self.clear(db)
        current_world, _ = FormalStateIdentityService().current(db, branch.project_id)
        current_history = DirectorContextBuilder().build(db, branch.project_id)["current_history_fingerprint"]
        if current_world != branch.base_world_fingerprint or current_history != branch.base_history_fingerprint:
            raise ValueError("CHAPTER_WORLD_BRANCH_BASELINE_CHANGED")

        db.info["publishing_chapter_world_branch"] = branch
        published: list[Scene] = []
        try:
            commit_service = SceneCommitService()
            for performance_id in branch.performance_ids or []:
                result = commit_service.commit(db, branch.project_id, performance_id)
                published.append(result.scene)
        finally:
            db.info.pop("publishing_chapter_world_branch", None)
        if branch.chapter_id:
            chapter = db.get(Chapter, branch.chapter_id)
            if chapter:
                expected_scene_ids = [item.get("scene_id") for item in (branch.writer_source_manifest or {}).get("scenes", [])]
                actual_scene_ids = db.scalars(select(ChapterSceneBinding.scene_id).where(
                    ChapterSceneBinding.chapter_id == chapter.id,
                ).order_by(ChapterSceneBinding.ordinal)).all()
                if expected_scene_ids and actual_scene_ids != expected_scene_ids:
                    raise ValueError("CHAPTER_WORLD_BRANCH_STRUCTURE_CHANGED")
                chapter.source_world_branch_id = branch.id
                chapter.pending_world_branch_id = None
        self.activate(db, branch)
        return published
