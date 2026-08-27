from __future__ import annotations

import argparse
import sys

from instagrapi.types import Collection

from src.client import dump_jsonl, init_client
from src.pagination import iter_collections


def collection_to_json(c: Collection) -> dict:
    return {
        "collection_id": str(c.id),
        "name": c.name,
        "type": c.type,
        "media_count": c.media_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="list-collections",
        description=(
            "Print a JSONL description of every Saved Collection on the "
            "account. By default walks the ENTIRE Instagram response via "
            "cursor pagination; --limit caps the number of output lines."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of output lines. By default no limit — all collections are walked.",
    )
    args = parser.parse_args()

    cl, _cfg = init_client()

    # Full pagination over all collections via ``iter_collections`` (driven
    # by Instagram's `more_available` / `next_max_id` cursor). We do not
    # skip anything — the user can pick the IDs they need afterwards.
    emitted = 0
    for c in iter_collections(cl):
        dump_jsonl([collection_to_json(c)])
        emitted += 1
        if args.limit is not None and emitted >= int(args.limit):
            break

    # To stderr — a short report of how many collections we walked. Useful
    # for pipes and tee-logging, does not pollute the JSONL on stdout.
    sys.stderr.write(f"[list-collections] processed={emitted}\n")
    sys.stderr.flush()
    return 0
