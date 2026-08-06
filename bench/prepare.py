"""Turn a SWE-bench instance into a local bare repo at its buggy base commit.

The critical property is negative: the gold patch, and every commit after the
base, must be UNREACHABLE from the prepared repo.  A worker that can ``git log``
its way to the answer measures nothing, and nobody finds out until the whole run
is spent.

``refs/heads/main`` is set to the TRUE base commit sha, keeping its real
ancestry.  Descendants are unreachable from an ancestor by construction, so
nothing needs re-rooting, and preserving the sha keeps the extracted patch lined
up with the commit the official SWE-bench grader checks out.  What re-rooting
would not have fixed either, and what preparation therefore does explicitly:

1. every ref other than ``refs/heads/main`` is deleted, because a release tag on
   a later commit hands over the answer no matter where the branches point;
2. the ``origin`` remote is dropped, because on a real instance its URL is the
   live GitHub repo and one ``git fetch`` reaches everything;
3. every ``objects/pack/*.keep`` marker is removed, because a local clone
   hardlinks the pack directory in wholesale and ``gc --prune=now`` refuses to
   repack a kept pack, so every unreachable object in it would survive;
4. the object store is pruned, because a local clone hardlinks the whole
   upstream store in, unreferenced objects included, and ``git show <sha>`` still
   answers from an object no ref can reach;
5. a repo that borrows objects through ``objects/info/alternates`` is refused,
   because pruning cannot touch a store the repo does not own.  A local clone of
   an alternates-backed mirror copies that file verbatim, which leaves the
   donor's entire history readable by sha with no ref to give it away.

Preparation then re-reads those invariants from the finished repo and raises on
any of them, so a half-prepared instance fails the run instead of silently
measuring nothing.  The load-bearing one is arithmetic: the object store must
EQUAL the reachable closure, object for object.  That needs nothing recorded
during the clone, so it holds equally when an existing repo is re-verified on a
later run, which is precisely where a leak would otherwise be laundered into a
success.  It catches extra objects, which is a leak, and missing ones, which is
a partial or truncated clone.  Anything that fails after the clone takes the
directory with it, so the next run rebuilds instead of trusting the wreckage.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import stat
import subprocess  # nosec B404 - the git CLI is the interface
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.config import DEFAULT_BENCH_ROOT


logger = logging.getLogger(__name__)

# The one ref a prepared repo is allowed to have.
MAIN_REF = "refs/heads/main"


class PreparationError(RuntimeError):
    """Raised when a prepared repo does not hold its own invariants."""


@dataclass(frozen=True)
class InstanceSpec:
    """One SWE-bench instance, reduced to what preparation needs.

    Attributes:
        instance_id: SWE-bench id, e.g. ``django__django-11099``.
        upstream: Anything ``git clone`` accepts, a URL or a local path.
        base_commit: The buggy commit the issue is filed against.
    """

    instance_id: str
    upstream: str
    base_commit: str


def _git(*args: str, cwd: Path | str | None = None) -> str:
    """Run git and return stripped stdout, raising on a non-zero exit."""
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_exit(*args: str, cwd: Path | str | None = None) -> int:
    """Run git and return its exit code, for probes that may legitimately fail."""
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode


def _git_stdin(payload: str, *args: str, cwd: Path | str | None = None) -> None:
    """Run git with ``payload`` on stdin, raising on a non-zero exit.

    Anything that scales with the number of refs goes through here rather than
    onto the command line.  A real instance carries thousands of tags, and the
    argv limit is not generous: measured on this machine, a single git call took
    790 forty-character shas and refused 800 with ``FileNotFoundError: [WinError
    206] The filename or extension is too long``.  Note the type.  That is not a
    ``CalledProcessError``, so a caller guarding the call with ``except
    subprocess.CalledProcessError`` does not catch it and the exception escapes
    from wherever it happens to be raised.
    """
    subprocess.run(  # nosec B603 - fixed argv, no shell
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=True,
        capture_output=True,
        text=True,
        input=payload,
    )


def _on_rm_error(func: Callable[..., Any], path: Any, exc_info: Any) -> None:
    """Clear the read-only bit git sets, then retry the removal.

    Git marks pack files, their indexes, and ``objects/info/commit-graph``
    read-only.  On Windows that makes ``shutil.rmtree`` raise ``PermissionError:
    [WinError 5] Access is denied``, so a stale prepared repo could never be
    replaced.  Python here is 3.11, where the handler parameter is ``onerror``.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _force_rmtree(path: Path) -> None:
    """Remove a git directory tree, read-only pack files included."""
    shutil.rmtree(path, onerror=_on_rm_error)


