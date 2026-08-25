"""The three doctor rows a first-time user's lost afternoon paid for.

Each row exists because the failure it names is SILENT: a local `repo_url`
that passes preflight inside the orchestrator and then bind-mounts an empty
directory into the worker; an agy credentials volume that reads as fine
because `agy help` exits 0; a container name that is global to the Docker
daemon, so the doctor table an operator is reading describes a different
checkout's install.

Every outcome of every decision function is pinned here, INCLUDING the ones
that decline to answer.  "not probed" and "not applicable" are verdicts in
this module's vocabulary, and a probe that quietly upgraded either into a
green would be the exact defect these rows were added to remove.
"""
# ruff: noqa: S101

from __future__ import annotations

import pytest

from orchestrator.core.doctor import CHECK_IDS, CheckStatus
from orchestrator.core.doctor_probes import (
    AGY_EMPTY,
    AGY_MODELS,
    AGY_SIGNED_OUT,
    AGY_UNRECOGNIZED,
    LocalRepoFact,
    classify_agy_models,
    probe_agy_credentials,
    probe_container_identity,
    probe_local_repo_paths,
)


# --- registration -----------------------------------------------------------


@pytest.mark.unit
def test_all_three_rows_are_registered() -> None:
    """A probe nobody registered is a probe `run_checks` never calls.

    `run_checks` iterates the CHECKS registry, not the probe map, so a decision
    function that is perfect and unregistered produces no row at all and no
    test failure anywhere else.
    """
    assert "local_repo_paths" in CHECK_IDS
    assert "agy_credentials" in CHECK_IDS
    assert "container_identity" in CHECK_IDS


# --- 4a: the local-repo path round trip -------------------------------------


def _fact(path: str, *, exists: bool = True, project: str = "playground"):
    return LocalRepoFact(project=project, repo_url=path, path=path, exists=exists)


@pytest.mark.unit
def test_no_local_project_is_not_applicable_rather_than_a_pass() -> None:
    result = probe_local_repo_paths([], repos_path="", host_path="")
    assert result.status is CheckStatus.GREEN
    assert "not applicable" in result.detail


@pytest.mark.unit
def test_a_missing_local_repo_path_is_red_and_names_both_namespaces() -> None:
    """The 422 the reporter lost an hour to, turned into a light.

    The detail has to name the SECOND namespace, because a path that is
    missing here is the easy half; the trap is that the same string is also
    the Docker daemon's bind source.
    """
    result = probe_local_repo_paths(
        [_fact("/repos/playground.git", exists=False)],
        repos_path="/repos",
        host_path="/repos",
    )
    assert result.status is CheckStatus.RED
    assert "playground" in result.detail
    assert "/repos/playground.git" in result.detail
    assert "bind-mount" in result.detail.lower()
    assert "host" in result.detail.lower()
    # The remedy must rule out the obvious wrong fix.
    assert "up -d" in result.hint
    assert "restart" in result.hint


@pytest.mark.unit
def test_a_path_outside_the_configured_mount_is_amber_not_green() -> None:
    """The configuration that passes preflight and fails at spawn.

    Docker creates a missing bind SOURCE as an empty directory rather than
    refusing, so this one never surfaces as an error at all: the worker
    clones nothing and the task fails for a reason no log names.
    """
    result = probe_local_repo_paths(
        [_fact("/elsewhere/playground.git")],
        repos_path="/repos",
        host_path="/repos",
    )
    assert result.status is CheckStatus.AMBER
    assert "/elsewhere/playground.git" in result.detail
    assert "LOCAL_REPOS_PATH" in result.detail
    assert result.hint


@pytest.mark.unit
def test_a_path_under_the_configured_mount_is_green() -> None:
    result = probe_local_repo_paths(
        [_fact("/repos/playground.git")], repos_path="/repos", host_path="/repos"
    )
    assert result.status is CheckStatus.GREEN
    assert "/repos" in result.detail


