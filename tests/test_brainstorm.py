from pathlib import Path

from orchestrator.core.brainstorm import (
    PLAN_BOOTSTRAP,
    BrainstormManager,
    BrainstormSession,
    parse_stream_line,
)


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


async def test_manager_starts_session(mocker, tmp_path):
    from orchestrator.core.brainstorm import BrainstormManager

    mgr = BrainstormManager(
        workspace_base=str(tmp_path), event_bus=mocker.MagicMock(), credentials="t"
    )
    mocker.patch.object(mgr, "_clone_repo", new=mocker.AsyncMock())
    sid = await mgr.create_session(repo_url="https://x/y")
    assert sid in mgr._sessions
    mgr._clone_repo.assert_called_once()


async def test_clone_repo_delegates_to_clone_with_token(mocker, tmp_path):
    """_clone_repo must call clone_with_token with clean URL + token, not embed token in URL."""
    from orchestrator.core.brainstorm import BrainstormManager

    mock_cwt = mocker.patch("orchestrator.core.brainstorm.clone_with_token")
    mgr = BrainstormManager(
        workspace_base=str(tmp_path),
        event_bus=mocker.MagicMock(),
        credentials="secret-tok",
    )
    await mgr._clone_repo("https://github.com/user/repo", "/some/dest")
    mock_cwt.assert_called_once_with(
        "https://github.com/user/repo", "/some/dest", "secret-tok", depth=50
    )
    # Token must NOT appear in the URL argument
    url_arg = mock_cwt.call_args[0][0]
    assert "secret-tok" not in url_arg


async def test_write_and_commit_uses_commit_and_push(mocker, tmp_path):
    """write_and_commit must delegate push to commit_and_push, not raw git commands."""
    from orchestrator.core.brainstorm import BrainstormManager

    mock_cap = mocker.patch("orchestrator.core.brainstorm.commit_and_push")

    mgr = BrainstormManager(
        workspace_base=str(tmp_path), event_bus=mocker.MagicMock(), credentials="tok"
    )
    mocker.patch.object(mgr, "_clone_repo", new=mocker.AsyncMock())

    result = await mgr.write_and_commit(
        repo_url="https://github.com/user/repo",
        path="docs/spec.md",
        content="# spec",
    )
    assert result == {"status": "committed", "path": "docs/spec.md"}
    mock_cap.assert_called_once()
    call_kwargs = mock_cap.call_args
    # token arg (2nd positional) must be the token
    assert call_kwargs[0][1] == "tok"
    # paths kwarg must include our path
    assert (
        call_kwargs[1].get("paths") == ["docs/spec.md"]
        or "docs/spec.md" in call_kwargs[0]
    )


async def test_write_and_commit_rejects_path_escape(mocker, tmp_path):
    """write_and_commit must raise ValueError for paths that escape workspace."""
    import pytest

    from orchestrator.core.brainstorm import BrainstormManager

    mgr = BrainstormManager(
        workspace_base=str(tmp_path), event_bus=mocker.MagicMock(), credentials="tok"
    )
    mocker.patch.object(mgr, "_clone_repo", new=mocker.AsyncMock())
    mocker.patch("orchestrator.core.brainstorm.commit_and_push")

    with pytest.raises(ValueError, match="escapes workspace"):
        await mgr.write_and_commit(
            repo_url="https://github.com/user/repo",
            path="../../etc/passwd",
            content="bad",
        )


async def test_generate_plan_cleans_up_workspace(mocker, tmp_path):
    """generate_plan must remove the temp workspace after run_turn completes."""
    from orchestrator.core.brainstorm import BrainstormManager, BrainstormSession

    mgr = BrainstormManager(
        workspace_base=str(tmp_path), event_bus=mocker.MagicMock(), credentials="tok"
    )
    mocker.patch.object(mgr, "_clone_repo", new=mocker.AsyncMock())

    async def fake_run_turn(self, message, *, resume):
        pass

    mocker.patch.object(BrainstormSession, "run_turn", fake_run_turn)

    await mgr.generate_plan("https://github.com/u/r", "docs/spec.md", "none")
    # workspace directories should all be cleaned up
    remaining = list(tmp_path.iterdir())
    assert remaining == [], f"Expected cleanup but found: {remaining}"


