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

class RevisionPatchEngine:
    """Single RFC6901 patch implementation shared by preview and apply."""
    def apply(self, document: dict[str, Any], operation: str, path: str, value: Any) -> tuple[Any, Any]:
        parts=_pointer(path)
        if not parts: raise ValueError("INVALID_PATH")
        parent=document
        for part in parts[:-1]:
            if isinstance(parent,dict) and part in parent: parent=parent[part]
            elif isinstance(parent,list) and part.isdigit() and 0<=int(part)<len(parent): parent=parent[int(part)]
            else: raise ValueError("INVALID_PATH")
        leaf=parts[-1]
        if isinstance(parent,list) and (not leaf.isdigit() or not 0<=int(leaf)<len(parent)): raise ValueError("INVALID_PATH")
        if isinstance(parent,dict) and leaf not in parent and operation=="REMOVE": raise ValueError("INVALID_PATH")
        before=parent.get(leaf) if isinstance(parent,dict) else parent[int(leaf)] if isinstance(parent,list) else None
        if operation=="SET":
            if isinstance(parent,dict): parent[leaf]=value
            elif isinstance(parent,list): parent[int(leaf)]=value
            else: raise ValueError("INVALID_PATH")
        elif operation=="MERGE":
            if not isinstance(before,dict) or not isinstance(value,dict): raise ValueError("INVALID_MERGE_TARGET")
            parent[leaf]={**before,**value}
        elif operation=="REMOVE":
            if isinstance(parent,dict): parent.pop(leaf)
            elif isinstance(parent,list): parent.pop(int(leaf))
            else: raise ValueError("INVALID_PATH")
        else: raise ValueError("INVALID_OPERATION")
        after=parent.get(leaf) if isinstance(parent,dict) else parent[int(leaf)] if isinstance(parent,list) and int(leaf)<len(parent) else None
        return before,after

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
        """IMPACT_EXACT mode: exact scalar/list values and dict keys only."""
        found=[]
        if isinstance(value, dict):
            for key, item in value.items():
                escaped_key = str(key).replace("~", "~0").replace("/", "~1")
                child = f"{path}/{escaped_key}"
                if key == target_id: found.append(child)
                if isinstance(item, str) and item == target_id: found.append(child)
                elif isinstance(item, list): found.extend(f"{child}/{index}" for index, entry in enumerate(item) if isinstance(entry, str) and entry == target_id)
                found.extend(self.paths(item, target_id, child))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                child=f"{path}/{index}"
                if isinstance(item, str) and item == target_id: found.append(child)
                found.extend(self.paths(item, target_id, child))
        return sorted(set(found))

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
            if change.operation == "REMOVE" and len(parts) == 1 and parts[0] in self.REQUIRED[change.target_type]:
                raise ValueError("REQUIRED_FIELD_REMOVAL")
            previous, after_value = RevisionPatchEngine().apply(
                virtual[key[:2]], change.operation, change.path, change.value
            )
            normalized.append({"target_type":change.target_type,"target_id":change.target_id,"operation":change.operation,"path":change.path,"before_value":previous,"after_value":after_value,"target_fingerprint_before":target_fingerprint(originals[key[:2]]),"warnings":[]})
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
            if field in {"name", "proposition"} and not value[field].strip(): raise ValueError("INVALID_TARGET_STATE")
        if target_type=="CANON_FACT" and value.get("fact_type") not in {"TEMPORARY","WORLD_FACT","CORE_CANON","SECRET_CANON"}: raise ValueError("INVALID_TARGET_STATE")
        if target_type=="WORLD_ENTITY" and value.get("entity_type") not in {"CITY","LOCATION","SECT","FACTION","COUNTRY","ITEM","SYSTEM","HISTORY","CUSTOM"}: raise ValueError("INVALID_TARGET_STATE")

    def _validate_references(self, value, session, project_id):
        key_types={"character_id":"character","character_ids":"character","entity_id":"entity","entity_ids":"entity","world_entity_id":"entity","world_entity_ids":"entity","location_id":"entity","canon_fact_id":"canon","canon_fact_ids":"canon"}
        def walk(node):
            if isinstance(node,dict):
                for key,item in node.items():
                    if key in key_types:
                        if key.endswith("s") and not isinstance(item, list): raise ValueError("INVALID_REFERENCE_TYPE")
                        values=item if key.endswith("s") else [item]
                        if not key.endswith("s") and item is None: values=[]
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
        return self._two_pass(session, project_id, normalized)

    def _two_pass(self, session: Session, project_id: str, normalized: list[dict[str, Any]]) -> dict[str, Any]:
        scanner=StructuredReferenceScanner(); impacts=[]; keys=set()
        affected_scenes=set(); affected_proposals=set(); affected_performances=set(); affected_decisions=set(); affected_resolutions=set(); affected_turns=set()
        def add(category,severity,certainty,resource_type,resource_id,relation,evidence,action):
            key=(category,resource_type,resource_id,relation)
            if key in keys:
                for old in impacts:
                    if (old["category"],old["resource_type"],old["resource_id"],old["relation"])==key:
                        for field in ("reference_paths","scene_ids","proposal_ids","turn_ids"):
                            values=set(old["evidence"].get(field,[]))|set(evidence.get(field,[]))
                            if values: old["evidence"][field]=sorted(values)
                        return
            keys.add(key); impacts.append({"category":category,"severity":severity,"certainty":certainty,"resource_type":resource_type,"resource_id":resource_id,"relation":relation,"evidence":evidence,"recommended_action":action})
        all_scenes=session.scalars(select(Scene).where(Scene.project_id==project_id)).all()
        all_proposals=session.scalars(select(SceneProposal).where(SceneProposal.project_id==project_id)).all()
        locked_override=False
        for change in normalized:
            target=change["target_id"]; typ=change["target_type"]; old=change["before_value"] if change["path"]=="/proposition" else None
            canon=session.get(CanonFact,target) if typ=="CANON_FACT" else None
            fact_type=getattr(canon.fact_type,"value",canon.fact_type) if canon else None
            if canon and canon.locked and fact_type in {"CORE_CANON","SECRET_CANON"}: locked_override=True; severity="CRITICAL"
            elif canon and canon.locked: severity="HIGH"
            else: severity="HIGH"
            add("DIRECT_TARGET",severity,"EXACT",typ,target,"DIRECT_CHANGE",{"path":change["path"]},"RETCON_REVIEW")
            if old:
                for item in session.scalars(select(CharacterKnowledge).join(Character).where(Character.project_id==project_id,CharacterKnowledge.proposition==old)).all(): add("KNOWLEDGE_DEPENDENCY","HIGH","EXACT","CHARACTER_KNOWLEDGE",item.id,"CANON_PROPOSITION",{"proposition":old},"REBUILD_KNOWLEDGE")
                for item in session.scalars(select(RevealConstraint).where(RevealConstraint.project_id==project_id,RevealConstraint.canon_fact_id==target)).all(): add("REVEAL_OR_FORESHADOWING","HIGH","EXACT","REVEAL_CONSTRAINT",item.id,"CANON_REFERENCE",{},"REVIEW_REVEAL")
            for scene in all_scenes:
                body={"participants":scene.participants,"facts":scene.facts,"result":scene.result}; paths=scanner.paths(body,target)
                prop_paths=scanner.paths(body,old) if old else []
                if target in (scene.participants or []) or paths or prop_paths:
                    affected_scenes.add(scene.id); add("SCENE_HISTORY","HIGH","EXACT" if target in (scene.participants or []) or prop_paths else "STRUCTURED","SCENE",scene.id,"DIRECT_REFERENCE" if target in (scene.participants or []) else "STRUCTURED_REFERENCE",{"reference_paths":paths+prop_paths},"REPLAY_FROM_SCENE")
            for proposal in all_proposals:
                body={"entry_state":proposal.entry_state,"character_motivations":proposal.character_motivations,"expected_progress":proposal.expected_progress,"possible_outcomes":proposal.possible_outcomes,"new_entity_requests":proposal.new_entity_requests,"risk_flags":proposal.risk_flags}; paths=scanner.paths(body,target)
                prop_hit=old and old in ((proposal.required_canon or [])+(proposal.allowed_reveals or [])+(proposal.forbidden_reveals or []))
                direct=target in (proposal.participants or []) or target==proposal.location_id or target in (proposal.required_canon or []) or target in (proposal.allowed_reveals or []) or target in (proposal.forbidden_reveals or [])
                if direct or paths or prop_hit:
                    affected_proposals.add(proposal.id); category="REVEAL_OR_FORESHADOWING" if prop_hit and (old in (proposal.allowed_reveals or []) or old in (proposal.forbidden_reveals or [])) else "REHEARSAL_ARTIFACT"; add(category,"LOW","EXACT" if direct or prop_hit else "STRUCTURED","SCENE_PROPOSAL",proposal.id,"DIRECT_REFERENCE" if direct or prop_hit else "STRUCTURED_REFERENCE",{"reference_paths":paths},"INVALIDATE_REHEARSAL")
            for decision in session.scalars(select(CharacterDecision).where(CharacterDecision.project_id==project_id)).all():
                paths=scanner.paths({"knowledge_used":decision.knowledge_used,"memory_refs":decision.memory_refs,"ability_refs":decision.ability_refs,"inventory_refs":decision.inventory_refs,"relationship_factors":decision.relationship_factors},target); prop=old and any(isinstance(x,dict) and x.get("proposition")==old for x in (decision.knowledge_used or []))
                if target in {decision.character_id,decision.target_character_id,decision.target_entity_id} or paths or prop: affected_decisions.add(decision.id); add("CHARACTER_DECISION","LOW","EXACT" if prop else "STRUCTURED","CHARACTER_DECISION",decision.id,"DIRECT_REFERENCE" if prop else "STRUCTURED_REFERENCE",{"reference_paths":paths},"INVALIDATE_REHEARSAL")
            for resolution in session.scalars(select(WorldResolution).where(WorldResolution.project_id==project_id)).all():
                paths=scanner.paths(resolution.objective_facts,target)
                if target in (resolution.canon_fact_ids_used or []) or target in (resolution.world_entity_ids_used or []) or paths: affected_resolutions.add(resolution.id); add("WORLD_RESOLUTION","LOW","STRUCTURED","WORLD_RESOLUTION",resolution.id,"STRUCTURED_REFERENCE",{"reference_paths":paths},"INVALIDATE_REHEARSAL")
            if typ=="WORLD_ENTITY":
                for entity in session.scalars(select(WorldEntity).where(WorldEntity.project_id==project_id,WorldEntity.id!=target)).all():
                    paths=scanner.paths(entity.profile,target)
                    if paths: add("STRUCTURED_REFERENCE","MEDIUM","STRUCTURED","WORLD_ENTITY",entity.id,"STRUCTURED_REFERENCE",{"reference_paths":paths},"REVIEW")
            if typ=="CANON_FACT":
                for fact in session.scalars(select(CanonFact).where(CanonFact.project_id==project_id,CanonFact.id!=target)).all():
                    paths=scanner.paths(fact.data,target)
                    if paths: add("STRUCTURED_REFERENCE","MEDIUM","STRUCTURED","CANON_FACT",fact.id,"STRUCTURED_REFERENCE",{"reference_paths":paths},"REVIEW")
            if typ=="CHARACTER":
                for char in session.scalars(select(Character).where(Character.project_id==project_id,Character.id!=target)).all():
                    rel=scanner.paths(char.relationships,target); other=scanner.paths({"current_state":char.current_state,"profile":char.profile,"inventory":char.inventory,"secrets":char.secrets},target)
                    if rel: add("RELATIONSHIP_DEPENDENCY","MEDIUM","STRUCTURED","CHARACTER",char.id,"STRUCTURED_REFERENCE",{"reference_paths":rel},"REVIEW_RELATIONSHIP")
                    if other: add("STRUCTURED_REFERENCE","MEDIUM","STRUCTURED","CHARACTER",char.id,"STRUCTURED_REFERENCE",{"reference_paths":other},"REVIEW")
            if any(x in change["path"].split("/") for x in {"world_time","happened_at","year","date","timeline"}): add("MANUAL_REVIEW","HIGH","MANUAL","REVISION",target,"TIMELINE_REVIEW_REQUIRED",{},"MANUAL_REVIEW")
        for performance in session.scalars(select(ScenePerformance).where(ScenePerformance.project_id==project_id)).all():
            if performance.scene_proposal_id in affected_proposals: affected_performances.add(performance.id); add("REHEARSAL_ARTIFACT","LOW","EXACT","SCENE_PERFORMANCE",performance.id,"AFFECTED_PROPOSAL",{},"INVALIDATE_REHEARSAL")
        for turn in session.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.project_id==project_id)).all():
            if turn.performance_id in affected_performances or turn.character_decision_id in affected_decisions or turn.id in {item.performance_turn_id for item in session.scalars(select(WorldResolution).where(WorldResolution.id.in_(affected_resolutions))).all()}:
                affected_turns.add(turn.id); add("REHEARSAL_ARTIFACT","LOW","EXACT","SCENE_PERFORMANCE_TURN",turn.id,"AFFECTED_ARTIFACT",{},"INVALIDATE_REHEARSAL")
        for memory in session.scalars(select(CharacterMemory).join(Character).where(Character.project_id==project_id)).all():
            if memory.source_scene in affected_scenes: add("MEMORY_DEPENDENCY","MEDIUM","EXACT","CHARACTER_MEMORY",memory.id,"AFFECTED_SOURCE_SCENE",{"scene_ids":[memory.source_scene]},"REVIEW_MEMORY")
        for chapter in session.scalars(select(Chapter).where(Chapter.project_id==project_id)).all():
            ids=sorted(set(chapter.source_scene_ids or []) & affected_scenes)
            if ids: add("CHAPTER_SOURCE","MEDIUM","EXACT","CHAPTER",chapter.id,"AFFECTED_SCENE_SOURCE",{"scene_ids":ids},"REVIEW_PROSE")
        earliest=min((item for item in all_scenes if item.id in affected_scenes),key=lambda x:(x.sequence,x.id),default=None)
        summary={"total_impacts":len(impacts),"critical":sum(x["severity"]=="CRITICAL" for x in impacts),"high":sum(x["severity"]=="HIGH" for x in impacts),"medium":sum(x["severity"]=="MEDIUM" for x in impacts),"manual_review":sum(x["certainty"]=="MANUAL" for x in impacts)}
        return {"summary":summary,"impacts":impacts,"earliest_affected_scene":{"id":earliest.id,"sequence":earliest.sequence} if earliest else None,"rehearsal_invalidations":[x for x in impacts if x["recommended_action"]=="INVALIDATE_REHEARSAL"],"author_override_required":locked_override}
