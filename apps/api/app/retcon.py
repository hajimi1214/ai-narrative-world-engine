"""Deterministic, preview-only historical impact planning."""
from __future__ import annotations
import hashlib, json
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from sqlalchemy import cast, or_, select, Text
from sqlalchemy.orm import Session
from .models import (CanonFact, Character, CharacterDecision, CharacterKnowledge, CharacterMemory, Scene, ScenePerformanceTurn, WorldEntity, WorldResolution, RevealConstraint, RetconImpactItem, RetconImpactPlan, RetconRequest, WorldRevision)
from .revision import StructuredReferenceScanner, _record

CLASSIFICATION_LABELS = {"UNCHANGED":"可以保留","REVALIDATE":"需要重新验证","REBUILD_COGNITION":"需要重建认知","REPLAY_REQUIRED":"需要重新演出","INVALIDATED":"修改后将失效"}
def semantic_fingerprint(value: Any) -> str:
    stable=json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(",",":"),default=str)
    return "retcon-basis-v1:"+hashlib.sha256(stable.encode()).hexdigest()

@dataclass(frozen=True)
class DependencyEdge:
    source_type:str; source_id:str; target_type:str; target_id:str; edge_type:str; reason:str; path:list[dict[str,str]]; certainty:str = "CONFIRMED"