def _bare_path(instance_id: str, root: Path) -> Path:
    """Return the bare repo path for ``instance_id`` under ``root``.

    Instance ids arrive from a sample file and preparation DELETES the directory
    they resolve to, so an id that is not a plain filename is refused rather
    than trusted.

    Raises:
        ValueError: If the id is empty or could escape ``root``.
    """
    if (
        not instance_id
        or instance_id in {".", ".."}
        or instance_id != Path(instance_id).name
    ):
        msg = f"unsafe instance id: {instance_id!r}"
        raise ValueError(msg)
    return root / f"{instance_id}.git"


def _ref_targets(bare: Path) -> dict[str, str]:
    """Return ``{refname: objectname}`` for every ref in ``bare``."""
    listing = _git("for-each-ref", "--format=%(objectname) %(refname)", cwd=bare)
    targets: dict[str, str] = {}
    for line in listing.splitlines():
        if not line:
            continue
        objectname, _, refname = line.partition(" ")
        targets[refname] = objectname
    return targets


def _sweep_refs(bare: Path) -> None:
    """Delete every ref except ``MAIN_REF``, in ONE update-ref transaction.

    A subprocess per ref is measurably not free: about 50 ms each on Windows,
    which is roughly 20 seconds on a django-sized instance and over half an hour
    of pure process spawn across a hundred-instance run.  Feeding the names on
    stdin also keeps them off the command line, where a few thousand tags would
    overflow the argv limit.

    ``--no-deref`` deletes the ref that was named and never the ref a symbolic
    ref happens to point at, which could otherwise quietly take
    ``refs/heads/main`` with it.
    """
    doomed = [name for name in _ref_targets(bare) if name != MAIN_REF]
    if not doomed:
        return
    payload = "".join(f"delete {name}\0\0" for name in doomed)
    _git_stdin(payload, "update-ref", "--no-deref", "--stdin", "-z", cwd=bare)


def _drop_pack_keeps(bare: Path) -> None:
    """Remove every ``objects/pack/*.keep`` marker the clone brought in.

    ``git clone`` from a local path hardlinks the whole pack directory in,
    ``pack-*.keep`` markers included, and ``gc --prune=now`` will not repack a
    kept pack.  Every unreachable object in it then survives, with refs, HEAD,
    remotes and ``rev-list --count --all`` all reading exactly like a clean repo.
    Verified on git 2.52.0.windows.1: with the marker left in place the gold
    commit was still readable by sha after the full sweep and prune.  An
    interrupted fetch or clone leaves that marker behind permanently, so an
    upstream does not have to be unusual to trigger it.
    """
    pack_dir = bare / "objects" / "pack"
    if not pack_dir.is_dir():
        return
    for marker in sorted(pack_dir.glob("*.keep")):
        marker.unlink()


