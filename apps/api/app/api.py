from datetime import datetime
from enum import Enum
from typing import Any, Type
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import SessionLocal
from .models import AntiAIBible, CanonFact, Character, CharacterKnowledge, CharacterMemory, Chapter, Project, ProjectTemplate, Scene, StoryArc, StoryThread, WorldEntity, WritingBible
from .services import DomainRuleError, activate_anti_ai_bible, activate_writing_bible, update_canon

router = APIRouter()

class Payload(BaseModel):
    model_config = ConfigDict(extra="allow")

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def serialize(value: Any) -> Any:
    if isinstance(value, Enum): return value.value
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, list): return [serialize(item) for item in value]
    if isinstance(value, dict): return {key: serialize(item) for key, item in value.items()}
    return value

def record_dict(record: Any) -> dict[str, Any]:
    return {column.name: serialize(getattr(record, column.name)) for column in record.__table__.columns}

def require_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project: raise HTTPException(status_code=404, detail="Project not found")
    return project

def create_record(db: Session, model: Type, values: dict[str, Any], project_id: str | None = None):
    if project_id: values["project_id"] = project_id
    record = model(**values)
    db.add(record); db.commit(); db.refresh(record)
    return record

def update_record(db: Session, record: Any, values: dict[str, Any]):
    for key, value in values.items():
        if key not in {"id", "project_id", "created_at", "updated_at"}: setattr(record, key, value)
    db.add(record); db.commit(); db.refresh(record)
    return record

@router.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    return [record_dict(item) for item in db.scalars(select(Project).order_by(Project.created_at.desc())).all()]

@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(payload: Payload, db: Session = Depends(get_db)):
    return record_dict(create_record(db, Project, payload.model_dump()))

@router.get("/projects/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db)):
    return record_dict(require_project(db, project_id))

@router.patch("/projects/{project_id}")
def patch_project(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    return record_dict(update_record(db, require_project(db, project_id), payload.model_dump()))

@router.get("/projects/{project_id}/snapshot")
def project_snapshot(project_id: str, db: Session = Depends(get_db)):
    project = require_project(db, project_id)
    characters = db.scalars(select(Character).where(Character.project_id == project_id, Character.active.is_(True))).all()
    character_ids = [item.id for item in characters]
    knowledge = db.scalars(select(CharacterKnowledge).where(CharacterKnowledge.character_id.in_(character_ids))).all() if character_ids else []
    return {
        "project": record_dict(project),
        "active_writing_bible": next((record_dict(item) for item in db.scalars(select(WritingBible).where(WritingBible.project_id == project_id, WritingBible.active.is_(True))).all()), None),
        "active_anti_ai_bible": next((record_dict(item) for item in db.scalars(select(AntiAIBible).where(AntiAIBible.project_id == project_id, AntiAIBible.active.is_(True))).all()), None),
        "canon": [record_dict(item) for item in db.scalars(select(CanonFact).where(CanonFact.project_id == project_id)).all()],
        "active_characters": [record_dict(item) for item in characters],
        "character_states": [{"character_id": item.id, "current_state": serialize(item.current_state), "physical_state": serialize(item.physical_state), "emotional_state": serialize(item.emotional_state), "goals": serialize(item.goals)} for item in characters],
        "character_knowledge_summary": [{"character_id": item.character_id, "proposition": item.proposition, "status": item.status.value, "confidence": item.confidence} for item in knowledge],
        "world_entities": [record_dict(item) for item in db.scalars(select(WorldEntity).where(WorldEntity.project_id == project_id, WorldEntity.active.is_(True))).all()],
        "active_story_threads": [record_dict(item) for item in db.scalars(select(StoryThread).where(StoryThread.project_id == project_id, StoryThread.status.in_(["OPEN", "PAUSED"]))).all()],
        "current_story_arc": next((record_dict(item) for item in db.scalars(select(StoryArc).where(StoryArc.project_id == project_id, StoryArc.status == "ACTIVE").order_by(StoryArc.id.desc())).all()), None),
        "recent_scenes": [record_dict(item) for item in db.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.sequence.desc()).limit(20)).all()],
    }

