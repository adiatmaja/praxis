"""Fill a drawn sample's ``problem_statement`` from the upstream corpus.

The instance pool is built from gold-patch metadata plus a tracked-file count,
so it carries no issue text.  The worker-facing prompt IS the issue text, so a
drawn sample has to be enriched before it can be run.  That step used to be an
uncommitted one-off, which meant the committed sample could not be reproduced
and a re-draw silently produced an unrunnable file.  It is code now.

Everything here fails loudly.  A blank statement written into a sample is
invisible: the file parses, the schema checks pass, the entry count is right,
and the failure only surfaces once ``bench.runner`` is already spawning
containers.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol


logger = logging.getLogger(__name__)

# The public dataset viewer serves the corpus over plain HTTP with no auth and
# no extra dependency, which matters because bench/ is dev-only and should not
# drag `datasets` and its transitive tree into the environment.
ROWS_URL = "https://datasets-server.huggingface.co/rows"
DATASET = "princeton-nlp/SWE-bench_Lite"
SPLIT = "test"
PAGE_SIZE = 100


class MissingInstanceError(Exception):
    """An instance in the sample has no counterpart in the corpus."""


class BlankProblemStatementError(Exception):
    """The corpus carries an empty issue text for an instance."""


class _PageFetcher(Protocol):
    def __call__(
        self, dataset: str, split: str, offset: int, length: int
    ) -> list[dict[str, Any]]: ...


def _http_fetch_page(
    dataset: str, split: str, offset: int, length: int
) -> list[dict[str, Any]]:
    """Read one page of rows from the public dataset viewer.

    Args:
        dataset: Hugging Face dataset id.
        split: Split name.
        offset: Zero-based row offset.
        length: Maximum rows to return.

    Returns:
        The page's row dicts, unwrapped from the viewer's envelope.
    """
    query = urllib.parse.urlencode(
        {
            "dataset": dataset,
            "config": "default",
            "split": split,
            "offset": offset,
            "length": length,
        }
    )
    with urllib.request.urlopen(f"{ROWS_URL}?{query}", timeout=60) as response:  # noqa: S310  # nosec B310 - fixed https host
        payload = json.load(response)
    return [row["row"] for row in payload.get("rows", [])]


def fetch_problem_statements(
    instance_ids: list[str],
    *,
    fetch_page: _PageFetcher | Callable[..., list[dict[str, Any]]] = _http_fetch_page,
    dataset: str = DATASET,
    split: str = SPLIT,
    page_size: int = PAGE_SIZE,
) -> dict[str, str]:
    """Return ``{instance_id: problem_statement}`` for every requested id.

    Args:
        instance_ids: Ids to look up.
        fetch_page: Page reader, injected so the tests need no network.
        dataset: Corpus to read.
        split: Split to read.
        page_size: Rows per request.

    Returns:
        One entry per requested id.

    Raises:
        MissingInstanceError: If the corpus is exhausted with ids outstanding.
        BlankProblemStatementError: If the corpus carries an empty statement.
    """
    wanted = set(instance_ids)
    found: dict[str, str] = {}
    offset = 0
    while wanted:
        rows = fetch_page(dataset, split, offset, page_size)
        if not rows:
            break
        for row in rows:
            instance_id = row.get("instance_id")
            if instance_id not in wanted:
                continue
            statement = str(row.get("problem_statement") or "")
            if not statement.strip():
                message = f"corpus carries a blank problem_statement for {instance_id}"
                raise BlankProblemStatementError(message)
            found[instance_id] = statement
            wanted.discard(instance_id)
        offset += len(rows)
    if wanted:
        message = f"not found in {dataset}/{split}: {sorted(wanted)}"
        raise MissingInstanceError(message)
    return found


def enrich_sample(path: Path, statements: dict[str, str]) -> int:
    """Write ``problem_statement`` onto every entry of a drawn sample.

    The file is rewritten only after every entry has been resolved, so a
    missing id leaves the original untouched rather than half-enriched.

    Args:
        path: The committed sample file.
        statements: ``{instance_id: problem_statement}``.

    Returns:
        How many entries were written.

    Raises:
        MissingInstanceError: If an entry has no statement in ``statements``.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    instances = data["instances"]
    absent = [
        entry["instance_id"]
        for entry in instances
        if not str(statements.get(entry["instance_id"], "")).strip()
    ]
    if absent:
        message = f"no problem_statement supplied for: {sorted(absent)}"
        raise MissingInstanceError(message)
    for entry in instances:
        entry["problem_statement"] = statements[entry["instance_id"]]
    path.write_text(
        json.dumps(data, indent=2) + "\n",
        encoding="utf-8",
        # Without this, Windows text mode translates to CRLF and the COMMITTED
        # sample lands with the wrong line endings in an LF repo.
        newline="\n",
    )
    return len(instances)


def main(argv: list[str] | None = None) -> int:
    """CLI: enrich a drawn sample in place from the upstream corpus."""
    parser = argparse.ArgumentParser(description="Enrich a bench sample")
    parser.add_argument("--sample", required=True, help="drawn sample JSON")
    parser.add_argument("--dataset", default=DATASET)
    parser.add_argument("--split", default=SPLIT)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    path = Path(args.sample)
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = [entry["instance_id"] for entry in data["instances"]]
    statements = fetch_problem_statements(ids, dataset=args.dataset, split=args.split)
    written = enrich_sample(path, statements)
    logger.info("enriched %d instances in %s", written, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