@pytest.mark.unit
def test_the_mount_prefix_matches_on_path_components_not_characters() -> None:
    """`/repos-scratch` is not under `/repos`, however the strings compare."""
    result = probe_local_repo_paths(
        [_fact("/repos-scratch/playground.git")],
        repos_path="/repos",
        host_path="/repos",
    )
    assert result.status is CheckStatus.AMBER


@pytest.mark.unit
def test_an_unconfigured_mount_never_reports_a_path_as_outside_it() -> None:
    """With LOCAL_REPOS_PATH unset there is no prefix to be outside of.

    Both variables unset is the DEFAULT and is a supported configuration on
    Linux, where the orchestrator and the daemon share one namespace. An
    amber here would be permanent on every correct Linux install.
    """
    result = probe_local_repo_paths(
        [_fact("/home/me/repos/playground.git")], repos_path="", host_path=""
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_the_half_set_case_is_detected_and_named() -> None:
    """LOCAL_REPOS_HOST_PATH alone mounts the repos where nothing reads them.

    compose nests the two: `${LOCAL_REPOS_HOST_PATH:-${LOCAL_REPOS_PATH:-...}}`
    is the bind SOURCE and `${LOCAL_REPOS_PATH:-/app/.local-repos-unused}` is
    the TARGET. So HOST_PATH alone is the half-set trap -- the operator's
    repos are mounted at the unused fallback target -- while PATH alone is
    the normal, documented, one-variable case.
    """
    result = probe_local_repo_paths([], repos_path="", host_path="C:/Users/me/repos")
    assert result.status is CheckStatus.RED
    assert "LOCAL_REPOS_HOST_PATH" in result.detail
    assert "LOCAL_REPOS_PATH" in result.detail
    assert ".local-repos-unused" in result.detail
    assert result.hint


@pytest.mark.unit
def test_local_repos_path_alone_is_the_documented_normal_case() -> None:
    """The other half-set direction is NOT a trap, and must not be reported.

    Reported as one, every Docker Desktop install that followed the docs
    (which say to set one variable) would be permanently red.
    """
    result = probe_local_repo_paths(
        [_fact("/repos/playground.git")], repos_path="/repos", host_path=""
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_a_missing_path_outranks_a_path_outside_the_mount() -> None:
    """Red outranks amber, and the missing path is the one that fails loudly."""
    result = probe_local_repo_paths(
        [
            _fact("/elsewhere/a.git", project="a"),
            _fact("/repos/b.git", exists=False, project="b"),
        ],
        repos_path="/repos",
        host_path="/repos",
    )
    assert result.status is CheckStatus.RED
    assert "b" in result.detail


# --- 4b: probing agy auth instead of documenting it -------------------------


@pytest.mark.unit
def test_a_sign_in_prompt_classifies_as_signed_out() -> None:
    kind, models = classify_agy_models("Please sign in to view available models")
    assert kind == AGY_SIGNED_OUT
    assert models == []


@pytest.mark.unit
def test_a_model_list_classifies_as_models() -> None:
    kind, models = classify_agy_models(
        "Available models:\n  Gemini 3.7 Flash (High)\n  Gemini 3.7 Pro (High)\n"
    )
    assert kind == AGY_MODELS
    assert models == ["Gemini 3.7 Flash (High)", "Gemini 3.7 Pro (High)"]


@pytest.mark.unit
def test_empty_output_is_its_own_classification() -> None:
    assert classify_agy_models("   \n\n")[0] == AGY_EMPTY


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "Error: quota exceeded for this account",
        "the request failed after 3 attempts",
        "Something entirely new happened here that no rule covers.",
    ],
)
def test_an_answer_with_no_rule_refuses_to_be_graded(text: str) -> None:
    """The pattern `bench/grade.py` was recently fixed to follow.

    A short error line has exactly the SHAPE of a one-item model list, so
    without this the probe would report "1 model available" for a refusal.
    """
    assert classify_agy_models(text)[0] == AGY_UNRECOGNIZED