def _seed_repo(workspace: str) -> None:
    root = Path(workspace)
    (root / "docs" / "superpowers" / "specs").mkdir(parents=True)
    (root / "docs" / "superpowers" / "plans").mkdir(parents=True)
    (root / "docs" / "superpowers" / "specs" / "x-design.md").write_text(
        "# X Design\n\nwhat to build", encoding="utf-8"
    )
    (root / "docs" / "superpowers" / "plans" / "x.md").write_text(
        "---\nspec_path: docs/superpowers/specs/x-design.md\n---\n"
        "# X Plan\n- [x] a\n- [ ] b\n",
        encoding="utf-8",
    )


async def test_list_lifecycle_docs(tmp_path, mocker):
    mgr = BrainstormManager(
        workspace_base=str(tmp_path / "ws"), event_bus=None, credentials="t"
    )
    mocker.patch.object(
        mgr,
        "_clone_repo",
        new=mocker.AsyncMock(side_effect=lambda _url, dest: _seed_repo(dest)),
    )
    docs = await mgr.list_lifecycle_docs("https://example.com/repo.git")
    specs = [d for d in docs if d["category"] == "spec"]
    plans = [d for d in docs if d["category"] == "plan"]
    assert specs[0]["path"] == "docs/superpowers/specs/x-design.md"
    assert plans[0]["spec_path"] == "docs/superpowers/specs/x-design.md"
    assert (plans[0]["done_count"], plans[0]["total_count"]) == (1, 2)


async def test_read_doc(tmp_path, mocker):
    mgr = BrainstormManager(
        workspace_base=str(tmp_path / "ws"), event_bus=None, credentials="t"
    )
    mocker.patch.object(
        mgr,
        "_clone_repo",
        new=mocker.AsyncMock(side_effect=lambda _url, dest: _seed_repo(dest)),
    )
    content = await mgr.read_doc(
        "https://example.com/repo.git", "docs/superpowers/plans/x.md"
    )
    assert "# X Plan" in content


def test_plan_bootstrap_requests_spec_path_frontmatter():
    prompt = PLAN_BOOTSTRAP.format(spec_path="docs/specs/x.md", notes="none")
    assert "spec_path" in prompt
    assert "front-matter" in prompt.lower() or "frontmatter" in prompt.lower()


async def test_brainstorm_resolves_token_from_provider(monkeypatch):
    import orchestrator.core.brainstorm as bs
    from orchestrator.core.github_credentials import PatCredentialProvider

    seen = {}

    def fake_clone(repo_url, dest, token, depth=50):
        seen["clone_token"] = token

    monkeypatch.setattr(bs, "clone_with_token", fake_clone)

    mgr = bs.BrainstormManager(
        workspace_base="/tmp/x",
        event_bus=None,
        credentials=PatCredentialProvider("ghs_scoped"),
    )
    await mgr._clone_repo("https://github.com/o/r", "read-1")
    assert seen["clone_token"] == "ghs_scoped"


async def test_stream_lines_logs_nonzero_exit(mocker, tmp_path):
    """_stream_lines must log a warning when the subprocess exits non-zero."""
    from orchestrator.core.brainstorm import BrainstormSession

    s = BrainstormSession(session_id="abc", workspace=str(tmp_path))

    async def empty_aiter():
        return
        yield b""  # make it an async generator

    mock_proc = mocker.MagicMock()
    mock_proc.stdout = empty_aiter()
    mock_proc.wait = mocker.AsyncMock()
    mock_proc.returncode = 1
    mock_proc.stderr = mocker.MagicMock()
    mock_proc.stderr.read = mocker.AsyncMock(return_value=b"some error")

    mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
    mock_logger = mocker.patch("orchestrator.core.brainstorm.logger")

    async for _ in s._stream_lines(["claude", "-p", "hi"]):
        pass

    mock_logger.warning.assert_called_once()
    warning_msg = str(mock_logger.warning.call_args)
    assert "1" in warning_msg or "some error" in warning_msg
