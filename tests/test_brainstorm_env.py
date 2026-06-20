def test_brainstorm_workspace_setting_default():
    from orchestrator.config import Settings

    s = Settings(auth_token="x", github_token="y", _env_file=None)
    assert s.brainstorm_workspace == "/tmp/praxis-brainstorm"