def _object_counts(bare: Path) -> tuple[int, int]:
    """Return ``(reachable, present)`` object counts for ``bare``.

    ``reachable`` walks every ref; ``present`` is what the store actually holds,
    loose plus packed.  ``--no-object-names`` keeps the reachable side one sha
    per line, so a path containing a newline cannot inflate the count.
    ``prune-packable`` is how many loose objects are also in a pack, and is
    subtracted because those appear in both of the other two numbers, so the
    result counts DISTINCT objects either way.

    Raises:
        PreparationError: If ``count-objects`` did not report what it must.
    """
    listing = _git("rev-list", "--objects", "--all", "--no-object-names", cwd=bare)
    reachable = len([line for line in listing.splitlines() if line])

    fields: dict[str, int] = {}
    for line in _git("count-objects", "-v", cwd=bare).splitlines():
        key, _, value = line.partition(":")
        if value.strip().isdigit():
            fields[key.strip()] = int(value.strip())
    missing = sorted({"count", "in-pack"} - set(fields))
    if missing:
        msg = f"{bare}: git count-objects -v reported no {missing}"
        raise PreparationError(msg)

    present = fields["count"] + fields["in-pack"] - fields.get("prune-packable", 0)
    return reachable, present


def _verify_prepared(bare: Path, base_commit: str) -> None:
    """Re-read the invariants from ``bare`` and raise if any one is broken.

    This is the fail-closed backstop.  The tests only ever see a two-file
    fixture, while a full run prepares a hundred real repos, so the repo is
    asked to prove its own state rather than assumed to have reached it.

    Every check reads only the finished repo.  Nothing here may depend on state
    captured during the clone, because this same function is what decides
    whether an ALREADY existing repo can be reused, and on that path there was
    no clone.  A check that needs clone-time state silently degrades to no check
    at all in exactly the case that matters.

    Args:
        bare: Path to the prepared bare repo.
        base_commit: The sha ``refs/heads/main`` must point at.

    Raises:
        PreparationError: On any broken invariant.
    """
    if _git("rev-parse", "--is-bare-repository", cwd=bare) != "true":
        msg = f"{bare} is not a bare repository"
        raise PreparationError(msg)

    alternates = bare / "objects" / "info" / "alternates"
    if alternates.exists():
        borrowed = alternates.read_text(encoding="utf-8").split()
        msg = f"{bare} borrows objects from {borrowed}, so it is not self-contained"
        raise PreparationError(msg)

    refs = sorted(_ref_targets(bare))
    if refs != [MAIN_REF]:
        msg = f"{bare} has refs other than {MAIN_REF}: {refs}"
        raise PreparationError(msg)

    head = _git("symbolic-ref", "HEAD", cwd=bare)
    if head != MAIN_REF:
        msg = f"{bare} HEAD is {head}, not {MAIN_REF}"
        raise PreparationError(msg)

    actual = _git("rev-parse", MAIN_REF, cwd=bare)
    if actual != base_commit:
        msg = f"{bare} main is {actual}, not the base commit {base_commit}"
        raise PreparationError(msg)

    remotes = _git("remote", cwd=bare)
    if remotes:
        msg = f"{bare} still has remotes configured: {remotes.split()}"
        raise PreparationError(msg)

    # Last because it is the expensive one, and because every check above gives
    # a sharper message for the case it covers.
    reachable, present = _object_counts(bare)
    if present != reachable:
        msg = (
            f"{bare} holds {present} objects but only {reachable} are reachable, "
            "so its object store is not exactly its reachable closure: more than "
            "reachable means it still answers for the gold patch by sha, fewer "
            "means the clone is incomplete"
        )
        raise PreparationError(msg)


def _already_prepared(bare: Path, base_commit: str) -> bool:
    """Return whether ``bare`` is an existing repo that already holds up.

    Existing is not the same as correct: a repo left by an older or interrupted
    run has to be rebuilt, so the guard runs the full verification rather than
    trusting the path.
    """
    if not bare.exists():
        return False
    try:
        _verify_prepared(bare, base_commit)
    except (PreparationError, subprocess.CalledProcessError, OSError) as exc:
        logger.info("rebuilding %s: %s", bare, exc)
        return False
    return True


