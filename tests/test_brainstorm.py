from orchestrator.core.brainstorm import BrainstormSession, parse_stream_line


def test_session_has_id_and_workspace(tmp_path):
    s = BrainstormSession(session_id="abc", workspace=str(tmp_path / "abc"))
    assert s.session_id == "abc"
    assert s.workspace.endswith("abc")


def test_build_args_includes_resume_and_flags():
    s = BrainstormSession(session_id="abc", workspace="/ws")
    args = s._build_args("hello", resume=True)
    assert "--resume" in args and "abc" in args  # noqa: PT018
    assert "--dangerously-skip-permissions" in args
    assert "--output-format" in args and "stream-json" in args  # noqa: PT018
    assert "-p" in args and "hello" in args  # noqa: PT018


def test_parse_assistant_text():
    line = (
        '{"type":"assistant","message":{"content":[{"type":"text","text":"Hi there"}]}}'
    )
    assert parse_stream_line(line) == {"kind": "text", "text": "Hi there"}


def test_parse_result_marks_done():
    line = '{"type":"result","session_id":"abc","is_error":false}'
    assert parse_stream_line(line) == {"kind": "result", "session_id": "abc"}


def test_parse_unknown_returns_none():
    assert parse_stream_line('{"type":"system"}') is None
    assert parse_stream_line("not json") is None


async def test_run_turn_publishes_text(mocker):
    from orchestrator.core.brainstorm import BrainstormSession

    published = []
    bus = mocker.MagicMock()
    bus.publish = lambda e: published.append(e)
    s = BrainstormSession(session_id="abc", workspace="/ws", event_bus=bus)

    async def fake_lines():
        yield '{"type":"assistant","message":{"content":[{"type":"text","text":"Q1?"}]}}'
        yield '{"type":"result","session_id":"abc","is_error":false}'

    mocker.patch.object(s, "_stream_lines", return_value=fake_lines())
    await s.run_turn("hello", resume=False)
    texts = [e for e in published if e.get("type") == "brainstorm_message"]
    assert any(e["text"] == "Q1?" for e in texts)


def test_manager_starts_session(mocker, tmp_path):
    from orchestrator.core.brainstorm import BrainstormManager

    mgr = BrainstormManager(
        workspace_base=str(tmp_path), event_bus=mocker.MagicMock(), github_token="t"
    )
    mocker.patch.object(mgr, "_clone_repo")
    sid = mgr.create_session(repo_url="https://x/y")
    assert sid in mgr._sessions
    mgr._clone_repo.assert_called_once()
