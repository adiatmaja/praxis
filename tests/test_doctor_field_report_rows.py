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
    _AGY_FAILURE_MARKERS,
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


#: The REAL output of `agy models`, authenticated, captured 2026-08-25 from
#: `agy-agent:latest` against the `praxis-gemini-creds` volume. Two
#: tab-separated columns, `id` then display name, under a progress line.
#:
#: This fixture is the reason the file exists in this shape. Every earlier
#: fixture here was IMAGINED (display names only, no progress line, no tabs)
#: and every one of them passed against a classifier that rejected the real
#: thing outright: the progress line ends in "." and the terminal-punctuation
#: rule threw the whole answer away, so a WORKING install was told to consider
#: wiping credentials that only an interactive browser flow can restore. The
#: broken install, meanwhile, graded correctly. Inverted polarity, hidden by
#: invented data.
_REAL_AGY_MODELS_OUTPUT = (
    "Fetching available models...\n"
    "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n"
    "gemini-3.7-flash-medium\tGemini 3.7 Flash (Medium)\n"
    "gemini-3.7-flash-low\tGemini 3.7 Flash (Low)\n"
    "gemini-3.6-flash-high\tGemini 3.6 Flash (High)\n"
    "gemini-3.6-flash-medium\tGemini 3.6 Flash (Medium)\n"
    "gemini-3.6-flash-low\tGemini 3.6 Flash (Low)\n"
    "gemini-3.5-flash-high\tGemini 3.5 Flash (High)\n"
    "gemini-3.5-flash-medium\tGemini 3.5 Flash (Medium)\n"
    "gemini-3.5-flash-low\tGemini 3.5 Flash (Low)\n"
    "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
    "gemini-3.1-pro-low\tGemini 3.1 Pro (Low)\n"
    "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
    "claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n"
    "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n"
)

