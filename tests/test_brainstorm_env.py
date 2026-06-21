import tempfile
from pathlib import Path


def test_brainstorm_workspace_setting_default():
    from orchestrator.config import Settings

    s = Settings(auth_token="x", github_token="y", _env_file=None)
    expected = str(Path(tempfile.gettempdir()) / "praxis-brainstorm")
    assert s.brainstorm_workspace == expected


def test_brainstorm_workspace_is_overridable(monkeypatch):
    """The brainstorm_workspace field can be overridden via env var."""
    monkeypatch.setenv("BRAINSTORM_WORKSPACE", "/custom/path")
    from importlib import reload

    import orchestrator.config as cfg_module

    reload(cfg_module)
    s = cfg_module.Settings(auth_token="x", github_token="y", _env_file=None)
    assert s.brainstorm_workspace == "/custom/path"
