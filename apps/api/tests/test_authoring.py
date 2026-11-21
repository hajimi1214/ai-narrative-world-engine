import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

import app.api as api_module
from app.authoring import AuthorDraftService
from app.main import app
from app.models import (
    AutoDirectorRun, Chapter, ChapterWriterDraft, Project, Scene,
    VolumeContract, VolumeContractStatus,
)
from app.quality import QualityGateService
from app.writer import WriterDomainError, WriterProjectionService
from test_writer import render, session, writer_project  # shared isolated fixtures


def adopted(session, writer_project):
    chapter = writer_project[3]
    parent = render(session, chapter)
    WriterProjectionService().adopt(session, parent.id)
    session.flush()
    return chapter, parent


def edit_request(parent, **changes):
    return {
        "base_draft_id": parent.id, "expected_current_draft_id": parent.id,
        "content": "林修复了档案。纸页响了一声，他没有回头。", "client_request_id": "author-edit-1",
        **changes,
    }


def client_for(session, monkeypatch):
    session.commit()
    monkeypatch.setattr(api_module, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    return TestClient(app)


def test_author_edit_preserves_history_invalidates_approval_and_world(writer_project, session):
    chapter, parent = adopted(session, writer_project)
    assessment = QualityGateService().assess(session, chapter.id, {"config": {"require_critic": False}})
    assert assessment.status.value == "PASS"
    QualityGateService().approve(session, assessment.id)
    scene_before = [(item.id, item.facts, item.result) for item in session.scalars(select(Scene)).all()]

    draft = AuthorDraftService().save(session, chapter.id, edit_request(parent))
    assert draft.origin.value == "AUTHOR_EDIT" and draft.version == parent.version + 1
    assert draft.parent_draft_id == parent.id and draft.status.value == "ADOPTED"
    assert parent.content == "The hall remains quiet." and parent.status.value == "SUPERSEDED"
    assert chapter.content == draft.content and chapter.status == "DRAFT"
    assert chapter.current_quality_assessment_id is None and chapter.quality_status == "STALE"
    assert not assessment.active and assessment.status.value == "STALE"
    assert scene_before == [(item.id, item.facts, item.result) for item in session.scalars(select(Scene)).all()]
    replacement = QualityGateService().assess(session, chapter.id, {"config": {"require_critic": False}})
    assert replacement.content_fingerprint == draft.content_fingerprint


def test_author_edit_is_idempotent_and_rejects_old_current_version(writer_project, session):
    chapter, parent = adopted(session, writer_project)
    request = edit_request(parent)
    draft = AuthorDraftService().save(session, chapter.id, request)
    assert AuthorDraftService().save(session, chapter.id, request).id == draft.id
    assert session.scalar(select(func.count(ChapterWriterDraft.id))) == 2
    with pytest.raises(WriterDomainError, match="AUTHOR_EDIT_CONFLICT"):
        AuthorDraftService().save(session, chapter.id, edit_request(parent, client_request_id="second", content="旧页面修改"))
    assert chapter.content == draft.content


def test_author_edit_rejects_empty_and_cross_chapter_base(writer_project, session):
    chapter, parent = adopted(session, writer_project)
    with pytest.raises(WriterDomainError, match="AUTHOR_CONTENT_EMPTY"):
        AuthorDraftService().save(session, chapter.id, edit_request(parent, content="  \n "))
    other = Project(name="Other")
    session.add(other); session.flush()
    other_chapter = Chapter(project_id=other.id, number=1, active=True)
    session.add(other_chapter); session.flush()
    with pytest.raises(WriterDomainError, match="WRITER_DRAFT_NOT_FOUND"):
        AuthorDraftService().save(session, other_chapter.id, edit_request(parent, expected_current_draft_id=None))


def test_sealed_volume_blocks_manual_edit_render_and_replacement(writer_project, session):
    project, _, _, chapter = writer_project
    chapter, parent = adopted(session, writer_project)
    session.add(VolumeContract(project_id=project.id, volume_number=1, status=VolumeContractStatus.SEALED,
                              actual_chapter_start=chapter.number, actual_chapter_end=chapter.number))
    session.flush()
    with pytest.raises(WriterDomainError, match="VOLUME_SEALED"):
        AuthorDraftService().save(session, chapter.id, edit_request(parent))
    with pytest.raises(WriterDomainError, match="VOLUME_SEALED"):
        render(session, chapter)
    assert chapter.content == parent.content


def test_manual_edit_requires_automatic_run_to_pause(writer_project, session):
    chapter, parent = adopted(session, writer_project)
    session.add(AutoDirectorRun(project_id=chapter.project_id, idempotency_key="running", status="RUNNING"))
    session.flush()
    with pytest.raises(WriterDomainError, match="AUTHOR_EDIT_REQUIRES_PAUSE"):
        AuthorDraftService().save(session, chapter.id, edit_request(parent))


def test_manual_edit_http_scope_and_validation(writer_project, session, monkeypatch):
    chapter, parent = adopted(session, writer_project)
    client = client_for(session, monkeypatch)
    path = f"/projects/{chapter.project_id}/chapters/{chapter.id}/writer/edit"
    response = client.post(path, json=edit_request(parent))
    assert response.status_code == 201, response.text
    assert response.json()["origin"] == "AUTHOR_EDIT"
    assert client.post(path, json=edit_request(parent)).json()["id"] == response.json()["id"]
    assert client.post(path, json={"content": "missing lineage"}).status_code == 422
    assert client.post(path.replace(chapter.project_id, "foreign-project"), json=edit_request(parent)).status_code == 404


def test_manuscript_exports_saved_chapters_in_order_with_volume_scope(writer_project, session, monkeypatch):
    chapter, parent = adopted(session, writer_project)
    project = writer_project[0]
    session.add(Chapter(project_id=project.id, number=2, title="第二场", content="第二章已保存的正文。", active=True))
    volume = VolumeContract(project_id=project.id, volume_number=1, title="首卷", actual_chapter_start=1, actual_chapter_end=1)
    session.add(volume); session.flush()
    volume_id = volume.id
    client = client_for(session, monkeypatch)
    path = f"/projects/{project.id}/manuscript"
    response = client.get(path)
    assert response.status_code == 200 and response.content.startswith(b"\xef\xbb\xbf")
    assert response.text.index(parent.content) < response.text.index("第二章已保存的正文。")
    assert "attachment;" in response.headers["content-disposition"]
    assert response.headers["x-manuscript-chapters"] == "2"
    one = client.get(path, params={"format": "md", "volume_id": volume_id})
    assert one.status_code == 200 and "## " in one.text
    assert parent.content in one.text and "第二章已保存的正文。" not in one.text
    assert client.get(path, params={"format": "exe"}).status_code == 422
    assert client.get(path, params={"volume_id": "foreign"}).status_code == 404


def test_approved_manuscript_excludes_edited_working_text(writer_project, session, monkeypatch):
    chapter, parent = adopted(session, writer_project)
    assessment = QualityGateService().assess(session, chapter.id, {"config": {"require_critic": False}})
    QualityGateService().approve(session, assessment.id)
    client = client_for(session, monkeypatch)
    path = f"/projects/{chapter.project_id}/manuscript?approved_only=true"
    assert client.get(path).status_code == 200
    session.expire_all()
    AuthorDraftService().save(session, chapter.id, edit_request(parent))
    session.commit()
    response = client.get(path)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "MANUSCRIPT_EMPTY"


def test_old_approved_version_can_be_restored_as_new_draft_and_reaudited(writer_project, session):
    chapter, original = adopted(session, writer_project)
    original_assessment = QualityGateService().assess(session, chapter.id, {"config": {"require_critic": False}})
    QualityGateService().approve(session, original_assessment.id)

    first_edit = AuthorDraftService().save(
        session, chapter.id,
        edit_request(original, content="第一版人工修改留下的回声。", client_request_id="first-edit"),
    )
    current = AuthorDraftService().save(
        session, chapter.id,
        edit_request(first_edit, expected_current_draft_id=first_edit.id,
                     content="第二版人工修改覆盖了原来的收束。", client_request_id="second-edit"),
    )
    assert current.id == chapter.current_writer_draft_id
    assert first_edit.status.value == "SUPERSEDED"
    assert chapter.current_quality_assessment_id is None

    # Restoring v2 saves its text as v4 and leaves v1/v2/v3 immutable history.
    restored = AuthorDraftService().save(
        session, chapter.id,
        edit_request(first_edit, expected_current_draft_id=current.id,
                     content=first_edit.content, client_request_id="restore-edit-v2"),
    )
    assert restored.version == 4
    assert restored.origin.value == "AUTHOR_EDIT"
    assert restored.parent_draft_id == first_edit.id
    assert restored.content == first_edit.content == "第一版人工修改留下的回声。"
    assert restored.status.value == "ADOPTED"
    assert chapter.content == first_edit.content
    assert original.status.value == "SUPERSEDED"
    assert first_edit.status.value == "SUPERSEDED"
    assert current.status.value == "SUPERSEDED"
    assert chapter.quality_status == "STALE"

    assessment = QualityGateService().assess(session, chapter.id, {"config": {"require_critic": False}})
    assert assessment.status.value == "PASS"
    assert assessment.writer_draft_id == restored.id
    assert assessment.content_fingerprint == restored.content_fingerprint
    QualityGateService().approve(session, assessment.id)
    assert chapter.current_quality_assessment_id == assessment.id
    assert chapter.status == "QUALITY_APPROVED"


def test_restore_historical_version_through_api_creates_new_immutable_revisions(writer_project, session, monkeypatch):
    chapter, original = adopted(session, writer_project)
    first = AuthorDraftService().save(
        session, chapter.id,
        edit_request(original, content="旧版里保存的尾声。", client_request_id="api-first-edit"),
    )
    current = AuthorDraftService().save(
        session, chapter.id,
        edit_request(first, expected_current_draft_id=first.id,
                     content="当前版后来改写了尾声。", client_request_id="api-second-edit"),
    )
    session.commit()
    client = client_for(session, monkeypatch)
    path = f"/projects/{chapter.project_id}/chapters/{chapter.id}/writer/edit"
    payload = {
        "base_draft_id": first.id,
        "expected_current_draft_id": current.id,
        "content": first.content,
        "client_request_id": "api-restore-v2",
    }

    restored = client.post(path, json=payload)
    assert restored.status_code == 201, restored.text
    body = restored.json()
    assert body["origin"] == "AUTHOR_EDIT"
    assert body["parent_draft_id"] == first.id
    assert body["version"] == 4
    assert body["content"] == "旧版里保存的尾声。"
    assert body["status"] == "ADOPTED"

    repeated = client.post(path, json=payload)
    assert repeated.status_code == 201 and repeated.json()["id"] == body["id"]
    stale = client.post(path, json={**payload, "expected_current_draft_id": current.id, "client_request_id": "api-stale-tab"})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "AUTHOR_EDIT_CONFLICT"
    with session.bind.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(ChapterWriterDraft.__table__)) == 4


def test_restore_historical_version_rejects_sealed_chapter(writer_project, session):
    project, _, _, chapter = writer_project
    chapter, original = adopted(session, writer_project)
    older = AuthorDraftService().save(session, chapter.id, edit_request(original, content="历史版本正文。", client_request_id="sealed-history-edit"))
    current = AuthorDraftService().save(session, chapter.id, edit_request(older, expected_current_draft_id=older.id, content="当前正文。", client_request_id="sealed-current-edit"))
    session.add(VolumeContract(project_id=project.id, volume_number=1, status=VolumeContractStatus.SEALED, actual_chapter_start=1, actual_chapter_end=1))
    session.flush()
    with pytest.raises(WriterDomainError, match="VOLUME_SEALED"):
        AuthorDraftService().save(session, chapter.id, edit_request(older, expected_current_draft_id=current.id, content=older.content, client_request_id="sealed-restore"))
    assert chapter.current_writer_draft_id == current.id and chapter.content == "当前正文。"
