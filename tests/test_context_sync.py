import subprocess
from pathlib import Path

import pytest

from orchestrator.core.context_sync import ContextSync


async def test_draft_runs_revise_and_captures_diff(mocker, tmp_path):
    cs = ContextSync(
        workspace_base=str(tmp_path),
        github_token="t",
        memory_md_path="docs/MEMORY.md",
    )
    mocker.patch.object(cs, "_clone_repo")
    mocker.patch.object(cs, "_run_revise", new=mocker.AsyncMock())
    mocker.patch.object(cs, "_git_diff", return_value="+ new line")

    draft = await cs.draft(repo_url="https://x/y", summary="merged plan z")

    assert draft["diff"] == "+ new line"
    assert draft["draft_id"] in cs._drafts
    cs._run_revise.assert_awaited()


def test_clone_repo_delegates_to_clone_with_token(mocker, tmp_path):
    """_clone_repo must call clone_with_token with clean URL — no token embedded."""
    mock_cwt = mocker.patch("orchestrator.core.context_sync.clone_with_token")
    cs = ContextSync(
        str(tmp_path), github_token="secret-token", memory_md_path="MEMORY.md"
    )
    cs._clone_repo("https://github.com/user/repo", "/some/dest")

    mock_cwt.assert_called_once_with(
        "https://github.com/user/repo", "/some/dest", "secret-token", depth=20
    )
    # Token must NOT appear in the URL argument
    url_arg = mock_cwt.call_args[0][0]
    assert "secret-token" not in url_arg


def test_approve_commits_and_pushes(mocker, tmp_path):
    from orchestrator.core.context_sync import ContextSync

    cs = ContextSync(str(tmp_path), "t", "docs/MEMORY.md")
    ws = tmp_path / "ws"
    ws.mkdir()
    cs._drafts["d1"] = {"workspace": str(ws), "repo_url": "https://x/y", "diff": "+x"}

    mock_cap = mocker.patch("orchestrator.core.context_sync.commit_and_push")
    result = cs.approve("d1")

    mock_cap.assert_called_once_with(str(ws), "t", "docs: sync CLAUDE.md and MEMORY.md")
    assert result == {"status": "committed", "draft_id": "d1"}
    assert "d1" not in cs._drafts


def test_approve_cleans_up_workspace(mocker, tmp_path):
    cs = ContextSync(str(tmp_path), "tok", "MEMORY.md")
    ws = tmp_path / "ws2"
    ws.mkdir()
    cs._drafts["d2"] = {"workspace": str(ws), "repo_url": "https://x/y", "diff": ""}

    mocker.patch("orchestrator.core.context_sync.commit_and_push")
    mock_rmtree = mocker.patch("orchestrator.core.context_sync.shutil.rmtree")

    cs.approve("d2")
    mock_rmtree.assert_called_once_with(str(ws), ignore_errors=True)


def test_current_cleans_up_workspace(mocker, tmp_path):
    cs = ContextSync(str(tmp_path), "tok", "MEMORY.md")
    mocker.patch.object(cs, "_clone_repo")  # skip actual git
    mock_rmtree = mocker.patch("orchestrator.core.context_sync.shutil.rmtree")

    cs.current("https://x/y")
    mock_rmtree.assert_called_once()
    call_kwargs = mock_rmtree.call_args
    assert call_kwargs[1].get("ignore_errors") is True


def test_current_cleans_up_workspace_on_clone_failure(mocker, tmp_path):
    """Workspace directory must be removed even when _clone_repo raises."""
    cs = ContextSync(str(tmp_path), "tok", "MEMORY.md")
    error = subprocess.CalledProcessError(128, "git clone", stderr=b"not a git repo")
    mocker.patch.object(cs, "_clone_repo", side_effect=error)
    mock_rmtree = mocker.patch("orchestrator.core.context_sync.shutil.rmtree")

    with pytest.raises(subprocess.CalledProcessError):
        cs.current("https://x/y")

    mock_rmtree.assert_called_once()
    assert mock_rmtree.call_args[1].get("ignore_errors") is True


def test_current_returns_files_read_before_cleanup(mocker, tmp_path):
    """current() must return file contents even though rmtree is called."""
    cs = ContextSync(str(tmp_path), "tok", "MEMORY.md")

    def _fake_clone(repo_url: str, dest: str) -> None:
        # dest is the workspace dir created by current(); write there
        (Path(dest) / "CLAUDE.md").write_text("hello", encoding="utf-8")

    mocker.patch.object(cs, "_clone_repo", side_effect=_fake_clone)

    result = cs.current("https://x/y")
    assert result["claude_md"] == "hello"
    assert result["memory_md"] == ""