class HistoricalDependencyGraphBuilder:
    """Target-driven BFS over explicit FK and structured JSON lineage."""
    def __init__(self,max_nodes:int=10000,max_edges:int|None=None):
        self.scanner=StructuredReferenceScanner(); self.max_nodes=max_nodes; self.max_edges=max_edges or max_nodes*4
        self.limit_reached=False; self.visited_nodes=set(); self.visited_edges=set()
    def _candidate_json(self,session,model,project_id,fields,needles):
        clauses=[]
        for field in fields:
            column=getattr(model,field,None)
            if column is not None:
                clauses.extend(cast(column,Text).contains(str(n)) for n in needles)
        return session.scalars(select(model).where(model.project_id==project_id,or_(*clauses))).all() if clauses else []
    def build(self,session:Session,project_id:str,target_ids:set[str],old_values:set[str],target_types:dict[str,str]|None=None):
        target_types=target_types or {}; queue=deque(); node_paths={}; node_certainty={}; edges=[]
        def enqueue(node,path=None):
            if node not in node_paths: node_paths[node]=path or [{"type":node[0],"id":node[1],"certainty":"CONFIRMED"}]; node_certainty[node]="CONFIRMED"; queue.append(node)
        for ident in sorted(target_ids):
            typ=target_types.get(ident)
            if typ is None:
                for model,label in ((CanonFact,"CANON_FACT"),(WorldEntity,"WORLD_ENTITY"),(Character,"CHARACTER")):
                    if session.get(model,ident): typ=label; break
            if typ: enqueue((typ,ident))
        def add(source,target,edge_type,reason,evidence_path=None):
            if target not in node_paths and len(node_paths)>=self.max_nodes: self.limit_reached=True; return
            key=(*source,*target,edge_type)
            if key in self.visited_edges: return
            if len(self.visited_edges)>=self.max_edges: self.limit_reached=True; return
            certainty = "UNCERTAIN" if edge_type in {"EXACT_VALUE_WITHOUT_LINEAGE", "DEPENDENCY_LINEAGE_MISSING"} or node_certainty.get(source) == "UNCERTAIN" else "CONFIRMED"
            self.visited_edges.add(key); path=list(node_paths[source]); path.append({"type":target[0],"id":target[1],"certainty":certainty,**({"path":evidence_path} if evidence_path else {})})
            edges.append(DependencyEdge(source[0],source[1],target[0],target[1],edge_type,reason,path,certainty))
            if target not in node_paths:
                node_paths[target]=path; node_certainty[target]=certainty; queue.append(target)
            elif certainty == "CONFIRMED" and node_certainty.get(target) == "UNCERTAIN" or (certainty == node_certainty.get(target) and len(path)<len(node_paths[target])):
                node_paths[target]=path; node_certainty[target]=certainty
        def exact(value,needles):
            for needle in sorted(needles):
                paths=self.scanner.paths(value,needle)
                if paths:return needle,paths[0]
            return None,None
        while queue and not self.limit_reached:
            source=queue.popleft()
            if source in self.visited_nodes: continue
            self.visited_nodes.add(source); typ,ident=source; needles=set(target_ids)|set(old_values)
            if typ=="CANON_FACT":
                for row in session.scalars(select(CharacterKnowledge).join(Character).where(Character.project_id==project_id,CharacterKnowledge.source==ident)).all(): add(source,("CHARACTER_KNOWLEDGE",row.id),"CANON_TO_KNOWLEDGE","人物认知直接来自待修改世界事实")
                for row in self._candidate_json(session,Scene,project_id,["participants","facts","result"],needles):
                    hit,path=exact({"participants":row.participants,"facts":row.facts,"result":row.result},{ident}|old_values)
                    if hit:add(source,("SCENE",row.id),"EXPLICIT_SCENE_REFERENCE","场景包含结构化历史事实引用",path)
                if old_values:
                    for row in session.scalars(select(CharacterKnowledge).join(Character).where(Character.project_id==project_id,CharacterKnowledge.proposition.in_(old_values))).all():
                        if row.source!=ident:add(source,("CHARACTER_KNOWLEDGE",row.id),"EXACT_VALUE_WITHOUT_LINEAGE","认知文本与旧命题完全相等但缺少来源链")
                for row in session.scalars(select(RevealConstraint).where(RevealConstraint.project_id==project_id,RevealConstraint.canon_fact_id==ident)).all():add(source,("REVEAL_CONSTRAINT",row.id),"CANON_TO_REVEAL","揭示约束直接引用世界事实")
                for row in self._candidate_json(session,CanonFact,project_id,["data"],{ident}):
                    if row.id!=ident:
                        hit,path=exact(row.data,{ident})
                        if hit:add(source,("CANON_FACT",row.id),"CANON_STRUCTURED_REFERENCE","世界事实包含结构化事实引用",path)
            elif typ=="WORLD_ENTITY":
                for row in self._candidate_json(session,WorldEntity,project_id,["profile"],{ident}):
                    if row.id!=ident:
                        hit,path=exact(row.profile,{ident})
                        if hit:add(source,("WORLD_ENTITY",row.id),"ENTITY_STRUCTURED_REFERENCE","实体资料包含结构化实体引用",path)
                for row in self._candidate_json(session,Character,project_id,["current_state","profile","inventory","secrets"],{ident}):
                    if row.id!=ident:
                        hit,path=exact({"current_state":row.current_state,"profile":row.profile,"inventory":row.inventory,"secrets":row.secrets},{ident})
                        if hit:add(source,("CHARACTER",row.id),"ENTITY_STRUCTURED_REFERENCE","人物状态包含结构化实体引用",path)
            elif typ=="CHARACTER":
                for row in self._candidate_json(session,Character,project_id,["relationships","current_state","profile","inventory","secrets"],{ident}):
                    if row.id!=ident:
                        body={"relationships":row.relationships,"current_state":row.current_state,"profile":row.profile,"inventory":row.inventory,"secrets":row.secrets}; hit,path=exact(body,{ident})
                        if hit:add(source,("CHARACTER",row.id),"RELATIONSHIP_STRUCTURED_REFERENCE" if path.startswith("/relationships") else "CHARACTER_STRUCTURED_REFERENCE","人物包含结构化角色引用",path)
                for row in self._candidate_json(session,Scene,project_id,["participants","facts","result"],{ident}):
                    hit,path=exact({"participants":row.participants,"facts":row.facts,"result":row.result},{ident})
                    if hit:add(source,("SCENE",row.id),"EXPLICIT_SCENE_REFERENCE","场景参与人物包含目标角色",path)
            elif typ=="SCENE":
                for row in session.scalars(select(CharacterKnowledge).join(Character).where(Character.project_id==project_id,CharacterKnowledge.source==ident)).all():add(source,("CHARACTER_KNOWLEDGE",row.id),"SCENE_TO_KNOWLEDGE","人物认知来源于受影响场景")
                for row in session.scalars(select(CharacterMemory).join(Character).where(Character.project_id==project_id,CharacterMemory.source_scene==ident)).all():add(source,("CHARACTER_MEMORY",row.id),"SCENE_TO_MEMORY","人物记忆来源于受影响场景")
            elif typ=="CHARACTER_KNOWLEDGE":
                for row in self._candidate_json(session,CharacterDecision,project_id,["knowledge_used"],{ident}):
                    hit,path=exact(row.knowledge_used,{ident})
                    if hit:add(source,("CHARACTER_DECISION",row.id),"KNOWLEDGE_TO_DECISION","角色决策使用受影响认知",path)
            elif typ=="CHARACTER_MEMORY":
                for row in self._candidate_json(session,CharacterDecision,project_id,["memory_refs","relationship_factors","inventory_refs","ability_refs"],{ident}):
                    hit,path=exact({"memory_refs":row.memory_refs,"relationship_factors":row.relationship_factors,"inventory_refs":row.inventory_refs,"ability_refs":row.ability_refs},{ident})
                    if hit:add(source,("CHARACTER_DECISION",row.id),"MEMORY_TO_DECISION","角色决策引用受影响记忆",path)
            elif typ=="CHARACTER_DECISION":
                for row in session.scalars(select(ScenePerformanceTurn).where(ScenePerformanceTurn.project_id==project_id,ScenePerformanceTurn.character_decision_id==ident)).all():add(source,("SCENE_PERFORMANCE_TURN",row.id),"DECISION_TO_TURN","演出回合使用受影响角色决策")
            elif typ=="SCENE_PERFORMANCE_TURN":
                for row in session.scalars(select(WorldResolution).where(WorldResolution.project_id==project_id,WorldResolution.performance_turn_id==ident)).all():add(source,("WORLD_RESOLUTION",row.id),"TURN_TO_RESOLUTION","世界响应属于受影响演出回合")
        return edges