@pytest.mark.unit
def test_a_structured_failure_report_is_still_not_a_model_list() -> None:
    """The failure-word rule, in the one case where only it can fire.

    Mutation testing found these two rules masking each other: every example
    above is a lone bare line, which the list-STRUCTURE rule rejects on its
    own, so deleting the failure words changed nothing and the suite stayed
    green. A multi-line error under a heading has every structural signal a
    real listing has, and without the failure words it reports as two
    available models.
    """
    text = "Error fetching models:\n  connection reset\n  retry limit reached"
    assert classify_agy_models(text)[0] == AGY_UNRECOGNIZED


@pytest.mark.unit
def test_agy_row_is_not_applicable_when_no_agy_harness_is_in_play() -> None:
    result = probe_agy_credentials(
        in_play=False, reason="no project uses the agy harness"
    )
    assert result.status is CheckStatus.GREEN
    assert "not applicable" in result.detail
    assert "no project uses the agy harness" in result.detail


@pytest.mark.unit
def test_agy_row_is_amber_and_names_the_reason_when_it_could_not_probe() -> None:
    result = probe_agy_credentials(
        in_play=True,
        reason="the default worker harness is agy",
        probed=False,
        not_probed_reason="the agy-agent:latest image is not built here",
    )
    assert result.status is CheckStatus.AMBER
    assert "not probed" in result.detail
    assert "agy-agent:latest image is not built here" in result.detail


@pytest.mark.unit
def test_a_signed_out_agy_is_amber_and_rules_out_the_command_that_does_not_exist() -> (
    None
):
    """`agy login` is the fix every operator reaches for and it does not exist.

    Naming the real command is half the value; saying that the obvious one is
    not a command at all is the other half, because otherwise the operator
    concludes the remedy is broken rather than that their memory is.
    """
    result = probe_agy_credentials(
        in_play=True,
        reason="the default worker harness is agy",
        probed=True,
        output="Please sign in to view available models",
        volume="praxis-gemini-creds",
    )
    assert result.status is CheckStatus.AMBER
    assert "praxis-gemini-creds" in result.detail
    # The sign-in branch specifically, not the verbatim fallback. Both are
    # amber and both carry the same remedy, so without these two assertions
    # this test passed with sign-in detection deleted entirely: the operator
    # would have been shown "no rule for this answer" for the one answer this
    # row exists to recognise.
    assert "asked for a sign-in" in result.detail
    assert "verbatim" not in result.detail
    assert "-c 'agy'" in result.hint
    assert "agy login" in result.hint
    assert "docker run --rm -it" in result.hint


@pytest.mark.unit
def test_a_model_list_is_green_and_reports_the_count() -> None:
    result = probe_agy_credentials(
        in_play=True,
        reason="the default worker harness is agy",
        probed=True,
        output="Gemini 3.7 Flash (High)\nGemini 3.7 Pro (High)",
        volume="praxis-gemini-creds",
    )
    assert result.status is CheckStatus.GREEN
    assert "2 model" in result.detail


@pytest.mark.unit
def test_a_configured_model_absent_from_the_list_is_a_note_not_a_gate() -> None:
    result = probe_agy_credentials(
        in_play=True,
        reason="the default worker harness is agy",
        probed=True,
        output="Gemini 3.7 Flash (High)\nGemini 3.7 Pro (High)",
        configured_model="Gemini 3.6 Flash (High)",
        volume="praxis-gemini-creds",
    )
    assert result.status is CheckStatus.AMBER
    assert "Gemini 3.6 Flash (High)" in result.detail
    assert "Gemini 3.7 Flash (High)" in result.detail


@pytest.mark.unit
def test_a_configured_model_present_in_the_list_stays_green() -> None:
    result = probe_agy_credentials(
        in_play=True,
        reason="the default worker harness is agy",
        probed=True,
        output="Gemini 3.7 Flash (High)\nGemini 3.7 Pro (High)",
        configured_model="Gemini 3.7 Pro (High)",
        volume="praxis-gemini-creds",
    )
    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_an_unrecognized_answer_is_surfaced_verbatim_not_bucketed() -> None:
    """The whole answer, in the operator's hands, ungraded.

    Summarising this into "agy is not authenticated" would be a verdict this
    check has no basis for, and the operator would never see the sentence that
    actually says what happened.
    """
    output = "GOAWAY received; upstream closed the stream"
    result = probe_agy_credentials(
        in_play=True,
        reason="the default worker harness is agy",
        probed=True,
        output=output,
        volume="praxis-gemini-creds",
    )
    assert result.status is CheckStatus.AMBER
    assert output in result.detail
    assert "verbatim" in result.detail


