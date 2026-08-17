"""Deterministic revision preview. It has no apply authority."""
import copy, hashlib, json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import CanonFact, Character, CharacterDecision, CharacterKnowledge, CharacterMemory, Chapter, RevealConstraint, Scene, ScenePerformance, ScenePerformanceTurn, SceneProposal, StoryArc, StoryThread, WorldEntity, WorldResolution

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
    parts=[]
    for item in path[1:].split("/"):
        out=""; index=0
        while index < len(item):
            if item[index] != "~": out += item[index]; index += 1; continue
            if index + 1 >= len(item) or item[index + 1] not in "01": raise ValueError("INVALID_PATH")
            out += "/" if item[index + 1] == "1" else "~"; index += 2
        parts.append(out)
    return parts

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
    MODELS = (CanonFact, WorldEntity, Character, CharacterKnowledge, CharacterMemory, RevealConstraint, StoryThread, StoryArc, Scene, Chapter, SceneProposal, ScenePerformance, ScenePerformanceTurn, CharacterDecision, WorldResolution)
    def build(self, session: Session, project_id: str) -> str:
        data={}
        character_ids=[item.id for item in session.scalars(select(Character).where(Character.project_id == project_id)).all()]
        for model in self.MODELS:
            if model in (CharacterKnowledge, CharacterMemory): rows=session.scalars(select(model).where(model.character_id.in_(character_ids)).order_by(model.id)).all() if character_ids else []
            else: rows=session.scalars(select(model).where(model.project_id == project_id).order_by(model.id)).all()
            values=[]
            for item in rows:
                record=_record(item)
                record.pop("created_at", None); record.pop("updated_at", None)
                if model is Chapter: record.pop("content", None); record.pop("word_count", None)
                values.append(record)
            data[model.__tablename__]=values
        stable=json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
        return "revision-state-v1:" + hashlib.sha256(stable.encode()).hexdigest()

