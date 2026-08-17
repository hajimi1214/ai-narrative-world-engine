import copy, hashlib, json
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import CanonFact, Character, CharacterKnowledge, CharacterMemory, Chapter, Project, RevealConstraint, Scene, StoryArc, StoryThread, WorldEntity, WorldSnapshot, SnapshotType, RevisionApplication, RevisionApplicationStatus, WorldRevision, RevisionStatus
from .revision import RevisionChangeNormalizer, RevisionStateFingerprintBuilder, _record

class WorldSnapshotBuilder:
    MODELS=(CanonFact,WorldEntity,Character,CharacterKnowledge,CharacterMemory,RevealConstraint,StoryThread,StoryArc,Scene,Chapter)
    def build(self,db:Session,project_id:str):
        chars=db.scalars(select(Character).where(Character.project_id==project_id)).all(); ids=[x.id for x in chars]
        data={"project":{k:v for k,v in _record(db.get(Project,project_id)).items() if k in {"id","status","creation_mode","story_seed","current_world_time"}}}
        for model in self.MODELS:
            rows=db.scalars(select(model).where(model.character_id.in_(ids)).order_by(model.id)).all() if model in (CharacterKnowledge,CharacterMemory) and ids else (db.scalars(select(model).where(model.project_id==project_id).order_by(model.id)).all() if model not in (CharacterKnowledge,CharacterMemory) else [])
            values=[]
            for row in rows:
                item=_record(row); item.pop("created_at",None); item.pop("updated_at",None)
                if model is Chapter: item.pop("content",None)
                values.append(item)
            data[model.__tablename__]=values
        stable=json.dumps(data,sort_keys=True,separators=(",",":"),default=str); return data,"world-snapshot-v1:"+hashlib.sha256(stable.encode()).hexdigest()
    def create(self,db,project_id,kind,revision_id=None):
        payload,fp=self.build(db,project_id); snap=WorldSnapshot(project_id=project_id,snapshot_type=kind,state_fingerprint=fp,payload=payload,source_revision_id=revision_id); db.add(snap); db.flush(); return snap

class RevisionApplyService:
    def apply(self,db,project_id,revision,override,reason):
        if revision.status!=RevisionStatus.PREVIEWED: raise ValueError("REVISION_NOT_PREVIEWED")
        actual=RevisionStateFingerprintBuilder().build(db,project_id)
        if actual!=revision.base_state_fingerprint: revision.status=RevisionStatus.STALE; raise ValueError("REVISION_STALE")
        if revision.impact_report.get("author_override_required") and (not override or not (reason or "").strip()): raise ValueError("AUTHOR_OVERRIDE_REQUIRED")
        from .revision import RevisionChangePayload
        changes=RevisionChangeNormalizer().normalize(db,project_id,[RevisionChangePayload.model_validate(x) for x in revision.change_set])
        if [x["target_fingerprint_before"] for x in changes] != [x.get("target_fingerprint_before") for x in revision.normalized_changes]: raise ValueError("TARGET_STATE_STALE")
        pre=WorldSnapshotBuilder().create(db,project_id,SnapshotType.PRE_REVISION,revision.id); app=RevisionApplication(project_id=project_id,revision_id=revision.id,status=RevisionApplicationStatus.PENDING,pre_snapshot_id=pre.id,expected_base_fingerprint=revision.base_state_fingerprint,actual_base_fingerprint=actual,author_override=override,author_override_reason=reason,applied_change_count=0); db.add(app); db.flush()
        models={"CANON_FACT":CanonFact,"WORLD_ENTITY":WorldEntity,"CHARACTER":Character}
        for target_type,target_id in {(x["target_type"],x["target_id"]) for x in changes}:
            target=db.get(models[target_type],target_id); final=next(x for x in changes if x["target_type"]==target_type and x["target_id"]==target_id)
            # Reconstruct final virtual value by applying normalizer's grouped result from changes through patch preview.
            for change in [x for x in changes if x["target_type"]==target_type and x["target_id"]==target_id]:
                parts=change["path"].strip("/").split("/"); obj=_record(target); cursor=obj
                for part in parts[:-1]: cursor=cursor[int(part)] if isinstance(cursor,list) else cursor[part]
                if change["operation"]=="REMOVE": cursor.pop(parts[-1],None) if isinstance(cursor,dict) else cursor.pop(int(parts[-1]))
                elif change["operation"]=="MERGE": cursor[parts[-1]]={**cursor[parts[-1]],**change["after_value"]}
                else: cursor[parts[-1]]=change["after_value"]
                for key,value in obj.items():
                    if key not in {"id","project_id","created_at","updated_at"}: setattr(target,key,value)
            db.flush()
        post=WorldSnapshotBuilder().create(db,project_id,SnapshotType.POST_REVISION,revision.id); app.post_snapshot_id=post.id; app.status=RevisionApplicationStatus.APPLIED; app.applied_change_count=len(changes); app.completed_at=datetime.utcnow(); revision.status=RevisionStatus.APPLIED; return app

    def rollback(self,db,project_id,application):
        latest=db.scalar(select(RevisionApplication).where(RevisionApplication.project_id==project_id,RevisionApplication.status==RevisionApplicationStatus.APPLIED).order_by(RevisionApplication.completed_at.desc(),RevisionApplication.id.desc()))
        if not latest or latest.id!=application.id: raise ValueError("ROLLBACK_NOT_LATEST")
        snapshot=db.get(WorldSnapshot,application.pre_snapshot_id); revision=db.get(WorldRevision,application.revision_id)
        from .revision import RevisionChangePayload
        targets={(item.target_type,item.target_id) for item in [RevisionChangePayload.model_validate(x) for x in revision.change_set]}; models={"CANON_FACT":(CanonFact,"canon_facts"),"WORLD_ENTITY":(WorldEntity,"world_entities"),"CHARACTER":(Character,"characters")}
        for target_type,target_id in targets:
            model,key=models[target_type]; saved=next(item for item in snapshot.payload[key] if item["id"]==target_id); target=db.get(model,target_id)
            for field,value in saved.items():
                if field not in {"id","project_id","created_at","updated_at"}: setattr(target,field,value)
        application.status=RevisionApplicationStatus.ROLLED_BACK; application.completed_at=datetime.utcnow(); revision.status=RevisionStatus.ROLLED_BACK; WorldSnapshotBuilder().create(db,project_id,SnapshotType.ROLLBACK_POINT,revision.id); return application
