import pytest

from orchestrator.core.llm_router import (
    CALL_SITE_DEFAULTS,
    LOGIN_HINTS,
    LLMRouter,
    ProviderAuthError,
    ProviderOutputError,
    UnknownProviderError,
    build_argv,
)


@pytest.fixture(autouse=True)
def _stub_which(mocker):
    # The router resolves argv[0] via shutil.which (Windows .CMD/.EXE shims);
    # stub it so subprocess-mocked tests don't depend on the real CLIs being
    # installed in the test environment.
    mocker.patch(
        "orchestrator.core.llm_router.shutil.which", side_effect=lambda name: name
    )


def _mock_proc(mocker, *, stdout=b"OUT", stderr=b"", returncode=0):
    proc = mocker.AsyncMock()
    proc.communicate = mocker.AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


def test_defaults_cover_all_call_sites():
    expected = {
        "plan_spec",
        "review_diff_first",
        "review_diff_rereview",
        "analyze_improvements",
        "classify_doc",
        "brainstorm_run_turn",
        "brainstorm_generate_plan",
        "context_sync",
        "derive_tasks",
    }
    assert expected <= set(CALL_SITE_DEFAULTS)


def test_build_argv_claude():
    argv = build_argv("claude", model="claude-opus-4-8", effort="high", prompt="hi")
    assert argv[:2] == ["claude", "-p"]
    assert "--model" in argv and "claude-opus-4-8" in argv  # noqa: PT018


def test_build_argv_unknown_provider():
    with pytest.raises(UnknownProviderError):
        build_argv("frobnicator", model="x", effort=None, prompt="hi")


async def test_run_claude_provider(mocker):
    resolver = mocker.AsyncMock(
        return_value={
            "provider": "claude",
            "model": "claude-opus-4-8",
            "effort": "high",
        }
    )
    proc = mocker.AsyncMock()
    proc.communicate = mocker.AsyncMock(return_value=(b"OUT", b""))
    proc.returncode = 0
    mocker.patch(
        "asyncio.create_subprocess_exec", new=mocker.AsyncMock(return_value=proc)
    )
    router = LLMRouter(resolve=resolver)
    out = await router.run("plan_spec", "prompt", project_id=None)
    assert out == "OUT"
    resolver.assert_awaited_once()


def test_build_argv_agy_embeds_prompt():
    # agy --print takes the prompt as a positional argv value, not stdin.
    argv = build_argv("agy", model="gemini-2.5-pro", effort=None, prompt="hello")
    assert argv[:2] == ["agy", "--print"]
    assert argv[2] == "hello"
    assert "--model" in argv and "gemini-2.5-pro" in argv  # noqa: PT018


def test_build_argv_codex_no_prompt_in_argv():
    # codex exec reads the prompt from stdin; argv must not contain it.
    argv = build_argv("codex", model="", effort=None, prompt="secret-prompt")
    assert argv == ["codex", "exec"]


async def test_run_agy_passes_prompt_as_argv_not_stdin(mocker):
    resolver = mocker.AsyncMock(
        return_value={"provider": "agy", "model": "gemini", "effort": None}
    )
    proc = _mock_proc(mocker, stdout=b"PONG")
    create = mocker.patch(
        "asyncio.create_subprocess_exec", new=mocker.AsyncMock(return_value=proc)
    )
    router = LLMRouter(resolve=resolver)
    out = await router.run("plan_spec", "the-prompt", project_id=None)
    assert out == "PONG"
    # prompt is in argv, and stdin gets None (not the prompt bytes)
    assert "the-prompt" in create.call_args.args
    proc.communicate.assert_awaited_once_with(input=None)


async def test_run_codex_auth_failure_raises_provider_auth_error(mocker):
    # codex exits 0 while printing a 401 to stderr — must NOT pass through.
    resolver = mocker.AsyncMock(
        return_value={"provider": "codex", "model": "", "effort": None}
    )
    proc = _mock_proc(
        mocker,
        stdout=b"",
        stderr=b"ERROR: refresh token was revoked. Please log out and sign in again.",
        returncode=0,
    )
    mocker.patch(
        "asyncio.create_subprocess_exec", new=mocker.AsyncMock(return_value=proc)
    )
    router = LLMRouter(resolve=resolver)
    with pytest.raises(ProviderAuthError) as exc:
        await router.run("plan_spec", "p", project_id=None)
    assert exc.value.provider == "codex"
    assert exc.value.login_hint == LOGIN_HINTS["codex"]


async def test_run_empty_output_raises_output_error(mocker):
    resolver = mocker.AsyncMock(
        return_value={"provider": "agy", "model": "", "effort": None}
    )
    proc = _mock_proc(mocker, stdout=b"   ", stderr=b"", returncode=0)
    mocker.patch(
        "asyncio.create_subprocess_exec", new=mocker.AsyncMock(return_value=proc)
    )
    router = LLMRouter(resolve=resolver)
    with pytest.raises(ProviderOutputError):
        await router.run("plan_spec", "p", project_id=None)


