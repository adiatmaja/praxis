"""One ``repo_url`` policy, shared by every request schema.

Before this module the three sibling request schemas carried three different
policies, all of them verified by execution:

- ``ProjectCreate`` allowed ``https://`` and scp-style ``git@host:path`` only.
- ``DispatchRequest`` additionally allowed ``http://`` and ``ssh://``, and
  checked the git option-injection fragments only as a PREFIX, so
  ``https://github.com/u/r --upload-pack=/bin/sh`` was accepted.
- ``ExecutePlanRequest`` had NO validator at all and accepted
  ``ext::sh -c whoami``, ``git://evil/repo.git`` and the ``--upload-pack`` form.

None of that was a live RCE (git 2.52 refuses ``ext::`` itself, and the URL is
passed as a single argv element so an embedded option is never re-split), but
it is defense in depth that should not disagree with itself.

There is one deliberate behavior change beyond unification: a LOCAL filesystem
path is now a syntactically valid ``repo_url``, because the local git backend
exists and is unreachable otherwise.  Admitting it is gated by
``Settings.allow_local_repo_paths``, which defaults to OFF and is enforced at
the endpoints, where runtime settings are reachable.  With the default the
HTTP-visible behavior is unchanged: a local path still gets a 422.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from orchestrator.config import Settings
from orchestrator.core.preflight import (
    PreflightError,
    PreflightKind,
    assert_repo_url_allowed,
    status_and_detail,
)
from orchestrator.core.repo_url_policy import (
    RepoUrlKind,
    classify_repo_url,
    validate_repo_url,
)
from orchestrator.models.schemas import (
    DispatchRequest,
    ExecutePlanRequest,
    ProjectCreate,
)


# Minimal valid payloads for each schema, keyed by name so a parametrize id
# names the schema that regressed.
def _project_create(repo_url: str) -> Any:
    return ProjectCreate(name="p", repo_url=repo_url, model_name="m")


def _dispatch(repo_url: str) -> Any:
    return DispatchRequest(repo_url=repo_url, instructions="do it", model="m")


def _execute_plan(repo_url: str) -> Any:
    return ExecutePlanRequest(repo_url=repo_url, plan="# plan", model="m")


SCHEMAS = (
    pytest.param(_project_create, id="ProjectCreate"),
    pytest.param(_dispatch, id="DispatchRequest"),
    pytest.param(_execute_plan, id="ExecutePlanRequest"),
)


# --------------------------------------------------------------------------
# The pure policy function
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_https_url_is_remote() -> None:
    value, kind = classify_repo_url("  https://github.com/u/r.git  ")
    assert value == "https://github.com/u/r.git"
    assert kind is RepoUrlKind.REMOTE


@pytest.mark.unit
def test_scp_style_ssh_is_remote() -> None:
    value, kind = classify_repo_url("git@github.com:u/r.git")
    assert value == "git@github.com:u/r.git"
    assert kind is RepoUrlKind.REMOTE


@pytest.mark.unit
@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param("file:///srv/praxis/r.git", id="file-url"),
        pytest.param("/srv/praxis/r.git", id="posix-absolute"),
        pytest.param(r"C:\repos\r.git", id="windows-drive"),
        pytest.param(r"\\fileserver\share\r.git", id="unc-share"),
        pytest.param("~/repos/r.git", id="tilde"),
    ],
)
def test_local_filesystem_forms_classify_as_local(candidate: str) -> None:
    """Every form ``git_backend.is_local_repo_url`` accepts is LOCAL, not refused.

    These used to raise at the schema, which is what made bench Phase A's local
    git backend unreachable through the REST surface.
    """
    value, kind = classify_repo_url(candidate)
    assert value == candidate
    assert kind is RepoUrlKind.LOCAL


@pytest.mark.unit
@pytest.mark.parametrize(
    ("candidate", "expected_fragment"),
    [
        pytest.param("ext::sh -c whoami", "ext", id="ext-transport"),
        pytest.param("EXT::sh -c whoami", "ext", id="ext-transport-uppercase"),
        pytest.param("git://evil.example/repo.git", "git://", id="git-protocol"),
        pytest.param("ssh://git@github.com/u/r.git", "ssh://", id="ssh-scheme"),
        pytest.param("http://github.com/u/r.git", "http://", id="plaintext-http"),
    ],
)
def test_each_blocked_transport_is_refused_by_name(
    candidate: str, expected_fragment: str
) -> None:
    """One test per refused transport, so a regression names what came back."""
    with pytest.raises(ValueError, match=expected_fragment):
        classify_repo_url(candidate)


@pytest.mark.unit
@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param("ftp://host/r.git", id="ftp"),
        pytest.param("not-a-url", id="bare-word"),
        pytest.param("https://", id="https-no-host"),
        pytest.param("https://hostonly", id="https-no-path"),
        pytest.param("git@hostwithoutcolon", id="scp-without-colon"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
    ],
)
def test_unrecognized_forms_are_refused(candidate: str) -> None:
    with pytest.raises(ValueError, match="repo_url"):
        classify_repo_url(candidate)


@pytest.mark.unit
@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param(
            "https://github.com/u/r --upload-pack=/bin/sh", id="upload-pack-embedded"
        ),
        pytest.param("--upload-pack=/bin/sh", id="upload-pack-leading"),
        pytest.param("https://github.com/u/r --config=core.pager=sh", id="config"),
        pytest.param("/srv/r.git --upload-pack=/bin/sh", id="local-path-with-option"),
    ],
)
def test_option_injection_fragments_are_refused_anywhere_in_the_value(
    candidate: str,
) -> None:
    """The fragment check is a containment check, never a prefix check.

    ``DispatchRequest`` used a prefix-only scheme check, so the embedded form
    got through.  The local case matters too: the fragment check must run
    BEFORE the local-path branch, or an opted-in deployment reintroduces it.
    """
    with pytest.raises(ValueError, match="--"):
        classify_repo_url(candidate)


@pytest.mark.unit
@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param(
            "file:///srv/r.git%20%2D%2Dupload-pack=/bin/sh", id="percent-encoded-option"
        ),
        pytest.param("file://%2D%2Dupload-pack=/bin/sh", id="percent-encoded-leading"),
        pytest.param("file://%2Dc/core.pager=sh", id="percent-encoded-dash-c"),
    ],
)
def test_a_percent_encoded_option_cannot_hide_inside_a_file_url(candidate: str) -> None:
    """The validated string and the CONSUMED string must be the same thing.

    ``git_backend.local_repo_path`` percent-decodes a ``file://`` URL before
    handing it to git, so checking only the raw candidate leaves a gap:
    ``file://%2D%2Dupload-pack=/bin/sh`` decodes to ``--upload-pack=/bin/sh``.
    That is one argv element, but it is an argv element BEGINNING WITH A DASH,
    and git consumes it as an option rather than as the repository. Measured:
    ``git clone --no-single-branch --upload-pack=/bin/sh dest`` reports
    ``repository 'dest' does not exist``, i.e. the path was eaten as a flag.
    The "single argv element" argument that holds for the remote forms does
    not hold here.
    """
    with pytest.raises(ValueError, match="repo_url"):
        classify_repo_url(candidate)


@pytest.mark.unit
def test_a_bare_leading_dash_never_becomes_a_local_path() -> None:
    """``-rf`` is not a local form at all and must not become one."""
    with pytest.raises(ValueError, match="repo_url"):
        classify_repo_url("-rf")


@pytest.mark.unit
def test_a_dash_inside_a_local_path_is_fine() -> None:
    """Only the DECODED path's first character matters to git's argv parsing.

    Refusing every path with a dash in it anywhere would reject ordinary
    directory names, and it is not what makes the difference.
    """
    value, kind = classify_repo_url("/srv/praxis/-weird/r.git")
    assert kind is RepoUrlKind.LOCAL
    assert value == "/srv/praxis/-weird/r.git"


@pytest.mark.unit
def test_an_ordinary_file_url_still_decodes_and_passes() -> None:
    """The decoded check must not refuse the legitimate case it guards."""
    value, kind = classify_repo_url("file:///srv/praxis/my%20repo.git")
    assert kind is RepoUrlKind.LOCAL
    assert value == "file:///srv/praxis/my%20repo.git"


@pytest.mark.unit
def test_validate_repo_url_returns_the_stripped_value() -> None:
    assert validate_repo_url("  https://github.com/u/r  ") == "https://github.com/u/r"


# --------------------------------------------------------------------------
# All three schemas share it
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("build", SCHEMAS)
@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param("ext::sh -c whoami", id="ext-transport"),
        pytest.param("git://evil.example/repo.git", id="git-protocol"),
        pytest.param("ssh://git@github.com/u/r.git", id="ssh-scheme"),
        pytest.param("http://github.com/u/r.git", id="plaintext-http"),
        pytest.param("https://github.com/u/r --upload-pack=/bin/sh", id="upload-pack"),
        pytest.param("", id="empty"),
    ],
)
def test_every_schema_refuses_every_blocked_form(build: Any, candidate: str) -> None:
    with pytest.raises(ValidationError):
        build(candidate)


@pytest.mark.unit
@pytest.mark.parametrize("build", SCHEMAS)
def test_every_schema_accepts_the_same_remote_forms(build: Any) -> None:
    assert build("https://github.com/u/r").repo_url == "https://github.com/u/r"
    assert build("git@github.com:u/r.git").repo_url == "git@github.com:u/r.git"


@pytest.mark.unit
@pytest.mark.parametrize("build", SCHEMAS)
def test_every_schema_admits_a_local_path_for_the_settings_layer_to_judge(
    build: Any,
) -> None:
    """The schema cannot see runtime settings, so it defers rather than refuses."""
    assert build("/srv/praxis/r.git").repo_url == "/srv/praxis/r.git"


# --------------------------------------------------------------------------
# The settings gate
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_local_repo_paths_default_to_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The opt-in defaults to OFF, so nothing about the shipped posture moves."""
    monkeypatch.setenv("AUTH_TOKEN", "t")
    monkeypatch.delenv("ALLOW_LOCAL_REPO_PATHS", raising=False)
    missing_yaml = tmp_path / "absent.yaml"
    settings = Settings(_env_file=None, yaml_path=str(missing_yaml))
    assert settings.allow_local_repo_paths is False