class RevisionChangeNormalizer:
    ALLOWED={"CANON_FACT":{"proposition","data","locked","fact_type"}, "WORLD_ENTITY":{"name","profile","active","entity_type"}, "CHARACTER":{"name","profile","personality","core_values","boundaries","goals","current_state","physical_state","emotional_state","abilities","voice_profile","relationships","inventory","secrets","active"}}
    REQUIRED={"CANON_FACT":{"proposition","data"}, "WORLD_ENTITY":{"name","profile"}, "CHARACTER":{"name","profile","goals"}}
    MODELS={"CANON_FACT":CanonFact,"WORLD_ENTITY":WorldEntity,"CHARACTER":Character}
    def normalize(self, session: Session, project_id: str, changes: list[RevisionChangePayload]) -> list[dict[str, Any]]:
        seen=[]; normalized=[]; virtual={}; originals={}
        for change in changes:
            parts=_pointer(change.path)
            if not parts or parts[0] in {"id","project_id","created_at","updated_at"}: raise ValueError("IMMUTABLE_FIELD")
            if parts[0] not in self.ALLOWED[change.target_type]: raise ValueError("INVALID_REVISION_PATH")
            key=(change.target_type,change.target_id,change.path)
            if any(existing[0] == key[0] and existing[1] == key[1] and (key[2].startswith(existing[2] + "/") or existing[2].startswith(key[2] + "/") or key[2] == existing[2]) for existing in seen): raise ValueError("CONFLICTING_CHANGE")
            seen.append(key); model=self.MODELS[change.target_type]; target=session.get(model, change.target_id)
            if not target: raise ValueError("REVISION_TARGET_NOT_FOUND")
            if target.project_id != project_id: raise ValueError("CROSS_PROJECT_REFERENCE")
            if key[:2] not in virtual: originals[key[:2]]=_record(target); virtual[key[:2]]=copy.deepcopy(originals[key[:2]])
            after=virtual[key[:2]]; parent=after
            for part in parts[:-1]:
                if isinstance(parent, dict) and part in parent: parent=parent[part]
                elif isinstance(parent, list) and part.isdigit() and int(part)<len(parent): parent=parent[int(part)]
                else: raise ValueError("INVALID_PATH")
            leaf=parts[-1]
            if isinstance(parent,list) and (not leaf.isdigit() or int(leaf)<0 or int(leaf)>=len(parent)): raise ValueError("INVALID_PATH")
            previous=parent.get(leaf) if isinstance(parent,dict) and leaf in parent else parent[int(leaf)] if isinstance(parent,list) else None
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
            normalized.append({"target_type":change.target_type,"target_id":change.target_id,"operation":change.operation,"path":change.path,"before_value":previous,"after_value":change.value if change.operation=="REMOVE" else (parent.get(leaf) if isinstance(parent,dict) else parent[int(leaf)] if isinstance(parent,list) and leaf.isdigit() and int(leaf)<len(parent) else None),"target_fingerprint_before":target_fingerprint(originals[key[:2]]),"warnings":[]})
        for key, preview in virtual.items():
            self._validate_target(preview, key[0], session, project_id)
            self._validate_references(preview, session, project_id)
            final_fp=target_fingerprint(preview)
            for item in normalized:
                if (item["target_type"], item["target_id"]) == key: item["target_fingerprint_after"] = final_fp
        return normalized

    def _validate_target(self, value, target_type, session, project_id):
        required={"CANON_FACT":{"proposition":str,"data":dict,"locked":bool},"WORLD_ENTITY":{"name":str,"profile":dict,"active":bool},"CHARACTER":{"name":str,"profile":dict,"personality":dict,"core_values":list,"boundaries":list,"goals":dict,"current_state":dict,"physical_state":dict,"emotional_state":dict,"abilities":list,"voice_profile":dict,"relationships":dict,"inventory":list,"secrets":list,"active":bool}}
        for field, expected in required[target_type].items():
            if field not in value or value[field] is None or not isinstance(value[field], expected): raise ValueError("INVALID_TARGET_STATE")
        if target_type=="CANON_FACT" and value.get("fact_type") not in {"TEMPORARY","WORLD_FACT","CORE_CANON","SECRET_CANON"}: raise ValueError("INVALID_TARGET_STATE")
        if target_type=="WORLD_ENTITY" and value.get("entity_type") not in {"CITY","LOCATION","SECT","FACTION","COUNTRY","ITEM","SYSTEM","HISTORY","CUSTOM"}: raise ValueError("INVALID_TARGET_STATE")

    def _validate_references(self, value, session, project_id):
        key_types={"character_id":"character","character_ids":"character","entity_id":"entity","entity_ids":"entity","world_entity_id":"entity","world_entity_ids":"entity","location_id":"entity","canon_fact_id":"canon","canon_fact_ids":"canon"}
        def walk(node):
            if isinstance(node,dict):
                for key,item in node.items():
                    if key in key_types:
                        values=item if key.endswith("s") else [item]
                        for ref in values:
                            if not isinstance(ref,str): raise ValueError("UNKNOWN_REFERENCE")
                            model={"character":Character,"entity":WorldEntity,"canon":CanonFact}[key_types[key]]; found=session.get(model,ref)
                            if not found: raise ValueError("UNKNOWN_REFERENCE")
                            if found.project_id != project_id: raise ValueError("CROSS_PROJECT_REFERENCE")
                    walk(item)
            elif isinstance(node,list):
                for item in node: walk(item)
        walk(value)

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
            affected_proposals=set()
            for proposal in session.scalars(select(SceneProposal).where(SceneProposal.project_id==project_id)).all():
                proposal_refs=scanner.paths({"entry_state":proposal.entry_state,"character_motivations":proposal.character_motivations,"expected_progress":proposal.expected_progress,"possible_outcomes":proposal.possible_outcomes,"new_entity_requests":proposal.new_entity_requests,"risk_flags":proposal.risk_flags},target_id)
                if target_id in (proposal.participants or []) or target_id==proposal.location_id or target_id in (proposal.required_canon or []) or target_id in (proposal.allowed_reveals or []) or target_id in (proposal.forbidden_reveals or []) or proposal_refs:
                    affected_proposals.add(proposal.id); add("REHEARSAL_ARTIFACT","LOW","SCENE_PROPOSAL",proposal.id,"DIRECT_REFERENCE",{"reference_paths":proposal_refs},"INVALIDATE_REHEARSAL")
            for decision in session.scalars(select(CharacterDecision).where(CharacterDecision.project_id==project_id)).all():
                if target_id in {decision.character_id,decision.target_character_id,decision.target_entity_id}: add("CHARACTER_DECISION","LOW","CHARACTER_DECISION",decision.id,"DIRECT_REFERENCE",{},"INVALIDATE_REHEARSAL")
            for resolution in session.scalars(select(WorldResolution).where(WorldResolution.project_id==project_id)).all():
                if target_id in (resolution.canon_fact_ids_used or []) or target_id in (resolution.world_entity_ids_used or []) or scanner.paths(resolution.objective_facts,target_id): add("WORLD_RESOLUTION","LOW","WORLD_RESOLUTION",resolution.id,"STRUCTURED_REFERENCE",{},"INVALIDATE_REHEARSAL")
            for memory in session.scalars(select(CharacterMemory).join(Character).where(Character.project_id==project_id)).all():
                paths=scanner.paths(memory.distortion,target_id)
                if memory.source_scene == target_id or paths or (change["target_type"]=="CANON_FACT" and memory.content == change["before_value"]): add("MEMORY_DEPENDENCY","MEDIUM","CHARACTER_MEMORY",memory.id,"STRUCTURED_REFERENCE",{"reference_paths":paths},"REVIEW_MEMORY")
            for character in session.scalars(select(Character).where(Character.project_id==project_id)).all():
                paths=scanner.paths(character.relationships,target_id)
                if paths or (isinstance(character.relationships,dict) and target_id in character.relationships): add("RELATIONSHIP_DEPENDENCY","MEDIUM","CHARACTER",character.id,"STRUCTURED_REFERENCE",{"reference_paths":paths},"REVIEW_RELATIONSHIP")
                other_paths=scanner.paths({"profile":character.profile,"current_state":character.current_state,"inventory":character.inventory},target_id)
                if other_paths: add("STRUCTURED_REFERENCE","MEDIUM","CHARACTER",character.id,"STRUCTURED_REFERENCE",{"reference_paths":other_paths},"REVIEW")
            for thread in session.scalars(select(StoryThread).where(StoryThread.project_id==project_id)).all():
                paths=scanner.paths(thread.state,target_id)
                if paths or (change["target_type"]=="CANON_FACT" and thread.goal == change["before_value"]): add("STORY_THREAD","MEDIUM","STORY_THREAD",thread.id,"STRUCTURED_REFERENCE",{"reference_paths":paths},"REVIEW_STORY_THREAD")
            for chapter in session.scalars(select(Chapter).where(Chapter.project_id==project_id)).all():
                affected=set(chapter.source_scene_ids or []) & set(scenes)
                if affected: add("CHAPTER_SOURCE","MEDIUM","CHAPTER",chapter.id,"DIRECT_SCENE_SOURCE",{"scene_ids":sorted(affected)},"REVIEW_PROSE")
            for performance in session.scalars(select(ScenePerformance).where(ScenePerformance.project_id==project_id)).all():
                if performance.scene_proposal_id in affected_proposals: add("REHEARSAL_ARTIFACT","LOW","SCENE_PERFORMANCE",performance.id,"PROPOSAL_ARTIFACT",{},"INVALIDATE_REHEARSAL")
            for turn in session.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.project_id==project_id)).all():
                decision=session.get(CharacterDecision,turn.character_decision_id)
                if decision and (decision.character_id==target_id or decision.target_character_id==target_id or decision.target_entity_id==target_id): add("REHEARSAL_ARTIFACT","LOW","SCENE_PERFORMANCE_TURN",turn.id,"DECISION_ARTIFACT",{},"INVALIDATE_REHEARSAL")
            if any(segment in change["path"].split("/") for segment in {"world_time","happened_at","year","date","timeline"}): add("MANUAL_REVIEW","HIGH","REVISION",target_id,"TIMELINE_REVIEW_REQUIRED",{},"MANUAL_REVIEW")
        unique={}
        for item in impacts:
            key=(item["category"],item["resource_type"],item["resource_id"],item["relation"])
            if key not in unique: unique[key]=item
            else:
                old=unique[key]; paths=set(old.get("evidence",{}).get("reference_paths",[])); paths.update(item.get("evidence",{}).get("reference_paths",[])); old.setdefault("evidence",{})["reference_paths"]=sorted(paths)
        impacts=list(unique.values())
        earliest=min(scenes.values(), key=lambda item:(item.sequence,item.id)) if scenes else None
        summary={"total_impacts":len(impacts),"critical":sum(item["severity"]=="CRITICAL" for item in impacts),"high":sum(item["severity"]=="HIGH" for item in impacts),"medium":sum(item["severity"]=="MEDIUM" for item in impacts),"manual_review":0}
        return {"summary":summary,"impacts":impacts,"earliest_affected_scene":{"id":earliest.id,"sequence":earliest.sequence} if earliest else None,"rehearsal_invalidations":[item for item in impacts if item["recommended_action"]=="INVALIDATE_REHEARSAL"],"author_override_required":summary["critical"]>0}
