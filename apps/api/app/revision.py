"""Deterministic revision preview. It has no apply authority."""
import copy, hashlib, json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import CanonFact, Character, CharacterDecision, CharacterKnowledge, CharacterMemory, Chapter, RevealConstraint, Scene, ScenePerformance, SceneProposal, StoryArc, StoryThread, WorldEntity, WorldResolution

TargetType = Literal["CANON_FACT", "WORLD_ENTITY", "CHARACTER"]
Operation = Literal["SET", "MERGE", "REMOVE"]

class RevisionChangePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_type: TargetType; target_id: str; operation: Operation; path: str; value: Any | None = None

class RevisionCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str; description: str | None = None; changes: list[RevisionChangePayload]

def _pointer(path: str) -> list[str]:
    if not path.startswith("/"): raise ValueError("INVALID_PATH")
    return [item.replace("~1", "/").replace("~0", "~") for item in path[1:].split("/") if item != ""]

def _record(record: Any) -> dict[str, Any]:
    from .api import serialize
    return {column.name: serialize(getattr(record, column.name)) for column in record.__table__.columns}

def target_fingerprint(value: dict[str, Any]) -> str:
    stable = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return "revision-target-v1:" + hashlib.sha256(stable.encode()).hexdigest()

class StructuredReferenceScanner:
    KEYS = {"character_id", "entity_id", "canon_fact_id", "location_id"}
    PLURAL = {"character_ids", "entity_ids", "canon_fact_ids"}
    def paths(self, value: Any, target_id: str, path="") -> list[str]:
        found=[]
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{path}/{key}"
                if key in self.KEYS and item == target_id: found.append(child)
                elif key in self.PLURAL and isinstance(item, list): found.extend(f"{child}/{index}" for index, entry in enumerate(item) if entry == target_id)
                found.extend(self.paths(item, target_id, child))
        elif isinstance(value, list):
            for index, item in enumerate(value): found.extend(self.paths(item, target_id, f"{path}/{index}"))
        return found

class RevisionStateFingerprintBuilder:
    MODELS = (CanonFact, WorldEntity, Character, CharacterKnowledge, CharacterMemory, RevealConstraint, StoryThread, StoryArc, Scene, Chapter, SceneProposal, ScenePerformance, CharacterDecision, WorldResolution)
    def build(self, session: Session, project_id: str) -> str:
        data={}
        character_ids=[item.id for item in session.scalars(select(Character).where(Character.project_id == project_id)).all()]
        for model in self.MODELS:
            if model in (CharacterKnowledge, CharacterMemory): rows=session.scalars(select(model).where(model.character_id.in_(character_ids)).order_by(model.id)).all() if character_ids else []
            else: rows=session.scalars(select(model).where(model.project_id == project_id).order_by(model.id)).all()
            values=[]
            for item in rows:
                record=_record(item)
                if model is Chapter: record.pop("content", None)
                values.append(record)
            data[model.__tablename__]=values
        stable=json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
        return "revision-state-v1:" + hashlib.sha256(stable.encode()).hexdigest()

class RevisionChangeNormalizer:
    ALLOWED={"CANON_FACT":{"proposition","data","locked","fact_type"}, "WORLD_ENTITY":{"name","profile","active","entity_type"}, "CHARACTER":{"name","profile","personality","core_values","boundaries","goals","current_state","physical_state","emotional_state","abilities","voice_profile","relationships","inventory","secrets","active"}}
    REQUIRED={"CANON_FACT":{"proposition","data"}, "WORLD_ENTITY":{"name","profile"}, "CHARACTER":{"name","profile","goals"}}
    MODELS={"CANON_FACT":CanonFact,"WORLD_ENTITY":WorldEntity,"CHARACTER":Character}
    def normalize(self, session: Session, project_id: str, changes: list[RevisionChangePayload]) -> list[dict[str, Any]]:
        seen=[]; normalized=[]
        for change in changes:
            parts=_pointer(change.path)
            if not parts or parts[0] in {"id","project_id","created_at","updated_at"}: raise ValueError("IMMUTABLE_FIELD")
            if parts[0] not in self.ALLOWED[change.target_type]: raise ValueError("INVALID_REVISION_PATH")
            key=(change.target_type,change.target_id,change.path)
            if any(existing[0] == key[0] and existing[1] == key[1] and (key[2].startswith(existing[2] + "/") or existing[2].startswith(key[2] + "/") or key[2] == existing[2]) for existing in seen): raise ValueError("CONFLICTING_CHANGE")
            seen.append(key); model=self.MODELS[change.target_type]; target=session.get(model, change.target_id)
            if not target: raise ValueError("REVISION_TARGET_NOT_FOUND")
            if target.project_id != project_id: raise ValueError("CROSS_PROJECT_REFERENCE")
            before=_record(target); after=copy.deepcopy(before); parent=after
            for part in parts[:-1]:
                if isinstance(parent, dict) and part in parent: parent=parent[part]
                elif isinstance(parent, list) and part.isdigit() and int(part)<len(parent): parent=parent[int(part)]
                else: raise ValueError("INVALID_PATH")
            leaf=parts[-1]; previous=parent.get(leaf) if isinstance(parent,dict) else parent[int(leaf)] if isinstance(parent,list) and leaf.isdigit() and int(leaf)<len(parent) else None
            if change.operation == "MERGE":
                if not isinstance(previous,dict) or not isinstance(change.value,dict): raise ValueError("INVALID_MERGE_TARGET")
                parent[leaf]={**previous,**change.value}
            elif change.operation == "REMOVE":
                if len(parts)==1 and leaf in self.REQUIRED[change.target_type]: raise ValueError("REQUIRED_FIELD_REMOVAL")
                if isinstance(parent,dict): parent.pop(leaf,None)
                elif isinstance(parent,list) and leaf.isdigit(): parent.pop(int(leaf))
                else: raise ValueError("INVALID_PATH")
            else:
                if isinstance(parent,dict): parent[leaf]=change.value
                elif isinstance(parent,list) and leaf.isdigit(): parent[int(leaf)]=change.value
                else: raise ValueError("INVALID_PATH")
            normalized.append({"target_type":change.target_type,"target_id":change.target_id,"operation":change.operation,"path":change.path,"before_value":previous,"after_value":parent.get(leaf) if isinstance(parent,dict) else parent[int(leaf)] if isinstance(parent,list) and leaf.isdigit() and int(leaf)<len(parent) else None,"target_fingerprint_before":target_fingerprint(before),"warnings":[]})
        return normalized