async def test_run_agy_rejects_oversized_prompt(mocker):
    resolver = mocker.AsyncMock(
        return_value={"provider": "agy", "model": "", "effort": None}
    )
    router = LLMRouter(resolve=resolver)
    with pytest.raises(ProviderOutputError):
        await router.run("plan_spec", "x" * 40000, project_id=None)


async def test_run_missing_binary_raises_provider_auth_error(mocker):
    resolver = mocker.AsyncMock(
        return_value={"provider": "codex", "model": "", "effort": None}
    )
    mocker.patch("orchestrator.core.llm_router.shutil.which", return_value=None)
    router = LLMRouter(resolve=resolver)
    with pytest.raises(ProviderAuthError):
        await router.run("plan_spec", "p", project_id=None)


async def test_run_local_provider(mocker):
    resolver = mocker.AsyncMock(
        return_value={"provider": "local", "model": "", "effort": None}
    )
    mocker.patch(
        "orchestrator.core.llm_router.LLMRouter._run_local",
        new=mocker.AsyncMock(return_value="LOCAL"),
    )
    router = LLMRouter(resolve=resolver, lm_studio_url="http://lm:1234")
    out = await router.run("derive_tasks", "p", project_id=None)
    assert out == "LOCAL"


@pytest.mark.unit
async def test_run_passes_cwd_to_subprocess(mocker):
    resolver = mocker.AsyncMock(
        return_value={
            "provider": "claude",
            "model": "claude-sonnet-4-6",
            "effort": None,
        }
    )
    seen: dict[str, object] = {}

    async def fake_exec(*argv, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        proc = mocker.AsyncMock()
        proc.communicate = mocker.AsyncMock(return_value=(b'{"verdict":"pass"}', b""))
        proc.returncode = 0
        return proc

    mocker.patch("asyncio.create_subprocess_exec", side_effect=fake_exec)
    router = LLMRouter(resolve=resolver)
    await router.run(
        "review_diff_first", "prompt", project_id=None, cwd="/tmp/checkout"
    )
    assert seen["cwd"] == "/tmp/checkout"


@pytest.mark.unit
def test_plan_review_call_site_registered():
    cfg = CALL_SITE_DEFAULTS["plan_review"]
    assert cfg["provider"] == "claude"  # capability judgment needs the brain


@pytest.mark.unit
async def test_llm_router_run_local_makes_http_request(mocker) -> None:
    # Setup mock for httpx.AsyncClient
    mock_post = mocker.patch("httpx.AsyncClient.post", new_callable=mocker.AsyncMock)

    mock_response = mocker.MagicMock()
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "resolved local response"
                }
            }
        ]
    }
    mock_post.return_value = mock_response

    resolver = mocker.AsyncMock(
        return_value={"provider": "local", "model": "my-local-model", "effort": None}
    )
    router = LLMRouter(resolve=resolver, lm_studio_url="http://localhost:1234")
    out = await router.run("derive_tasks", "hello local", project_id=None)

    assert out == "resolved local response"
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "http://localhost:1234/v1/chat/completions"
    assert kwargs["json"]["model"] == "my-local-model"
    assert kwargs["json"]["temperature"] == 0
    assert kwargs["json"]["messages"] == [{"role": "user", "content": "hello local"}]


@pytest.mark.unit
async def test_llm_router_unknown_provider_raises_error(mocker) -> None:
    resolver = mocker.AsyncMock(
        return_value={"provider": "unsupported-provider", "model": "", "effort": None}
    )
    router = LLMRouter(resolve=resolver)
    with pytest.raises(UnknownProviderError, match="Unknown or non-CLI provider"):
        await router.run("plan_spec", "hello", project_id=None)


@pytest.mark.integration
async def test_llm_router_routing_decision_overridden_by_db(db, test_settings, mocker) -> None:
    # 1. Override plan_spec routing decision in the database
    import json
    override_config = {
        "provider": "local",
        "model": "db-overridden-model",
        "effort": None,
    }
    await db.execute(
        "INSERT INTO settings_overrides (key, value) VALUES (?, ?)",
        ("models.plan_spec", json.dumps(override_config)),
    )

    # 2. Setup EffectiveSettings with the database
    from orchestrator.core.effective_settings import EffectiveSettings
    effective_settings = EffectiveSettings(test_settings, db)

    # 3. Mock the httpx request so we don't hit actual local network
    mock_post = mocker.patch("httpx.AsyncClient.post", new_callable=mocker.AsyncMock)
    mock_response = mocker.MagicMock()
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "db override response"
                }
            }
        ]
    }
    mock_post.return_value = mock_response

    # 4. Instantiate LLMRouter using EffectiveSettings' resolver
    router = LLMRouter(
        resolve=effective_settings.call_site_config,
        lm_studio_url="http://localhost:1234",
    )

    # 5. Run the router and verify it uses the DB override
    out = await router.run("plan_spec", "test plan spec prompt", project_id=None)
    assert out == "db override response"

    # Verify that the DB override's model was used
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["model"] == "db-overridden-model"