@pytest.mark.unit
def test_the_shipped_yaml_also_ships_the_opt_in_off() -> None:
    """The FIELD default is not the effective default; the YAML overlays it.

    ``Settings.__init__`` overlays the settings YAML above field defaults, so
    a test that passes an absent ``yaml_path`` pins only half the invariant:
    flipping the committed YAML to ``true`` leaves it green while every real
    deployment ships opted in. Read the committed file itself.
    """
    import yaml

    from orchestrator.core.settings_file import config_file_path

    shipped = yaml.safe_load(Path(config_file_path()).read_text(encoding="utf-8"))
    assert shipped["allow_local_repo_paths"] is False


@pytest.mark.unit
def test_a_local_path_is_refused_when_the_setting_is_off() -> None:
    settings = type("S", (), {"allow_local_repo_paths": False})()
    with pytest.raises(PreflightError) as excinfo:
        assert_repo_url_allowed("/srv/praxis/r.git", settings)
    assert excinfo.value.kind is PreflightKind.LOCAL_DISABLED
    assert status_and_detail(excinfo.value)[0] == 422


@pytest.mark.unit
def test_a_local_path_is_allowed_when_the_setting_is_on() -> None:
    settings = type("S", (), {"allow_local_repo_paths": True})()
    assert_repo_url_allowed("/srv/praxis/r.git", settings)