class ImpactClassificationResolver:
    def classify(self,edge):
        if edge.certainty == "UNCERTAIN" or edge.edge_type in {"EXACT_VALUE_WITHOUT_LINEAGE", "DEPENDENCY_LINEAGE_MISSING"}:return "REVALIDATE",edge.edge_type
        if edge.target_type=="CHARACTER_KNOWLEDGE":return "REBUILD_COGNITION","该人物认知直接依赖被修改的世界事实。"
        if edge.target_type=="CHARACTER_MEMORY":return "REBUILD_COGNITION","该人物记忆包含被修改事实的结构化依赖。"
        if edge.target_type in {"CHARACTER_DECISION","SCENE_PERFORMANCE_TURN"}:return "REPLAY_REQUIRED","演出使用了需要重新建立的认知或决策。"
        if edge.target_type=="WORLD_RESOLUTION":return "INVALIDATED","世界响应属于需要重新演出的回合。"
        if edge.target_type=="SCENE":return "REPLAY_REQUIRED","场景存在明确的结构化历史依赖。"
        if edge.target_type=="REVEAL_CONSTRAINT":return "REVALIDATE","揭示约束直接引用被修改事实。"
        return "REVALIDATE","存在明确结构化引用，需要重新验证。"

class ReplayBoundaryFinder:
    def find(self,scenes,affected_scene_ids):
        affected=[s for s in scenes if s.id in affected_scene_ids]
        if not affected:return None,None,{"issues":[]}
        all_sequences=[s.sequence for s in scenes]
        if any(s.sequence is None for s in scenes) or len(all_sequences)!=len(set(all_sequences)) or any(s.sequence is None for s in affected) or len({s.sequence for s in affected})!=len(affected):return None,None,{"issues":[{"code":"REPLAY_BOUNDARY_UNRESOLVED","severity":"BLOCKING","message":"项目历史场景存在无法唯一确定的顺序。"}]}
        first=min(affected,key=lambda s:s.sequence); return first.id,first.sequence,{"issues":[]}

