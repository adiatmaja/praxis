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