@pytest.mark.unit
@pytest.mark.parametrize("enabled", [True, False])
def test_a_remote_url_is_never_touched_by_the_gate(enabled: bool) -> None:
    settings = type("S", (), {"allow_local_repo_paths": enabled})()
    assert_repo_url_allowed("https://github.com/u/r", settings)


# --------------------------------------------------------------------------
# The same defect, one field over
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "branch",
    [
        pytest.param("--upload-pack=/bin/sh", id="leading-option"),
        pytest.param("-rf", id="leading-dash"),
        pytest.param("../escape", id="traversal"),
        pytest.param("a//b", id="double-slash"),
        pytest.param("has space", id="whitespace"),
        pytest.param("x.lock", id="lock-suffix"),
    ],
)
def test_both_schemas_sanitize_branch_identically(branch: str) -> None:
    """``branch`` had the same three-copies problem, and reaches git's argv.

    ``DispatchRequest`` sanitized it and ``ExecutePlanRequest`` did not, so
    ``branch="--upload-pack=/bin/sh"`` was accepted there, became
    ``plans.plan_branch_name``, then ``BASE_BRANCH``, then
    ``PullRequestRef.base`` (which round-trips intact, since ``-`` is
    unreserved), then a git argument. No exploit was demonstrated end to end,
    because ``git checkout <base>`` dies on the unknown option first. It is
    fixed on the same principle as ``repo_url``: three disagreeing copies of
    one control means the weakest copy is the real one.
    """
    with pytest.raises(ValidationError):
        DispatchRequest(
            repo_url="https://github.com/u/r",
            instructions="x",
            model="m",
            branch=branch,
        )
    with pytest.raises(ValidationError):
        ExecutePlanRequest(
            repo_url="https://github.com/u/r", plan="p", model="m", branch=branch
        )


@pytest.mark.unit
def test_both_schemas_accept_an_ordinary_branch() -> None:
    ordinary = "plan/2026-08-07-thing"
    assert (
        DispatchRequest(
            repo_url="https://github.com/u/r",
            instructions="x",
            model="m",
            branch=ordinary,
        ).branch
        == ordinary
    )
    assert (
        ExecutePlanRequest(
            repo_url="https://github.com/u/r", plan="p", model="m", branch=ordinary
        ).branch
        == ordinary
    )


@pytest.mark.unit
def test_the_gate_treats_a_settings_object_without_the_field_as_off() -> None:
    """An older settings object must fail closed, not open."""
    with pytest.raises(PreflightError):
        assert_repo_url_allowed("/srv/praxis/r.git", object())
