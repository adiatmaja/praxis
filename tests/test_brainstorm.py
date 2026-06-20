from orchestrator.core.brainstorm import BrainstormSession


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