class CharacterCognitionImpactPlanner:
    def plan(self,session,project_id,items):
        ids={i.get("character_id") for i in items if i.get("character_id")}; out=[]
        for cid in sorted(ids):out.append({"character_id":cid,"affected_knowledge_ids":sorted({i["resource_id"] for i in items if i.get("character_id")==cid and i["resource_type"]=="CHARACTER_KNOWLEDGE"}),"affected_memory_ids":sorted({i["resource_id"] for i in items if i.get("character_id")==cid and i["resource_type"]=="CHARACTER_MEMORY"}),"reason":"来自受影响世界事实的明确认知依赖"})
        return out

class RetconBasisFingerprintBuilder:
    MODELS={"CANON_FACT":CanonFact,"WORLD_ENTITY":WorldEntity,"CHARACTER":Character,"CHARACTER_KNOWLEDGE":CharacterKnowledge,"CHARACTER_MEMORY":CharacterMemory,"CHARACTER_DECISION":CharacterDecision,"SCENE":__import__("app.models",fromlist=["Scene"]).Scene,"SCENE_PERFORMANCE_TURN":ScenePerformanceTurn,"WORLD_RESOLUTION":WorldResolution}
    def build(self,session,project_id,revision,edges=None):
        normalized=revision.normalized_changes or []; target_ids={i["target_id"] for i in normalized}; old={str(i["before_value"]) for i in normalized if i.get("path")=="/proposition" and i.get("before_value") is not None}
        if edges is None: edges=HistoricalDependencyGraphBuilder().build(session,project_id,target_ids,old,{i["target_id"]:i["target_type"] for i in normalized})
        seen={(i["target_type"],i["target_id"]) for i in normalized}
        for e in edges:seen.update(((e.source_type,e.source_id),(e.target_type,e.target_id)))
        data=[]
        for typ,ident in sorted(seen):
            model=self.MODELS.get(typ); row=session.get(model,ident) if model else None
            if not row:continue
            data.append({"type":typ,"id":ident,"state":self._semantic_state(typ,row)})
        data.append({"type":"SOURCE_REVISION","id":revision.id,"status":getattr(revision.status,"value",revision.status),"normalized_changes":sorted(normalized,key=lambda x:json.dumps(x,sort_keys=True,default=str))})
        return semantic_fingerprint(sorted(data,key=lambda x:(x["type"],x["id"],json.dumps(x,sort_keys=True,default=str))))
    def _semantic_state(self,typ,row):
        record=_record(row)
        record.pop("created_at",None); record.pop("updated_at",None)
        fields={
            "CANON_FACT":["fact_type","proposition","data","locked"], "WORLD_ENTITY":["entity_type","name","profile","active"],
            "CHARACTER":["name","profile","personality","core_values","boundaries","goals","current_state","physical_state","emotional_state","abilities","voice_profile","relationships","inventory","secrets","active"],
            "CHARACTER_KNOWLEDGE":["proposition","status","source","confidence"],
            "CHARACTER_MEMORY":["content","importance","emotional_weight","confidence","distortion","source_scene","happened_at"],
            "CHARACTER_DECISION":["knowledge_used","memory_refs","ability_refs","inventory_refs","relationship_factors","chosen_action","decision_type","status"],
            "SCENE":["sequence","participants","facts","result","story_threads","status","location_id","world_time"],
            "SCENE_PERFORMANCE_TURN":["character_decision_id","observable_action","recipient_character_ids","requires_world_resolution","world_resolution_request"],
            "WORLD_RESOLUTION":["performance_turn_id","status","outcome","objective_facts","canon_fact_ids_used","world_entity_ids_used","recipient_character_ids","actor_observation","public_observation"],
        }
        return {key:record.get(key) for key in fields.get(typ,[]) if key in record}