#: The REAL output on a fresh, unauthenticated volume, same capture. Note that
#: it carries the SAME progress line, so the two paths are distinguished by
#: the sign-in sentence alone.
_REAL_AGY_SIGNED_OUT_OUTPUT = (
    "Fetching available models...\n"
    "Error: Please sign in to view available models. "
    "Launch the CLI without arguments to sign in.\n"
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
def test_the_real_authenticated_output_classifies_as_models() -> None:
    """The measured answer, which the first version of this rejected outright.

    Both columns are kept, because both are names an operator may have
    configured: `config/praxis.yaml` ships the DISPLAY form while an
    API-shaped config uses the id.
    """
    kind, models = classify_agy_models(_REAL_AGY_MODELS_OUTPUT)

    assert kind == AGY_MODELS
    assert len(models) == 14
    assert models[0].model_id == "gemini-3.7-flash-high"
    assert models[0].display == "Gemini 3.7 Flash (High)"
    assert models[-1].display == "GPT-OSS 120B (Medium)"


@pytest.mark.unit
def test_the_real_signed_out_output_classifies_as_signed_out() -> None:
    """Same progress line as the authenticated path; only the sentence differs."""
    kind, models = classify_agy_models(_REAL_AGY_SIGNED_OUT_OUTPUT)

    assert kind == AGY_SIGNED_OUT
    assert models == []


@pytest.mark.unit
def test_the_shipped_default_model_is_found_in_the_real_output() -> None:
    """The polarity bug end to end, pinned on real data.

    `config/praxis.yaml` ships `Gemini 3.7 Flash (High)`. Compared against
    whole `id<TAB>Display` lines it never matched, so the row announced that
    the configured model was "not among them" in the same sentence as a list
    containing it: a row denying the presence of a string it was printing.
    """
    _, models = classify_agy_models(_REAL_AGY_MODELS_OUTPUT)

    assert any(m.matches("Gemini 3.7 Flash (High)") for m in models)
    assert any(m.matches("gemini-3.7-flash-high") for m in models)
    assert not any(m.matches("Gemini 9.9 Imaginary (High)") for m in models)


@pytest.mark.unit
def test_a_sign_in_prompt_classifies_as_signed_out() -> None:
    kind, models = classify_agy_models("Please sign in to view available models")
    assert kind == AGY_SIGNED_OUT
    assert models == []


@pytest.mark.unit
def test_empty_output_is_its_own_classification() -> None:
    assert classify_agy_models("   \n\n")[0] == AGY_EMPTY


@pytest.mark.unit
def test_a_progress_line_alone_is_not_a_model_list() -> None:
    """Dropping the progress line must not leave an empty list reading green."""
    assert classify_agy_models("Fetching available models...")[0] == AGY_UNRECOGNIZED


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


#: The failure words this file pins, written out HERE rather than imported.
#:
#: Parametrizing over the production tuple looked equivalent and was vacuous:
#: mutation testing showed that deleting "quota" from `_AGY_FAILURE_MARKERS`
#: also deleted the "quota" CASE, so the suite stayed green having silently
#: stopped testing the thing that was removed. A test whose case list is
#: generated from the code under test cannot notice that code shrinking.
#: Duplicated on purpose, with the equality check below as the seam.
_PINNED_FAILURE_MARKERS = (
    "error",
    "failed",
    "failure",
    "unable",
    "denied",
    "quota",
    "unauthorized",
    "expired",
    "invalid",
    "timed out",
    "refused",
)


@pytest.mark.unit
def test_the_pinned_failure_words_match_production() -> None:
    """The seam that makes the duplication above safe, in both directions.

    Catches a marker added to production with no case, and a marker removed
    from production that this file still believes in.
    """
    assert set(_PINNED_FAILURE_MARKERS) == set(_AGY_FAILURE_MARKERS)


@pytest.mark.unit
@pytest.mark.parametrize("marker", _PINNED_FAILURE_MARKERS)
def test_every_failure_word_is_load_bearing_on_its_own(marker: str) -> None:
    """One isolating case per marker, because a list is not one guard.

    Review found only "error" load-bearing: every other word shared all its
    scenarios with the list-structure rule, so the rest could be deleted with
    the suite green. Each case here is a STRUCTURED failure report (a heading
    plus two entries), a shape every structural rule accepts, carrying exactly
    one marker. A marker added without a case is dead weight nothing notices.
    """
    text = f"Models:\n  the operation reported {marker}\n  see the log"

    assert classify_agy_models(text)[0] == AGY_UNRECOGNIZED


@pytest.mark.unit
def test_a_structured_failure_report_is_still_not_a_model_list() -> None:
    """The failure-word rule against a shape every structural signal accepts."""
    text = "Error fetching models:\n  connection reset\n  retry limit reached"
    assert classify_agy_models(text)[0] == AGY_UNRECOGNIZED


# Each structural signal below appears ALONE, because four signals sharing one
# scenario are one signal. Review found `header` and `bulleted` both deletable
# with the suite green -- only `len(candidates) > 1` was doing any work, and
# the fix for an earlier masking pair had reintroduced the same fault in the
# same expression.


@pytest.mark.unit
def test_a_single_tabbed_entry_is_a_list_on_the_tab_alone() -> None:
    """The signal the REAL output depends on: tabs, no heading, no bullets.

    Without it, an account with a single model reads as ungraded output.
    """
    kind, models = classify_agy_models("gemini-3.7-flash-high\tGemini 3.7 Flash (High)")

    assert kind == AGY_MODELS
    assert len(models) == 1


@pytest.mark.unit
def test_a_single_entry_under_a_heading_is_a_list_on_the_heading_alone() -> None:
    kind, models = classify_agy_models("Available models:\nGemini 3.7 Pro")

    assert kind == AGY_MODELS
    assert [m.display for m in models] == ["Gemini 3.7 Pro"]


@pytest.mark.unit
def test_a_single_bulleted_entry_is_a_list_on_the_bullet_alone() -> None:
    kind, models = classify_agy_models("- Gemini 3.7 Pro")

    assert kind == AGY_MODELS
    assert [m.display for m in models] == ["Gemini 3.7 Pro"]


@pytest.mark.unit
def test_two_bare_entries_are_a_list_on_the_count_alone() -> None:
    kind, models = classify_agy_models("Gemini 3.7 Pro\nGemini 3.6 Pro")

    assert kind == AGY_MODELS
    assert len(models) == 2


@pytest.mark.unit
def test_a_sentence_under_a_heading_is_rejected_on_its_terminal_stop() -> None:
    """The trailing-.!? rule, isolated.

    A heading satisfies the structure rule and there is no failure word here,
    so the full stop is the only thing between this and "1 model(s)". It is
    also the rule that misfired on the real progress line, so being able to
    see it move matters.
    """
    text = "Models:\n  the daemon closed the connection while we were reading."

    assert classify_agy_models(text)[0] == AGY_UNRECOGNIZED


@pytest.mark.unit
def test_an_over_long_line_under_a_heading_is_rejected_on_its_length() -> None:
    """The 80-character rule, isolated: no failure word, no terminal stop."""
    long_line = (
        "a line of narration that simply keeps going and going well past any "
        "plausible model name"
    )
    assert len(long_line) > 80
    text = f"Models:\n  {long_line}"

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
        output=_REAL_AGY_SIGNED_OUT_OUTPUT,
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
def test_the_sign_in_remedy_includes_the_mandatory_chown() -> None:
    """Step 1 is not optional, and the hint used to skip straight to step 2.

    Measured: `agy-agent:latest` carries no `/home/agent/.gemini`, so a fresh
    volume is created root-owned while the container runs as uid 1000, and the
    sign-in fails with a permission error. The hint is printed on exactly two
    occasions -- an empty volume, and the wipe it recommends -- and both are
    the case where the chown is required. `docs/deployment.md` and
    `core/harnesses.py` both name it; this hint was a regression against them.
    """
    result = probe_agy_credentials(
        in_play=True,
        reason="the default worker harness is agy",
        probed=True,
        output=_REAL_AGY_SIGNED_OUT_OUTPUT,
        volume="praxis-gemini-creds",
    )

    assert "--user root" in result.hint
    assert "chown -R agent:agent /home/agent/.gemini" in result.hint
    # And the ordering: ownership before login, or the login is what fails.
    assert result.hint.index("chown") < result.hint.index("-c 'agy'")


@pytest.mark.unit
def test_the_remedy_operates_on_the_configured_volume_not_the_default() -> None:
    """GEMINI_CREDS_VOLUME is configurable, so the remedy cannot be a literal.

    With the default passed in, a hardcoded name and a threaded one look
    identical, which is how the row came to name one volume in its detail and
    operate on another in its remedy.
    """
    result = probe_agy_credentials(
        in_play=True,
        reason="the default worker harness is agy",
        probed=True,
        output=_REAL_AGY_SIGNED_OUT_OUTPUT,
        volume="team-gemini-creds",
    )

    assert "team-gemini-creds" in result.detail
    assert "team-gemini-creds:/home/agent/.gemini" in result.hint
    assert "praxis-gemini-creds" not in result.hint


@pytest.mark.unit
def test_the_real_model_list_is_green_and_reports_the_count() -> None:
    result = probe_agy_credentials(
        in_play=True,
        reason="the default worker harness is agy",
        probed=True,
        output=_REAL_AGY_MODELS_OUTPUT,
        volume="praxis-gemini-creds",
    )
    assert result.status is CheckStatus.GREEN
    assert "14 model" in result.detail


@pytest.mark.unit
def test_the_shipped_default_worker_model_keeps_the_real_output_green() -> None:
    """The exact combination that shipped broken: real output, shipped model.

    `config/praxis.yaml` names `Gemini 3.7 Flash (High)` and the volume lists
    it, so this must be GREEN. It was amber, telling a working install its
    configured model was missing from a list that contained it.
    """
    result = probe_agy_credentials(
        in_play=True,
        reason="the default worker harness is agy",
        probed=True,
        output=_REAL_AGY_MODELS_OUTPUT,
        configured_model="Gemini 3.7 Flash (High)",
        volume="praxis-gemini-creds",
    )

    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_a_model_named_by_its_id_also_keeps_the_row_green() -> None:
    """The other column is a real name too, so it must match as well."""
    result = probe_agy_credentials(
        in_play=True,
        reason="the default worker harness is agy",
        probed=True,
        output=_REAL_AGY_MODELS_OUTPUT,
        configured_model="claude-sonnet-4-6",
        volume="praxis-gemini-creds",
    )

    assert result.status is CheckStatus.GREEN


@pytest.mark.unit
def test_a_configured_model_absent_from_the_list_is_a_note_not_a_gate() -> None:
    result = probe_agy_credentials(
        in_play=True,
        reason="the default worker harness is agy",
        probed=True,
        output=_REAL_AGY_MODELS_OUTPUT,
        configured_model="Gemini 9.9 Imaginary (High)",
        volume="praxis-gemini-creds",
    )
    assert result.status is CheckStatus.AMBER
    assert "Gemini 9.9 Imaginary (High)" in result.detail
    # The display names are what an operator configures, so they are what the
    # row lists back.
    assert "Gemini 3.7 Flash (High)" in result.detail


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
def test_container_identity_never_reds_when_it_could_not_identify_itself() -> None:
    """An unknown self is "not probed", never an accusation.

    `_resolve_self` degrades `self_id` to None on ANY failure, and that
    failure is ordinary: `socket.gethostname()` stops resolving to a container
    id under a compose `hostname:`, `network_mode: host`, a `--hostname` flag,
    Podman or Kubernetes. Comparing `None != "<id>"` fell through to RED and
    printed a sentence saying "this container" and "a different container"
    about the same process -- a confident accusation of another checkout,
    manufactured out of a fact nobody obtained.
    """
    result = probe_container_identity(
        container_name="orchestrator",
        in_container=True,
        self_id=None,
        named_id="deadbeef",
        named_project="praxis",
        named_working_dir="/c/working-space/praxis",
    )

    assert result.status is CheckStatus.AMBER
    assert "not probed" in result.detail
    assert "could not say WHICH one" in result.detail
    assert result.hint


@pytest.mark.unit
def test_container_identity_unknown_self_is_amber_with_no_named_container() -> None:
    """Same degradation, and still not a verdict when nothing holds the name."""
    result = probe_container_identity(
        container_name="orchestrator", in_container=True, self_id=None, named_id=None
    )

    assert result.status is CheckStatus.AMBER
    assert "not probed" in result.detail


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