class RevisionImpactAnalyzer:
    def analyze(self, session: Session, project_id: str, normalized: list[dict[str, Any]]) -> dict[str, Any]:
        impacts=[]; scenes={}; scanner=StructuredReferenceScanner()
        def add(category,severity,resource_type,resource_id,relation,evidence,action): impacts.append({"category":category,"severity":severity,"certainty":"EXACT" if relation.startswith("DIRECT") else "STRUCTURED","resource_type":resource_type,"resource_id":resource_id,"relation":relation,"evidence":evidence,"recommended_action":action})
        for change in normalized:
            target_id=change["target_id"]
            add("DIRECT_TARGET", "CRITICAL" if change["target_type"]=="CANON_FACT" and session.get(CanonFact,target_id).locked else "HIGH", change["target_type"], target_id, "DIRECT_CHANGE", {"path":change["path"]}, "RETCON_REVIEW")
            if change["target_type"] == "CANON_FACT":
                old=change["before_value"] if change["path"]=="/proposition" else None
                if old:
                    for item in session.scalars(select(CharacterKnowledge).join(Character).where(Character.project_id==project_id, CharacterKnowledge.proposition==old)).all(): add("KNOWLEDGE_DEPENDENCY","HIGH","CHARACTER_KNOWLEDGE",item.id,"DIRECT_PROPOSITION",{"proposition":old},"REBUILD_KNOWLEDGE")
                for item in session.scalars(select(RevealConstraint).where(RevealConstraint.project_id==project_id, RevealConstraint.canon_fact_id==target_id)).all(): add("REVEAL_OR_FORESHADOWING","HIGH","REVEAL_CONSTRAINT",item.id,"DIRECT_CANON_REFERENCE",{},"REVIEW_REVEAL")
            for scene in session.scalars(select(Scene).where(Scene.project_id==project_id)).all():
                refs=scanner.paths({"participants":scene.participants,"facts":scene.facts,"result":scene.result},target_id)
                if target_id in (scene.participants or []) or refs or (change["target_type"]=="CANON_FACT" and change["before_value"] in (scene.facts or [])):
                    add("SCENE_HISTORY","HIGH","SCENE",scene.id,"STRUCTURED_REFERENCE",{"reference_paths":refs},"REPLAY_FROM_SCENE"); scenes[scene.id]=scene
            for proposal in session.scalars(select(SceneProposal).where(SceneProposal.project_id==project_id)).all():
                if target_id in (proposal.participants or []) or target_id==proposal.location_id or target_id in (proposal.required_canon or []) or target_id in (proposal.allowed_reveals or []) or target_id in (proposal.forbidden_reveals or []): add("REHEARSAL_ARTIFACT","LOW","SCENE_PROPOSAL",proposal.id,"DIRECT_REFERENCE",{},"INVALIDATE_REHEARSAL")
            for decision in session.scalars(select(CharacterDecision).where(CharacterDecision.project_id==project_id)).all():
                if target_id in {decision.character_id,decision.target_character_id,decision.target_entity_id}: add("CHARACTER_DECISION","LOW","CHARACTER_DECISION",decision.id,"DIRECT_REFERENCE",{},"INVALIDATE_REHEARSAL")
            for resolution in session.scalars(select(WorldResolution).where(WorldResolution.project_id==project_id)).all():
                if target_id in (resolution.canon_fact_ids_used or []) or target_id in (resolution.world_entity_ids_used or []) or scanner.paths(resolution.objective_facts,target_id): add("WORLD_RESOLUTION","LOW","WORLD_RESOLUTION",resolution.id,"STRUCTURED_REFERENCE",{},"INVALIDATE_REHEARSAL")
        earliest=min(scenes.values(), key=lambda item:(item.sequence,item.id)) if scenes else None
        summary={"total_impacts":len(impacts),"critical":sum(item["severity"]=="CRITICAL" for item in impacts),"high":sum(item["severity"]=="HIGH" for item in impacts),"medium":sum(item["severity"]=="MEDIUM" for item in impacts),"manual_review":0}
        return {"summary":summary,"impacts":impacts,"earliest_affected_scene":{"id":earliest.id,"sequence":earliest.sequence} if earliest else None,"rehearsal_invalidations":[item for item in impacts if item["recommended_action"]=="INVALIDATE_REHEARSAL"],"author_override_required":summary["critical"]>0}
