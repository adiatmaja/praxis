"""Seam test: the text `praxis submit` sends must reach the planner prompt.

This is deliberately NOT a unit test at either end.  `praxis submit` and the
planner prompt were each independently correct and independently tested while
the carrier between them was dead: the endpoint validated ``spec`` and then
never referenced it again, so the brain planned from the repository name alone.
A test anchored at either end passes on that broken product.

This one starts at the real Typer ``submit`` command, goes over a real
``httpx.Client`` into the real ASGI app, and ends at the prompt string handed
to the LLM router.  Every hop in between is production code.

If you touch the carrier (the spec doc write, ``plans.spec_path``, or the spec
read in ``plan_and_activate``), this test is the one that must go red.
"""
# ruff: noqa: S101

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from typer.testing import CliRunner

from cli import main as cli_main
from orchestrator.core.opus_bridge import OpusBridge
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.database import Database
from tests.conftest import seed_user


# A sentence that exists nowhere else in the product, so finding it in the
# planner prompt can only mean it travelled the whole way from the CLI.
SPEC_TEXT = (
    "Create src/leap.py exposing is_leap_year(year: int) -> bool so the four "
    "frozen tests in tests/test_leap.py pass. Add no other files."
)

PLAN_JSON = json.dumps(
    {
        "plan_summary": "Add a leap year helper",
        "plan_slug": "leap-year-helper",
        "tasks": [
            {
                "title": "Add is_leap_year",
                "slug": "add-is-leap-year",
                "description": "Write src/leap.py",
                "depends_on": [],
            }
        ],
    }
)


class _LoopBridgeTransport(httpx.BaseTransport):
    """A *sync* httpx transport that dispatches into an ASGI app on ``loop``.

    The CLI uses a blocking ``httpx.Client``; the app under test runs on the
    test's event loop.  Bridging the two lets the real CLI command drive the
    real app without binding a socket, so the test starts where a user starts.
    """

    def __init__(self, app: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._asgi = httpx.ASGITransport(app=app)
        self._loop = loop

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        outgoing = httpx.Request(
            request.method,
            request.url,
            headers=request.headers,
            content=request.read(),
        )

        async def _send() -> httpx.Response:
            response = await self._asgi.handle_async_request(outgoing)
            await response.aread()
            return response

        response = asyncio.run_coroutine_threadsafe(_send(), self._loop).result(
            timeout=30
        )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={
                "content-type": response.headers.get("content-type", "application/json")
            },
        )


class _RecordingRouter:
    """Stands in for the LLM router and keeps every prompt it is handed."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run(
        self,
        call_site: str,
        prompt: str,
        project_id: str | None = None,
        cwd: str | None = None,
    ) -> str:
        self.prompts.append(prompt)
        return PLAN_JSON


@pytest.mark.integration
async def test_submitted_spec_text_reaches_the_planner_prompt(
    client: AsyncClient,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_user(db)
    app = client.app  # type: ignore[attr-defined]
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name)
           VALUES (?, ?, ?, ?, ?)""",
        ("p1", "test-user", "playground", "https://github.com/u/playground", "qwen"),
    )

    # An in-memory stand-in for the repository the spec doc is committed to.
    # Writes land here; reads are served from here, so the doc must genuinely
    # round-trip through the path recorded on the plan row.
    repo_docs: dict[str, str] = {}

    async def _write_and_commit(repo_url: str, path: str, content: str) -> dict:
        repo_docs[path] = content
        return {"status": "committed", "path": path}

    async def _read_doc(repo_url: str, path: str) -> str:
        if path not in repo_docs:
            msg = f"doc not found: {path}"
            raise FileNotFoundError(msg)
        return repo_docs[path]

    monkeypatch.setattr(app.state.brainstorm, "write_and_commit", _write_and_commit)
    monkeypatch.setattr(app.state.brainstorm, "read_doc", _read_doc)

    router = _RecordingRouter()
    app.state.opus_bridge = OpusBridge(db, router=router)
    orchestrator = Orchestrator(
        task_queue=app.state.task_queue,
        agent_manager=None,
        opus_bridge=app.state.opus_bridge,
        git_ops=None,
        event_bus=app.state.event_bus,
        spec_reader=app.state.brainstorm,
    )
    app.state.orchestrator = orchestrator

    loop = asyncio.get_running_loop()

    def _cli_client() -> httpx.Client:
        return httpx.Client(
            base_url=cli_main._api_url(),
            headers={"Authorization": f"Bearer {cli_main._auth_token()}"},
            transport=_LoopBridgeTransport(app, loop),
            timeout=30.0,
        )

    monkeypatch.setattr(cli_main, "_client", _cli_client)

    # The real `praxis submit`, invoked the way a user invokes it.
    result = await asyncio.to_thread(
        CliRunner().invoke, cli_main.app, ["submit", "p1", SPEC_TEXT]
    )
    assert result.exit_code == 0, result.output

    plan = await db.fetch_one("SELECT * FROM plans WHERE project_id = ?", ("p1",))
    assert plan is not None, "submit created no plan"
    assert plan["spec_path"], "the plan carries no spec_path, so the spec is lost"
    assert SPEC_TEXT in repo_docs.get(plan["spec_path"], ""), (
        "the submitted spec was not persisted to the doc the plan points at"
    )

    await orchestrator.run_once()
    await orchestrator.drain_background()

    assert router.prompts, "the planner was never called"
    assert SPEC_TEXT in router.prompts[0], (
        "the submitted spec never reached the planner prompt; the brain planned "
        f"from this instead:\n{router.prompts[0]}"
    )