def _discard_failed(bare: Path) -> None:
    """Delete a repo whose preparation did not finish.

    A half-prepared repo left on disk is the one the NEXT run inspects, and the
    operator's natural response to a failed run is to run the same command
    again.  Removing it makes that re-run rebuild from the upstream, which is
    the only outcome that cannot be wrong.  A failure to remove it is logged and
    swallowed: it must not replace whatever exception is already on its way out,
    and the verifier will refuse the leftovers on the next pass anyway.
    """
    if not bare.exists():
        return
    try:
        _force_rmtree(bare)
    except OSError:
        logger.exception("could not remove the half-prepared repo at %s", bare)


def prepare_instance(spec: InstanceSpec, root: Path) -> Path:
    """Materialize ``spec`` as a bare repo whose ``main`` is the buggy base.

    Either this returns a repo that holds every invariant, or it leaves nothing
    at ``<root>/<instance_id>.git`` at all.  There is no third outcome, because
    the third outcome is what the next run would trust.

    Args:
        spec: The instance to prepare.
        root: Directory the bare repo is written into.

    Returns:
        Path to the prepared bare repo, ``<root>/<instance_id>.git``.

    Raises:
        ValueError: If the instance id could escape ``root``.
        PreparationError: If the base commit is absent from the upstream, or the
            finished repo fails any invariant.
    """
    bare = _bare_path(spec.instance_id, root)

    if _already_prepared(bare, spec.base_commit):
        logger.info("instance %s already prepared at %s", spec.instance_id, bare)
        return bare
    if bare.exists():
        _force_rmtree(bare)

    root.mkdir(parents=True, exist_ok=True)

    # Nothing below may leave a directory behind on the way out.  A repo that
    # got as far as the ref sweep but not the prune looks clean to every cheap
    # signal while still holding the answer, and the next run would return it.
    # The flag rather than a bare ``except`` so an interrupt cleans up too.
    finished = False
    try:
        _git("clone", "--bare", spec.upstream, str(bare))

        if _git_exit("cat-file", "-e", f"{spec.base_commit}^{{commit}}", cwd=bare) != 0:
            msg = f"base commit {spec.base_commit} is not in {spec.upstream}"
            raise PreparationError(msg)

        _git("update-ref", MAIN_REF, spec.base_commit, cwd=bare)
        _git("symbolic-ref", "HEAD", MAIN_REF, cwd=bare)
        _sweep_refs(bare)
        for remote in _git("remote", cwd=bare).split():
            _git("remote", "remove", remote, cwd=bare)
        _drop_pack_keeps(bare)

        _git("reflog", "expire", "--expire=now", "--all", cwd=bare)
        _git("gc", "--prune=now", "--quiet", cwd=bare)

        _verify_prepared(bare, spec.base_commit)
        finished = True
    finally:
        if not finished:
            _discard_failed(bare)

    logger.info("prepared %s at %s", spec.instance_id, bare)
    return bare


def tracked_file_count(bare: Path, ref: str = "main") -> int:
    """Return the number of tracked files at ``ref``, for repo size strata."""
    listing = _git("ls-tree", "-r", "--name-only", ref, cwd=bare)
    return len([line for line in listing.splitlines() if line])


def main(argv: list[str] | None = None) -> int:
    """Prepare every instance named in a committed sample file."""
    parser = argparse.ArgumentParser(description="Prepare bench instances")
    parser.add_argument("--sample", required=True, help="path to a sample JSON file")
    parser.add_argument(
        "--root",
        default=os.environ.get("PRAXIS_BENCH_ROOT", str(DEFAULT_BENCH_ROOT / "repos")),
        help="where to write prepared bare repos",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sample = json.loads(Path(args.sample).read_text(encoding="utf-8"))
    root = Path(args.root)
    instances = sample["instances"]
    for entry in instances:
        prepare_instance(
            InstanceSpec(
                instance_id=entry["instance_id"],
                upstream=entry["upstream"],
                base_commit=entry["base_commit"],
            ),
            root,
        )
    logger.info("prepared %d instances into %s", len(instances), root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