class RetconPlanStalenessChecker:
    def is_stale(self,session,plan):
        request=session.get(RetconRequest,plan.retcon_request_id)
        revision=session.get(WorldRevision,request.source_revision_id) if request else None
        if not request or not revision or revision.project_id != plan.project_id or getattr(revision.status,"value",revision.status) != "PREVIEWED": return True
        return RetconBasisFingerprintBuilder().build(session,plan.project_id,revision)!=plan.basis_fingerprint

class RetconImpactPlanner:
    def analyze(self,session,request,revision):
        normalized=revision.normalized_changes or []; ids={i["target_id"] for i in normalized}; types={i["target_id"]:i["target_type"] for i in normalized}; old={str(i["before_value"]) for i in normalized if i.get("path")=="/proposition" and i.get("before_value") is not None}
        builder=HistoricalDependencyGraphBuilder(); edges=builder.build(session,request.project_id,ids,old,types); resolver=ImpactClassificationResolver(); items=[]; affected={e.target_id for e in edges if e.target_type=="SCENE"}
        for e in edges:
            cls,reason=resolver.classify(e); cid=None
            if e.target_type in {"CHARACTER_KNOWLEDGE","CHARACTER_MEMORY"}:
                row=session.get(CharacterKnowledge if e.target_type=="CHARACTER_KNOWLEDGE" else CharacterMemory,e.target_id); cid=row.character_id if row else None
            items.append(RetconImpactItem(plan_id="",resource_type=e.target_type,resource_id=e.target_id,classification=cls,reason_code=e.edge_type,reason_summary=reason,character_id=cid,scene_id=e.target_id if e.target_type=="SCENE" else None,dependency_path=e.path))
        scene_meta=[SimpleNamespace(id=row[0],sequence=row[1]) for row in session.execute(select(Scene.id,Scene.sequence).where(Scene.project_id==request.project_id)).all()]
        scenes=scene_meta
        earliest_id,earliest_seq,validation=ReplayBoundaryFinder().find(scenes,affected)
        if builder.limit_reached:validation["issues"].append({"code":"PLAN_GRAPH_LIMIT_REACHED","severity":"BLOCKING","resource_type":"RETCON_GRAPH","resource_id":request.id,"message":"依赖图超过规划节点上限。"})
        cognition=CharacterCognitionImpactPlanner().plan(session,request.project_id,[{"resource_type":i.resource_type,"resource_id":i.resource_id,"character_id":i.character_id} for i in items])
        affected_sequences=sorted(s.sequence for s in scenes if s.id in affected); preserved=[s.sequence for s in scenes if s.id not in affected]
        ranges=[]
        for sequence in preserved:
            if not ranges or sequence != ranges[-1]["sequence_end"]+1:ranges.append({"sequence_start":sequence,"sequence_end":sequence,"count":1})
            else:ranges[-1]["sequence_end"]=sequence; ranges[-1]["count"]+=1
        summary={k:sum(i.classification==k for i in items) for k in ("REVALIDATE","REBUILD_COGNITION","REPLAY_REQUIRED","INVALIDATED")}; summary.update(total_impacts=len(items),affected_characters=len({i.character_id for i in items if i.character_id}),replay_scene_count=sum(i.resource_type=="SCENE" and i.classification=="REPLAY_REQUIRED" for i in items),preserved_scene_count=len(preserved),preserved_scene_ranges=ranges,cognition_impacts=cognition)
        parent=session.scalar(select(RetconImpactPlan).where(RetconImpactPlan.retcon_request_id==request.id).order_by(RetconImpactPlan.version.desc())); blocked=bool(validation["issues"])
        plan=RetconImpactPlan(project_id=request.project_id,retcon_request_id=request.id,version=request.current_plan_version+1,parent_plan_id=parent.id if parent else None,basis_fingerprint=RetconBasisFingerprintBuilder().build(session,request.project_id,revision,edges),status="BLOCKED" if blocked else "READY",earliest_affected_scene_id=earliest_id,earliest_affected_sequence=earliest_seq,impact_summary=summary,validation_report=validation)
        return plan,items