def project_routes(prefix: str, model: Type, allow_update: bool = True):
    @router.get(f"/projects/{{project_id}}/{prefix}")
    def list_items(project_id: str, db: Session = Depends(get_db)):
        require_project(db, project_id)
        return [record_dict(item) for item in db.scalars(select(model).where(model.project_id == project_id)).all()]
    @router.post(f"/projects/{{project_id}}/{prefix}", status_code=status.HTTP_201_CREATED)
    def add_item(project_id: str, payload: Payload, db: Session = Depends(get_db)):
        require_project(db, project_id)
        return record_dict(create_record(db, model, payload.model_dump(), project_id))
    @router.get(f"/{prefix}/{{item_id}}")
    def get_item(item_id: str, db: Session = Depends(get_db)):
        item = db.get(model, item_id)
        if not item: raise HTTPException(status_code=404, detail="Resource not found")
        return record_dict(item)
    if allow_update:
        @router.patch(f"/{prefix}/{{item_id}}")
        def patch_item(item_id: str, payload: Payload, db: Session = Depends(get_db)):
            item = db.get(model, item_id)
            if not item: raise HTTPException(status_code=404, detail="Resource not found")
            return record_dict(update_record(db, item, payload.model_dump()))
    @router.delete(f"/{prefix}/{{item_id}}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_item(item_id: str, db: Session = Depends(get_db)):
        item = db.get(model, item_id)
        if not item: raise HTTPException(status_code=404, detail="Resource not found")
        db.delete(item); db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

for path, model, allow_update in [("characters", Character, True), ("world-entities", WorldEntity, True), ("canon", CanonFact, False), ("story-threads", StoryThread, True), ("scenes", Scene, True), ("chapters", Chapter, True), ("story-arcs", StoryArc, True)]:
    project_routes(path, model, allow_update)

@router.patch("/canon/{fact_id}")
def patch_canon(fact_id: str, payload: Payload, db: Session = Depends(get_db)):
    fact = db.get(CanonFact, fact_id)
    if not fact: raise HTTPException(status_code=404, detail="Canon fact not found")
    try: return record_dict(update_canon(db, fact, **payload.model_dump()))
    except DomainRuleError as error: raise HTTPException(status_code=409, detail=str(error)) from error

@router.get("/projects/{project_id}/writing-bibles")
def list_writing_bibles(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [record_dict(item) for item in db.scalars(select(WritingBible).where(WritingBible.project_id == project_id).order_by(WritingBible.version)).all()]

@router.post("/projects/{project_id}/writing-bibles", status_code=status.HTTP_201_CREATED)
def create_writing_bible(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    bible = create_record(db, WritingBible, payload.model_dump(), project_id)
    return record_dict(activate_writing_bible(db, bible) if bible.active else bible)

@router.post("/writing-bibles/{bible_id}/activate")
def activate_writing_bible_endpoint(bible_id: str, db: Session = Depends(get_db)):
    bible = db.get(WritingBible, bible_id)
    if not bible: raise HTTPException(status_code=404, detail="Writing Bible not found")
    return record_dict(activate_writing_bible(db, bible))

@router.get("/projects/{project_id}/anti-ai-bibles")
def list_anti_ai_bibles(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return [record_dict(item) for item in db.scalars(select(AntiAIBible).where(AntiAIBible.project_id == project_id).order_by(AntiAIBible.version)).all()]

@router.post("/projects/{project_id}/anti-ai-bibles", status_code=status.HTTP_201_CREATED)
def create_anti_ai_bible(project_id: str, payload: Payload, db: Session = Depends(get_db)):
    require_project(db, project_id)
    bible = create_record(db, AntiAIBible, payload.model_dump(), project_id)
    return record_dict(activate_anti_ai_bible(db, bible) if bible.active else bible)

@router.post("/anti-ai-bibles/{bible_id}/activate")
def activate_anti_ai_bible_endpoint(bible_id: str, db: Session = Depends(get_db)):
    bible = db.get(AntiAIBible, bible_id)
    if not bible: raise HTTPException(status_code=404, detail="Anti-AI Bible not found")
    return record_dict(activate_anti_ai_bible(db, bible))

@router.get("/project-templates")
def list_templates(db: Session = Depends(get_db)):
    return [record_dict(item) for item in db.scalars(select(ProjectTemplate)).all()]

@router.post("/project-templates", status_code=status.HTTP_201_CREATED)
def create_template(payload: Payload, db: Session = Depends(get_db)):
    return record_dict(create_record(db, ProjectTemplate, payload.model_dump()))

@router.get("/characters/{character_id}/knowledge")
def character_knowledge(character_id: str, db: Session = Depends(get_db)):
    if not db.get(Character, character_id): raise HTTPException(status_code=404, detail="Character not found")
    return [record_dict(item) for item in db.scalars(select(CharacterKnowledge).where(CharacterKnowledge.character_id == character_id)).all()]

@router.post("/characters/{character_id}/knowledge", status_code=status.HTTP_201_CREATED)
def create_character_knowledge(character_id: str, payload: Payload, db: Session = Depends(get_db)):
    if not db.get(Character, character_id): raise HTTPException(status_code=404, detail="Character not found")
    return record_dict(create_record(db, CharacterKnowledge, payload.model_dump() | {"character_id": character_id}))

@router.get("/characters/{character_id}/memories")
def character_memories(character_id: str, db: Session = Depends(get_db)):
    if not db.get(Character, character_id): raise HTTPException(status_code=404, detail="Character not found")
    return [record_dict(item) for item in db.scalars(select(CharacterMemory).where(CharacterMemory.character_id == character_id)).all()]

@router.post("/characters/{character_id}/memories", status_code=status.HTTP_201_CREATED)
def create_character_memory(character_id: str, payload: Payload, db: Session = Depends(get_db)):
    if not db.get(Character, character_id): raise HTTPException(status_code=404, detail="Character not found")
    return record_dict(create_record(db, CharacterMemory, payload.model_dump() | {"character_id": character_id}))
