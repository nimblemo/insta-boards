"""Shared helpers for every ``insta-boards`` subcommand.

Holds:

* Collection-ID parsing utilities (CSV + file reader) — used by ``sync``
  and shared via this module to avoid duplicating the file-format contract.
* A single ``make_pacer_and_pool`` factory that interprets ``--no-humanize``
  and ``--concurrency`` exactly the same way across subcommands.
* A tiny ``log`` helper so every subcommand can emit stderr lines with the
  same ``[<command>]`` prefix without re-implementing the format.

Argument groups are attached directly inside ``app.py`` — see the
``add_*_args`` helpers below for the small building blocks.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from src.instagram_sync import get_pacer, get_pool
from src.paths import resolve_from_repo_root


# Name of the default file that is consulted when ``sync`` is invoked
# without ``--collection`` / ``--collection-file``. Kept here so the
# format contract is documented in one place.
DEFAULT_COLLECTION_LIST_FILENAME = "sync-collection-list.txt"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log(prog: str, message: str) -> None:
    """Write ``[<prog>] <message>`` to stderr and flush immediately."""
    sys.stderr.write(f"[{prog}] {message}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Collection IDs: parsing, file reader, deduplication
# ---------------------------------------------------------------------------


def split_collection_ids(raw: str) -> list[str]:
    """Split ``"111,222,333"`` into ``["111", "222", "333"]`` (with trim)."""
    return [s.strip() for s in raw.split(",") if s.strip()]


def read_collection_file(path: Path) -> list[str]:
    """Read a file with a list of collection IDs.

    File format (one ID per line OR comma-separated; ``#`` is a comment)::

        # favorites
        18427410172124759
        12345678901234567, 18143529535276037
    """
    if not path.exists():
        raise FileNotFoundError(f"Collection list file not found: {path}")
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        ids.extend(split_collection_ids(stripped))
    return ids


def collect_collection_ids(
    cli_values: Sequence[str] | None, file_path: str | None
) -> list[str] | None:
    """Merge ``--collection`` (CSV supported) and ``--collection-file``.

    Returns ``None`` if both sources are empty (= "apply no filter").
    Otherwise a deduplicated list with order preserved.
    """
    raw: list[str] = []
    if cli_values:
        for value in cli_values:
            raw.extend(split_collection_ids(value))
    if file_path:
        resolved = Path(file_path).expanduser().resolve()
        raw.extend(read_collection_file(resolved))
    if not raw:
        return None
    seen: set[str] = set()
    result: list[str] = []
    for cid in raw:
        if cid and cid not in seen:
            seen.add(cid)
            result.append(cid)
    return result


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def default_state_path() -> Path:
    """Resolve the JSON state file path from env or repo default."""
    env = os.getenv("IG_STATE_PATH", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return resolve_from_repo_root("data", "state", "instagram_sync.json")


# ---------------------------------------------------------------------------
# Pacer / pool — used by ``sync`` and ``download`` (the two commands that
# actually move bytes). Centralised so the semantics of ``--no-humanize``
# and ``--concurrency`` cannot drift between the two.
# ---------------------------------------------------------------------------


def make_pacer_and_pool(
    prog: str, *, no_humanize: bool, concurrency: int | None
) -> tuple[Any, Any]:
    """Build the humanizer pacer and the parallel download pool.

    The pacer is the process-wide singleton from ``src.instagram_sync``;
    ``--no-humanize`` toggles it off on the fly (handy for tests / CI).
    The pool is the singleton too, unless ``--concurrency`` is explicitly
    different from the pool's current ``max_workers`` — in which case we
    build a fresh pool with the requested size.
    """
    pacer = get_pacer()
    if no_humanize:
        pacer.set_enabled(False)

    pool = get_pool(pacer)
    if concurrency is not None and concurrency != pool.max_workers:
        from src.parallel import DownloadPool  # local import — keeps top clean

        pool = DownloadPool(pacer=pacer, max_workers=max(1, int(concurrency)))

    log(
        prog,
        f"pacer={'on' if pacer.enabled else 'off'} "
        f"base_delay={pacer.config.base_delay}s "
        f"concurrency={pool.max_workers} "
        f"ua_rotate={pacer.config.rotate_user_agent}",
    )
    return pacer, pool


# ---------------------------------------------------------------------------
# Argparse building blocks — used by ``app.py`` to keep every subparser
# consistent in naming, defaults and help text.
# ---------------------------------------------------------------------------


def add_workdir_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workdir",
        dest="workdir",
        default=None,
        type=str,
        help=(
            "Directory to chdir into before running. Useful when the CLI is "
            "launched from cron / another environment. By default uses the "
            "repository root (parent of src/)."
        ),
    )


def add_concurrency_args(parser: argparse.ArgumentParser) -> None:
    """Flags that only matter when bytes are actually being downloaded."""
    parser.add_argument(
        "--concurrency",
        dest="concurrency",
        default=None,
        type=int,
        help=(
            "How many media files inside a single item to download in parallel. "
            "By default taken from IG_DOWNLOAD_CONCURRENCY (1 = sequential). "
            "Each download still goes through human-like throttling."
        ),
    )
    parser.add_argument(
        "--no-humanize",
        dest="no_humanize",
        action="store_true",
        help=(
            "Disable human-like throttling: pauses become exactly "
            "IG_DOWNLOAD_DELAY seconds, without the log-normal distribution, "
            "micro- and session breaks. Useful for CI/tests where timing "
            "reproducibility matters."
        ),
    )


def add_pagination_args(parser: argparse.ArgumentParser) -> None:
    """Cursor + limit — shared between ``list items`` and ``download``."""
    parser.add_argument(
        "--limit",
        dest="limit",
        default=None,
        type=int,
        help=(
            "Maximum number of items in this run. By default no limit — "
            "all items in the collection are walked via cursor pagination."
        ),
    )
    parser.add_argument(
        "--max-id",
        dest="max_id",
        default="",
        type=str,
        help=(
            "Opaque Instagram cursor at which to resume walking (see the "
            "status of a previous run). Empty string = start from the beginning."
        ),
    )
    parser.add_argument(
        "--output-cursor",
        dest="output_cursor",
        default=None,
        type=str,
        help=(
            "Path to a file where a JSON status (last_max_id / done) will "
            "be written on completion (handy for a follow-up --max-id)."
        ),
    )
