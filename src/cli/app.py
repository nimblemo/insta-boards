"""Single entry point for the ``insta-boards`` CLI.

Top-level dispatch::

    insta-boards sync                    # full state-aware sync
    insta-boards list boards             # JSONL of every collection
    insta-boards list items --collection <id>
    insta-boards download --collection <id>

The actual subcommand bodies live in ``src.cli.commands.*``; this module
only wires up argparse and forwards the parsed namespace to the right
``run(args, parser)`` function.

The shape (verb-first, single binary with subcommands) follows the
kubectl / gh / aws / uv convention so users coming from those tools feel
at home.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from src.cli._common import add_concurrency_args, add_workdir_arg


# Description printed in ``insta-boards --help``.
DESCRIPTION = (
    "Local sync of Instagram Saved Collections into the filesystem. "
    "Pick a subcommand to inspect, download or fully sync your account."
)


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse tree. Split out for unit-testability."""
    parser = argparse.ArgumentParser(
        prog="insta-boards",
        description=DESCRIPTION,
    )

    sub = parser.add_subparsers(
        dest="command",
        metavar="<command>",
        required=True,
        title="commands",
    )

    # ----- sync -----------------------------------------------------------
    sync_p = sub.add_parser(
        "sync",
        help="Full sync of all collections (state-aware, resumable).",
        description=(
            "Full sync of Instagram Saved Collections into a local directory. "
            "Supports incremental download of new items via a JSON state file: "
            "re-runs do not re-download already-synced items, they only pick "
            "up new ones."
        ),
    )
    sync_p.add_argument(
        "--state-path",
        dest="state_path",
        default=None,
        type=str,
        help=(
            "Path to the JSON state file. By default taken from the "
            "IG_STATE_PATH env var, or "
            "<repo>/data/state/instagram_sync.json."
        ),
    )
    sync_p.add_argument(
        "--collection",
        dest="collections",
        action="append",
        default=None,
        type=str,
        help=(
            "Collection ID to sync. Can be passed multiple times "
            "(--collection 111 --collection 222) or comma-separated "
            "(--collection 111,222,333). Merged with --collection-file. "
            "By default uses sync-collection-list.txt from the repo root "
            "if it exists; otherwise syncs every collection on the account."
        ),
    )
    sync_p.add_argument(
        "--collection-file",
        dest="collection_file",
        default=None,
        type=str,
        help=(
            "Path to a file with a list of collection IDs (one per line "
            "or comma-separated; lines starting with # are comments). "
            "Merged with --collection. If not provided, sync-collection-list.txt "
            "from the repo root is used if it exists; otherwise every "
            "collection on the account."
        ),
    )
    sync_p.add_argument(
        "--reset",
        dest="reset_all",
        action="store_true",
        help="Reset cursor/done for ALL collections in state (items are kept).",
    )
    sync_p.add_argument(
        "--reset-collection",
        dest="reset_collections",
        action="append",
        default=None,
        type=str,
        help="Reset cursor/done for the given collection ID only. Can be passed multiple times.",
    )
    sync_p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "Do not contact Instagram and do not write state: only show "
            "the current state and the plan."
        ),
    )
    sync_p.add_argument(
        "--report-json",
        dest="report_json",
        default=None,
        type=str,
        help="Path to write a JSON sync report (in addition to the stderr log).",
    )
    sync_p.add_argument(
        "--print-state",
        dest="print_state",
        action="store_true",
        help="After the sync, print the final state to stderr (for debugging).",
    )
    add_concurrency_args(sync_p)
    add_workdir_arg(sync_p)
    sync_p.set_defaults(_handler=_resolve("src.cli.commands.sync", "run"))

    # ----- list -----------------------------------------------------------
    list_p = sub.add_parser(
        "list",
        help="Inspect collections or items (read-only, emits JSONL).",
        description=(
            "Read-only inspection commands. Output is JSONL on stdout; "
            "progress / summary lines go to stderr."
        ),
    )
    list_sub = list_p.add_subparsers(
        dest="list_target",
        metavar="<target>",
        required=True,
        title="list targets",
    )
    # Defer to per-command modules so each can grow its own flags.
    from src.cli.commands import list_boards, list_items

    list_boards.add_arguments(list_sub)
    list_items.add_arguments(list_sub)

    # ----- download -------------------------------------------------------
    download_p = sub.add_parser(
        "download",
        help="Download a single collection into data/raw/<slug>/.",
        description=(
            "Download media from an Instagram collection into the raw-project "
            "layout. By default walks the WHOLE collection via cursor "
            "pagination; supports --limit, --max-id / --output-cursor and "
            "--resume for incremental downloads. For a full sync of ALL "
            "collections, use ``insta-boards sync``."
        ),
    )
    from src.cli.commands import download as download_cmd

    download_cmd.add_arguments(download_p)

    return parser


def _resolve(dotted: str, attr: str) -> Callable[[argparse.Namespace], int]:
    """Lazy import + attribute lookup — keeps ``--help`` cheap."""
    import importlib

    module = importlib.import_module(dotted)
    return getattr(module, attr)


def main(argv: list[str] | None = None) -> int:
    """Entry point declared in ``pyproject.toml`` under ``[project.scripts]``."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args._handler
    try:
        return handler(args)
    except KeyboardInterrupt:
        sys.stderr.write("\n[insta-boards] interrupted by user\n")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
