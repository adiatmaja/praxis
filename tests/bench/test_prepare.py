"""Instance preparation is the thing the whole benchmark rests on.

A repo at the wrong commit, or one that still holds the answer, silently
produces a run that measures nothing.  These tests use a real local git repo as
the upstream, so no network, and they check the negative property directly: the
gold commit OBJECT must be gone from the prepared object store, not merely
unreferenced.  Counting reachable commits is blind to that difference, because
a local clone hardlinks the whole upstream object store in and the answer stays
readable by sha.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

from bench import prepare as prepare_module
from bench.prepare import (
    InstanceSpec,
    PreparationError,
    _already_prepared,
    _verify_prepared,
    main,
    prepare_instance,
    tracked_file_count,
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_exit(*args: str, cwd: Path) -> int:
    """Run git and return the exit code, for probes that must FAIL."""
    return subprocess.run(
        ["git", *args], cwd=cwd, check=False, capture_output=True, text=True
    ).returncode


class Upstream(NamedTuple):
    """The fixture repo, plus the shas a prepared repo must and must not hold."""

    repo: Path
    base: str
    gold: str
    dangling: str


@pytest.fixture(scope="module")
def upstream(tmp_path_factory) -> Upstream:
    """A repo with two commits, a release tag on the fix, and a stray object.

    The tag is not decoration.  Real SWE-bench repos (django, sympy, astropy)
    carry hundreds of release tags, and a tag on any commit after the base keeps
    the answer reachable no matter what the branches say.  The stray blob is the
    other half: a local clone copies unreferenced objects too, so preparation
    has to prune the store, not just fix the refs.

    The default branch is ``master``, NOT ``main``, and a second branch sits on
    the gold commit.  Both details are load bearing.  A fixture whose only
    branch is already called ``main`` and already sits at the base makes two
    mutations invisible: a sweep that spares branches leaves nothing behind to
    notice, and a preparation that never writes ``symbolic-ref HEAD`` still
    finds HEAD pointing at the right place by accident.  Measured: with a
    single ``main`` branch both of those survive the entire suite green.  Here
    a spared branch is a real leak, since ``stable`` carries the answer, and a
    missing ``symbolic-ref`` is a real dangling HEAD.

    READ ONLY, and shared across the module because every git call here costs
    real wall clock on Windows.  Preparation only ever clones from it, and a
    clone plus prune provably leaves it untouched, so nothing in this file may
    write to it.  Anything that needs a mutable repo takes ``prepared``, which
    stays per test.
    """
    repo = tmp_path_factory.mktemp("upstream")
    _git("init", "-b", "master", cwd=repo)
    _git("config", "user.email", "t@e.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "util.py").write_text("X = 1\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "buggy state", cwd=repo)
    base = _git("rev-parse", "HEAD", cwd=repo)

    (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    _git("commit", "-am", "the gold fix", cwd=repo)
    gold = _git("rev-parse", "HEAD", cwd=repo)
    _git("tag", "v2.0", cwd=repo)
    _git("branch", "stable", gold, cwd=repo)

    stray = repo / "stray.txt"
    stray.write_text("the gold patch, held by no ref at all\n", encoding="utf-8")
    dangling = _git("hash-object", "-w", "stray.txt", cwd=repo)
    stray.unlink()

    return Upstream(repo=repo, base=base, gold=gold, dangling=dangling)


@pytest.fixture(scope="module")
def packed_upstream(upstream, tmp_path_factory) -> Upstream:
    """A COPY of the fixture repo whose objects live in a ``.keep``-marked pack.

    ``git clone --local`` hardlinks everything under ``objects/`` in wholesale,
    ``pack-*.keep`` markers included, and ``gc --prune=now`` will not repack a
    kept pack.  An interrupted fetch or clone leaves that marker behind
    permanently, so the upstream does not have to be unusual for a bench run to
    hit this.  The fixture repo itself has only loose objects, so the pack has
    to be built here for the marker to mean anything.

    READ ONLY for the same reason ``upstream`` is.
    """
    repo = tmp_path_factory.mktemp("packed") / "upstream"
    shutil.copytree(upstream.repo, repo)
    _git("repack", "-a", "-d", "-q", cwd=repo)
    packs = sorted((repo / ".git" / "objects" / "pack").glob("*.pack"))
    assert packs, "repack produced no pack for the .keep marker to protect"
    packs[0].with_suffix(".keep").write_text("", encoding="utf-8")
    return upstream._replace(repo=repo)


def _reintroduce_the_gold_object(bare: Path, upstream: Upstream) -> None:
    """Put the gold commit back into ``bare`` as an object no ref can reach.

    This is the swept-but-unpruned state exactly: a run that deleted the refs
    and then died, or was killed, before ``gc --prune=now`` finished.  Measured
    on the fixture, every cheap signal afterwards is identical to a clean repo
    (one ref, HEAD on it, ``rev-list --count --all`` of 1, no remotes) while
    ``git show <gold>:app.py`` still prints the answer.  That is what makes this
    the state a re-run launders instead of rebuilding.
    """
    _git(
        "fetch", "--no-tags", str(upstream.repo), f"{upstream.gold}:refs/tmp", cwd=bare
    )
    _git("update-ref", "-d", "refs/tmp", cwd=bare)


@pytest.fixture
def spec(upstream) -> InstanceSpec:
    return InstanceSpec(
        instance_id="test__repo-1",
        upstream=str(upstream.repo),
        base_commit=upstream.base,
    )


@pytest.fixture
def prepared(spec, tmp_path) -> Path:
    return prepare_instance(spec, tmp_path / "work")


@pytest.mark.integration
def test_prepared_repo_is_bare(prepared):
    assert _git("rev-parse", "--is-bare-repository", cwd=prepared) == "true"


@pytest.mark.integration
def test_prepared_repo_head_is_the_buggy_base_commit(prepared, upstream):
    """The sha is preserved, so the extracted patch lines up with the grader.

    The official SWE-bench harness checks out ``base_commit`` by sha.  A
    re-rooted history would satisfy every content check here and still hand the
    grader a diff against a commit it has never heard of.
    """
    assert _git("rev-parse", "main", cwd=prepared) == upstream.base


@pytest.mark.integration
def test_head_points_at_main(prepared):
    assert _git("symbolic-ref", "HEAD", cwd=prepared) == "refs/heads/main"


@pytest.mark.integration
def test_the_gold_fix_is_not_present_in_the_prepared_repo(prepared):
    """The single most important property: the answer is not in the repo."""
    assert "return 1" in _git("show", "main:app.py", cwd=prepared)
    assert "return 2" not in _git("show", "main:app.py", cwd=prepared)


@pytest.mark.integration
def test_no_later_commit_is_reachable(prepared):
    """A worker must not be able to ``git log`` its way to the gold patch."""
    assert int(_git("rev-list", "--count", "--all", cwd=prepared)) == 1


@pytest.mark.integration
def test_the_gold_commit_object_is_gone_from_the_object_store(prepared, upstream):
    """Unreachable is not enough, the object has to be GONE.

    ``rev-list --count --all`` counts what the refs reach and so cannot see this
    at all: a default local clone hardlinks the entire upstream object store, and
    ``git show <gold sha>:app.py`` then prints the answer while the commit count
    still reads 1.  This is the assertion that catches it.
    """
    assert _git_exit("cat-file", "-e", upstream.gold, cwd=prepared) != 0
    assert _git_exit("cat-file", "-e", f"{upstream.gold}^{{commit}}", cwd=prepared) != 0
    assert _git_exit("show", f"{upstream.gold}:app.py", cwd=prepared) != 0


@pytest.mark.integration
def test_a_stray_upstream_object_is_not_carried_over(prepared, upstream):
    """A local clone copies unreferenced objects too; the prune must drop them."""
    assert _git_exit("cat-file", "-e", upstream.dangling, cwd=prepared) != 0


@pytest.mark.integration
def test_no_tags_survive(prepared):
    assert _git("tag", "-l", cwd=prepared) == ""


@pytest.mark.integration
def test_exactly_one_ref_survives(prepared):
    """The sweep is the guarantee; anything else is a path back to the answer."""
    refs = _git("for-each-ref", "--format=%(refname)", cwd=prepared).splitlines()
    assert refs == ["refs/heads/main"]


@pytest.mark.integration
def test_no_origin_remote_is_configured(prepared):
    """On a real instance ``remote.origin.url`` is the live GitHub repo.

    Leaving it configured means one ``git fetch origin`` reaches the gold patch
    over the network, whatever the local object store says.
    """
    assert _git("remote", cwd=prepared) == ""
    assert _git_exit("config", "--get", "remote.origin.url", cwd=prepared) != 0


@pytest.mark.integration
def test_tracked_file_count_matches_git(prepared):
    assert tracked_file_count(prepared, "main") == 2


@pytest.mark.integration
def test_tree_at_main_equals_the_upstream_tree_at_the_base_commit(prepared, upstream):
    """Independent of every ref check: the content IS the buggy base.

    Comparing tree shas catches anything that changed the tree, including an
    extra bookkeeping file committed alongside the real content, which no ref or
    reachability assertion would notice.
    """
    assert _git("rev-parse", "main^{tree}", cwd=prepared) == _git(
        "rev-parse", f"{upstream.base}^{{tree}}", cwd=upstream.repo
    )


@pytest.mark.integration
def test_preparation_is_idempotent(spec, upstream, tmp_path):
    """The guard must RETURN EARLY, not quietly re-clone on every call.

    The sentinel is what makes that visible.  Asserting on ``rev-parse main``
    alone passes just as happily when the repo was torn down and rebuilt, which
    is exactly the failure this test exists to catch.
    """
    root = tmp_path / "work"
    first = prepare_instance(spec, root)
    sentinel = first / "SENTINEL"
    sentinel.write_text("survived\n", encoding="utf-8")

    second = prepare_instance(spec, root)

    assert first == second
    assert sentinel.exists()
    assert _git("rev-parse", "main", cwd=second) == upstream.base


@pytest.mark.integration
def test_a_repo_that_no_longer_holds_the_invariants_is_rebuilt(
    spec, upstream, tmp_path
):
    """The guard is a check on the repo, not a directory-exists test.

    A repo prepared by an older or half-finished run must not be trusted just
    because the path is there.  The rebuild also has to delete git's read-only
    pack files, so the sentinel disappearing here is the Windows rmtree
    regression test as well.
    """
    root = tmp_path / "work"
    bare = prepare_instance(spec, root)
    sentinel = bare / "SENTINEL"
    sentinel.write_text("should not survive\n", encoding="utf-8")
    _git("update-ref", "refs/tags/leak", upstream.base, cwd=bare)

    again = prepare_instance(spec, root)

    assert again == bare
    assert not sentinel.exists()
    refs = _git("for-each-ref", "--format=%(refname)", cwd=again).splitlines()
    assert refs == ["refs/heads/main"]


@pytest.mark.integration
def test_verification_rejects_a_repo_holding_an_unreachable_object(prepared, upstream):
    """The object store has to BE the reachable closure, not merely contain it.

    Sweeping the refs without finishing the prune leaves a repo that answers
    every cheap question the way a clean one does.  Asserting those first is the
    point of this test: it shows that nothing short of counting the store can
    tell the two apart, which is why the closure count is the check that runs on
    every real instance.
    """
    _reintroduce_the_gold_object(prepared, upstream)

    assert _git("for-each-ref", "--format=%(refname)", cwd=prepared).splitlines() == [
        "refs/heads/main"
    ]
    assert _git("symbolic-ref", "HEAD", cwd=prepared) == "refs/heads/main"
    assert _git("rev-parse", "main", cwd=prepared) == upstream.base
    assert int(_git("rev-list", "--count", "--all", cwd=prepared)) == 1
    assert _git_exit("cat-file", "-e", upstream.gold, cwd=prepared) == 0

    with pytest.raises(PreparationError, match="reachable"):
        _verify_prepared(prepared, upstream.base)


@pytest.mark.integration
def test_already_prepared_is_false_for_a_repo_holding_the_gold_object(
    prepared, upstream
):
    """The guard is where the hole was: it verifies without any clone-time state.

    A check that needs something recorded during the clone cannot run here at
    all, so it silently degraded to nothing and a leaking repo passed.
    """
    assert _already_prepared(prepared, upstream.base) is True

    _reintroduce_the_gold_object(prepared, upstream)

    assert _already_prepared(prepared, upstream.base) is False


@pytest.mark.integration
def test_a_repo_holding_the_gold_object_is_rebuilt_not_returned(
    spec, upstream, tmp_path
):
    """The regression test for the laundering path, end to end.

    Preparation does not delete the directory when a step after the clone
    fails, and the driver has no per-instance recovery, so the operator's
    natural response to a failed run is to re-run the same command.  If that
    second run returns the half-prepared repo, it logs success, exits zero, and
    every number the whole matrix produces afterwards is measured against a repo
    that contains its own answer.
    """
    root = tmp_path / "work"
    bare = prepare_instance(spec, root)
    _reintroduce_the_gold_object(bare, upstream)
    sentinel = bare / "SENTINEL"
    sentinel.write_text("should not survive\n", encoding="utf-8")

    again = prepare_instance(spec, root)

    assert again == bare
    assert not sentinel.exists()
    assert _git_exit("cat-file", "-e", upstream.gold, cwd=again) != 0
    assert _git_exit("show", f"{upstream.gold}:app.py", cwd=again) != 0


@pytest.mark.integration
@pytest.mark.parametrize("failing_step", ["_sweep_refs", "_verify_prepared"])
def test_a_failed_preparation_leaves_nothing_on_disk(
    spec, tmp_path, monkeypatch, failing_step
):
    """A half-prepared repo on disk is what the next run trusts.

    Whatever went wrong, the directory must be gone, so the next run rebuilds
    from the upstream instead of inspecting whatever the failed one left.  Both
    injection points matter: a git step failing part way through the sweep, and
    the verifier itself rejecting the finished repo.
    """

    def boom(*args: object, **kwargs: object) -> None:
        msg = "injected failure"
        raise PreparationError(msg)

    monkeypatch.setattr(prepare_module, failing_step, boom)
    root = tmp_path / "work"

    with pytest.raises(PreparationError, match="injected failure"):
        prepare_instance(spec, root)

    assert not (root / f"{spec.instance_id}.git").exists()


@pytest.mark.integration
def test_a_kept_pack_in_the_upstream_cannot_smuggle_the_answer_through(
    packed_upstream, tmp_path
):
    """``gc --prune=now`` will not repack a pack the clone hardlinked a ``.keep`` for.

    Measured on git 2.52.0.windows.1 with the marker left in place: refs, HEAD,
    remotes and ``rev-list --count --all`` all read exactly like a clean repo
    while the gold commit stays readable by sha out of the kept pack.
    """
    spec = InstanceSpec(
        instance_id="test__repo-kept-pack",
        upstream=str(packed_upstream.repo),
        base_commit=packed_upstream.base,
    )

    bare = prepare_instance(spec, tmp_path / "work")

    assert not list((bare / "objects" / "pack").glob("*.keep"))
    assert _git_exit("cat-file", "-e", packed_upstream.gold, cwd=bare) != 0
    assert _git("rev-parse", "main", cwd=bare) == packed_upstream.base


@pytest.mark.integration
def test_verification_rejects_a_repo_with_a_stray_ref(prepared, upstream):
    """The fail-closed backstop for the real repos no test will ever see."""
    _git("update-ref", "refs/tags/leak", upstream.base, cwd=prepared)
    with pytest.raises(PreparationError, match="refs other than"):
        _verify_prepared(prepared, upstream.base)


@pytest.mark.integration
def test_verification_rejects_a_repo_whose_main_is_not_the_base(prepared, upstream):
    with pytest.raises(PreparationError, match="not the base commit"):
        _verify_prepared(prepared, "0" * 40)


@pytest.mark.integration
def test_verification_rejects_a_repo_that_borrows_objects(prepared, upstream):
    """A borrowed object store is the one leak the closure count cannot see.

    Cloning a mirror that was built with ``--shared`` copies its
    ``objects/info/alternates`` verbatim, and ``gc --prune=now`` cannot prune a
    store the repo does not own, so the donor's whole history stays readable by
    sha.  Measured: gold still resolved after the full sweep and prune.  Counting
    objects does not catch it either, because the donor's objects are not in this
    repo's store: everything reachable is local, the counts agree, and the extra
    history sits outside both numbers.  This check has to stay its own check.
    """
    alternates = prepared / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text("/somewhere/else/objects\n", encoding="utf-8")
    with pytest.raises(PreparationError, match="borrows objects"):
        _verify_prepared(prepared, upstream.base)


@pytest.mark.integration
def test_a_base_commit_that_is_not_in_the_upstream_is_refused(upstream, tmp_path):
    """A typo in the sample file must fail loudly, not prepare a wrong repo."""
    bad = InstanceSpec(
        instance_id="test__repo-2", upstream=str(upstream.repo), base_commit="0" * 40
    )
    with pytest.raises(PreparationError, match="is not in"):
        prepare_instance(bad, tmp_path / "work")


@pytest.mark.unit
@pytest.mark.parametrize("instance_id", ["../escape", "a/b", "..", ""])
def test_an_instance_id_that_escapes_the_root_is_refused(instance_id, tmp_path):
    """Preparation DELETES the path it resolves, so the id is never trusted."""
    bad = InstanceSpec(instance_id=instance_id, upstream="unused", base_commit="0" * 40)
    with pytest.raises(ValueError, match="unsafe instance id"):
        prepare_instance(bad, tmp_path / "work")


@pytest.mark.integration
def test_main_prepares_every_instance_in_the_sample(upstream, tmp_path):
    """The CLI is how the run actually calls this, so it gets a test too."""
    sample = tmp_path / "sample.json"
    sample.write_text(
        json.dumps(
            {
                "instances": [
                    {
                        "instance_id": "test__repo-1",
                        "upstream": str(upstream.repo),
                        "base_commit": upstream.base,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    root = tmp_path / "work"

    assert main(["--sample", str(sample), "--root", str(root)]) == 0

    bare = root / "test__repo-1.git"
    assert _git("rev-parse", "main", cwd=bare) == upstream.base
    assert _git_exit("cat-file", "-e", upstream.gold, cwd=bare) != 0
