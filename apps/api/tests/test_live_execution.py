from app.live_execution import LiveExecutionBroker


def test_live_execution_snapshot_keeps_bounded_preview_and_versions():
    broker = LiveExecutionBroker()
    session_id = broker.begin(project_id="project-1", trace_id="trace-1", label="整书规划", provider="fake", model="test", stage="STORY_PLAN")
    broker.phase(session_id, "GENERATING", "正在生成")
    broker.append(session_id, "第一段")
    version, sessions = broker.wait_for_snapshots("project-1", -1, timeout=0)
    assert version > 0
    assert sessions[0]["phase"] == "STREAMING"
    assert sessions[0]["preview"] == "第一段"
    broker.complete(session_id, "已保存")
    assert broker.snapshots("project-1")[0]["status"] == "COMPLETED"
