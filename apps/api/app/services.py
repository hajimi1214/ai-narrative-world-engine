from sqlalchemy import select, update
from sqlalchemy.orm import Session
from .models import AntiAIBible, CanonFact, CanonType, WritingBible

class DomainRuleError(ValueError):
    pass

def update_canon(session: Session, fact: CanonFact, **changes) -> CanonFact:
    if fact.locked and fact.fact_type == CanonType.CORE_CANON:
        raise DomainRuleError("locked CORE_CANON facts cannot be overwritten")
    for key, value in changes.items():
        setattr(fact, key, value)
    session.add(fact)
    session.commit()
    session.refresh(fact)
    return fact

def activate_writing_bible(session: Session, bible: WritingBible) -> WritingBible:
    session.execute(update(WritingBible).where(WritingBible.project_id == bible.project_id).values(active=False))
    bible.active = True
    session.add(bible)
    session.commit()
    session.refresh(bible)
    return bible

def activate_anti_ai_bible(session: Session, bible: AntiAIBible) -> AntiAIBible:
    session.execute(update(AntiAIBible).where(AntiAIBible.project_id == bible.project_id).values(active=False))
    bible.active = True
    session.add(bible)
    session.commit()
    session.refresh(bible)
    return bible

def active_bible(session: Session, model, project_id: str):
    return session.scalar(select(model).where(model.project_id == project_id, model.active.is_(True)))
