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


def test_approve_commits_and_pushes(mocker, tmp_path):
    from orchestrator.core.context_sync import ContextSync

    cs = ContextSync(str(tmp_path), "t", "docs/MEMORY.md")
    ws = tmp_path / "ws"
    ws.mkdir()
    cs._drafts["d1"] = {"workspace": str(ws), "repo_url": "https://x/y", "diff": "+x"}
    run = mocker.patch("subprocess.run")
    cs.approve("d1")
    cmds = [c.args[0] for c in run.call_args_list]
    assert any("commit" in c for c in cmds)
    assert any("push" in c for c in cmds)
    assert "d1" not in cs._drafts
