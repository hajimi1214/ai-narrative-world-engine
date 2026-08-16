from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import pytest
from app.db import Base
from app.models import AntiAIBible, CanonFact, CanonType, Character, CharacterKnowledge, KnowledgeStatus, Project, Scene, WritingBible
from app.services import DomainRuleError, activate_anti_ai_bible, activate_writing_bible, update_canon

@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        yield db

def test_project_and_scene_are_independent(session):
    project = Project(name="Test world")
    session.add(project); session.commit()
    scene = Scene(project_id=project.id, sequence=1, summary="A first event")
    session.add(scene); session.commit()
    assert scene.id and scene.project_id == project.id

def test_locked_core_canon_cannot_be_overwritten(session):
    project = Project(name="Test world")
    session.add(project); session.flush()
    fact = CanonFact(project_id=project.id, fact_type=CanonType.CORE_CANON, proposition="The moon is blue", locked=True)
    session.add(fact); session.commit()
    with pytest.raises(DomainRuleError): update_canon(session, fact, proposition="The moon is red")

def test_character_knowledge_is_separate_from_world_fact(session):
    project = Project(name="Test world"); session.add(project); session.flush()
    character = Character(project_id=project.id, name="A")
    session.add(character); session.commit()
    knowledge = CharacterKnowledge(character_id=character.id, proposition="A secret", status=KnowledgeStatus.SUSPECTED)
    session.add(knowledge); session.commit()
    assert session.scalar(select(CharacterKnowledge).where(CharacterKnowledge.character_id == character.id)).proposition == "A secret"

def test_only_one_writing_bible_and_anti_ai_bible_is_active(session):
    project = Project(name="Test world"); session.add(project); session.commit()
    wb1 = WritingBible(project_id=project.id, version=1, active=True); wb2 = WritingBible(project_id=project.id, version=2)
    ab1 = AntiAIBible(project_id=project.id, version=1, active=True); ab2 = AntiAIBible(project_id=project.id, version=2)
    session.add_all([wb1, wb2, ab1, ab2]); session.commit()
    activate_writing_bible(session, wb2); activate_anti_ai_bible(session, ab2)
    assert session.scalar(select(WritingBible).where(WritingBible.project_id == project.id, WritingBible.active.is_(True))).version == 2
    assert session.scalar(select(AntiAIBible).where(AntiAIBible.project_id == project.id, AntiAIBible.active.is_(True))).version == 2
