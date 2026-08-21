"""`praxis plans` must hand back a plan id you can actually copy.

Found live in newcomer walkthrough #9, and it is the same defect that cost
run #8 its point, surviving in the one surface run #8 did not touch.

Two failures compounded. The ID column carried `max_width=36, overflow="fold"`,
and `max_width` is a MAXIMUM: with four columns competing for an 80-column
console rich shrank the column and folded each uuid across two rows. And the
copyable line below the table was printed ONLY for a plan with an open
integration PR, so a pending, active, or already-integrated plan got no
copyable id at all. A conditional copyable line reads as a working one right
up until you need the id it withheld, which is why `tasks` could carry a
comment claiming `plans` "already does" this while it did not.

Every test here runs at COLUMNS=80 and asserts the command appears whole on a
SINGLE line. Asserting on flattened or joined output is what let the original
`tasks` fold survive five runs: the damage IS the line break, so a test that
removes line breaks cannot see it.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from cli import main as cli_main
from cli.main import app


runner = CliRunner()

PLAN_ID = "c9604dd1-88ad-4f77-a387-f3a7db076b0b"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for name in ("AUTH_TOKEN", "ORCHESTRATOR_TOKEN", "ORCHESTRATOR_URL", "COLUMNS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.chdir(tmp_path)
    cli_main._env_file_values.cache_clear()
    yield
    cli_main._env_file_values.cache_clear()


def _patch_client(monkeypatch: pytest.MonkeyPatch, payload: list[dict]) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        "cli.main._client",
        lambda _timeout=None: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


def _plan(**over) -> dict:
    base = {
        "id": PLAN_ID,
        "spec_path": "docs/superpowers/specs/2026-08-21-add-slugify-helper.md",
        "source": "user",
        "status": "active",
        "integration_pr_url": None,
        "integration_merged_at": None,
    }
    base.update(over)
    return base


def _contiguous(output: str, needle: str) -> bool:
    """True when `needle` appears whole on one physical line of `output`."""
    return any(needle in line for line in output.splitlines())


#: Box-drawing glyphs typer/rich frames its help panels with.
_BOX = "│┃┌┐└┘├┤┬┴┼─━╭╮╰╯"


def _flat_help(output: str) -> str:
    """Collapse rendered help into one line, borders removed.

    Stripping the box glyphs BEFORE collapsing whitespace is load-bearing, and
    getting it wrong made the first version of this guard inert. Rich wraps a
    long option help string across panel rows and puts a border on each row,
    so the rendered text of "use the registry default" is literally
    "use the registry | | default". A plain `" ".join(output.split())` leaves
    those borders in place, the phrase never matches, and the assertion passes
    whether the help is right or wrong.

    This is the safe direction of the strip-vs-keep rule: removing the borders
    can only make a bad string easier to FIND. It cannot let prose satisfy a
    check that should have failed.
    """
    stripped = "".join(" " if ch in _BOX else ch for ch in output)
    return " ".join(stripped.split())


@pytest.mark.parametrize(
    ("status", "over"),
    [
        ("pending", {"status": "pending"}),
        ("active", {"status": "active"}),
        (
            "integrated",
            {
                "status": "completed",
                "integration_pr_url": "https://github.com/o/r/pull/63",
                "integration_merged_at": "2026-08-21T14:26:00Z",
            },
        ),
    ],
)
def test_every_plan_gets_a_copyable_id_whatever_its_status(
    monkeypatch: pytest.MonkeyPatch, status: str, over: dict
) -> None:
    """The three statuses that used to print no copyable line at all.

    Parametrized rather than merged into one call deliberately: the bug was a
    CONDITION on the line, so each branch of that condition needs its own
    scenario, or one passing status masks the others.
    """
    _patch_client(monkeypatch, [_plan(**over)])

    result = runner.invoke(app, ["plans", "proj-1"])

    assert result.exit_code == 0
    assert _contiguous(result.output, f"praxis tasks {PLAN_ID}"), (
        f"no contiguous copyable id for a {status} plan:\n{result.output}"
    )


def test_the_uuid_never_folds_across_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """The id must survive as one token at 80 columns.

    Pinned separately from the line above because a future table change could
    reintroduce an ID column and fold it while the copyable line still passes.
    """
    _patch_client(monkeypatch, [_plan()])

    result = runner.invoke(app, ["plans", "proj-1"])

    assert result.exit_code == 0
    assert _contiguous(result.output, PLAN_ID), (
        f"the plan uuid folded across lines:\n{result.output}"
    )
    for line in result.output.splitlines():
        assert PLAN_ID[:12] not in line or PLAN_ID in line, (
            f"a partial uuid appears on its own line:\n{result.output}"
        )


def test_a_plan_awaiting_integration_still_offers_merge_plan_and_its_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The behaviour that already worked must not be traded away for the fix."""
    _patch_client(
        monkeypatch,
        [
            _plan(
                status="completed",
                integration_pr_url="https://github.com/o/r/pull/63",
            )
        ],
    )

    result = runner.invoke(app, ["plans", "proj-1"])

    assert result.exit_code == 0
    assert _contiguous(result.output, f"praxis merge-plan {PLAN_ID}"), result.output
    assert _contiguous(result.output, "https://github.com/o/r/pull/63"), result.output


def test_add_project_harness_help_does_not_claim_the_registry_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--harness` omitted takes the CONFIGURED worker, not the registry default.

    `POST /api/projects` resolves `body.harness or settings.default_worker_harness`,
    so with the shipped `gemini-agy` preset an omitted flag yields `agy` while
    `default_harness_id()` is `opencode`. The help said "registry default",
    which named the wrong one of the two. Measured live in walkthrough #9:
    `praxis add-project` with no `--harness` produced a project with
    `harness = agy`.
    """
    result = runner.invoke(app, ["add-project", "--help"])

    assert result.exit_code == 0
    flat = _flat_help(result.output)
    assert "registry default" not in flat, (
        "add-project --harness help still claims the registry default, which is "
        f"opencode, while an omitted flag actually yields the configured worker:\n{flat}"
    )
    assert "preset" in flat, (
        f"the help no longer says where an omitted harness comes from:\n{flat}"
    )
