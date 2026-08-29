"""``insta-boards list boards`` — list every Saved Collection as JSONL."""

from __future__ import annotations

import argparse
from typing import Any

from instagrapi.types import Collection

from src.client import dump_jsonl, init_client
from src.pagination import iter_collections


PROG = "list boards"


def collection_to_json(c: Collection) -> dict:
    return {
        "collection_id": str(c.id),
        "name": c.name,
        "type": c.type,
        "media_count": c.media_count,
    }


def add_arguments(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``boards`` sub-subcommand under the ``list`` verb."""
    p = sub.add_parser(
        "boards",
        prog=f"insta-boards {PROG}",
        description=(
            "Print a JSONL description of every Saved Collection on the "
            "account. By default walks the ENTIRE Instagram response via "
            "cursor pagination; --limit caps the number of output lines."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of output lines. By default no limit — all collections are walked.",
    )
    p.set_defaults(_handler=lambda args: run(args, p))


def run(args: Any, _parser: argparse.ArgumentParser) -> int:
    from src.cli._common import log

    cl, _cfg = init_client()

    emitted = 0
    for c in iter_collections(cl):
        dump_jsonl([collection_to_json(c)])
        emitted += 1
        if args.limit is not None and emitted >= int(args.limit):
            break

    # Stderr summary — does not pollute the JSONL on stdout.
    log(PROG, f"processed={emitted}")
    return 0