@pytest.mark.unit
def test_no_output_at_all_is_named_rather_than_read_as_a_refusal() -> None:
    result = probe_agy_credentials(
        in_play=True,
        reason="the default worker harness is agy",
        probed=True,
        output="",
        volume="praxis-gemini-creds",
    )
    assert result.status is CheckStatus.AMBER
    assert "no output" in result.detail


# --- 4d: container ownership ------------------------------------------------


@pytest.mark.unit
def test_container_identity_is_green_when_the_named_container_is_this_one() -> None:
    result = probe_container_identity(
        container_name="orchestrator",
        in_container=True,
        self_id="abc123",
        self_name="orchestrator",
        self_project="praxis",
        named_id="abc123",
        named_project="praxis",
        named_working_dir="/c/working-space/praxis",
    )
    assert result.status is CheckStatus.GREEN
    assert "orchestrator" in result.detail
    assert "praxis" in result.detail


@pytest.mark.unit
def test_container_identity_is_red_when_the_name_belongs_to_another_checkout() -> None:
    """The measured mismatch: `docker logs orchestrator` is a different install.

    The remedy has to name the consequence, because the symptom an operator
    actually sees is a database that looks wiped, which reads as data loss
    rather than as a naming collision.
    """
    result = probe_container_identity(
        container_name="orchestrator",
        in_container=True,
        self_id="abc123",
        self_name="orchestrator-old",
        self_project="praxis-newcomer",
        named_id="def456",
        named_project="praxis",
        named_working_dir="/c/working-space/praxis",
    )
    assert result.status is CheckStatus.RED
    assert "praxis-newcomer" in result.detail
    assert "praxis" in result.detail
    assert "/c/working-space/praxis" in result.detail
    assert "database" in result.hint.lower()
    assert "PRAXIS_CONTAINER_NAME" in result.hint


@pytest.mark.unit
def test_container_identity_names_a_bare_process_beside_a_named_container() -> None:
    result = probe_container_identity(
        container_name="orchestrator",
        in_container=False,
        named_id="def456",
        named_project="praxis",
        named_working_dir="/c/working-space/praxis",
    )
    assert result.status is CheckStatus.AMBER
    assert "outside Docker" in result.detail
    assert "/c/working-space/praxis" in result.detail


@pytest.mark.unit
def test_container_identity_is_not_applicable_with_no_container_anywhere() -> None:
    result = probe_container_identity(
        container_name="orchestrator", in_container=False, named_id=None
    )
    assert result.status is CheckStatus.GREEN
    assert "not applicable" in result.detail


@pytest.mark.unit
def test_container_identity_flags_a_name_no_container_holds() -> None:
    """PRAXIS_CONTAINER_NAME was edited and applied with `restart`.

    A container's name is baked in at CREATE, exactly like a mount, so the
    edit did nothing and every `docker logs <new name>` since has failed.
    """
    result = probe_container_identity(
        container_name="orchestrator-b",
        in_container=True,
        self_id="abc123",
        self_name="orchestrator",
        self_project="praxis-newcomer",
        named_id=None,
    )
    assert result.status is CheckStatus.AMBER
    assert "orchestrator-b" in result.detail
    assert "orchestrator" in result.detail
    assert "up -d" in result.hint


@pytest.mark.unit
def test_container_identity_is_not_probed_when_the_daemon_would_not_say() -> None:
    result = probe_container_identity(
        container_name="orchestrator",
        in_container=True,
        error="APIError: 500 Server Error",
    )
    assert result.status is CheckStatus.AMBER
    assert "not probed" in result.detail
    assert "APIError" in result.detail
