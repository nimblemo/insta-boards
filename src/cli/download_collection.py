from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from instagrapi.types import Media

from src.client import init_client
from src.instagram_sync import (
    collection_raw_dir,
    get_pacer,
    get_pool,
    item_metadata_path,
    load_state,
    write_item_metadata,
)
from src.naming import resolve_collection_name, slugify_collection_name
from src.pagination import CollectionMediasPager, emit_status
from src.paths import resolve_from_repo_root


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return resolve_from_repo_root()


def _default_state_path() -> Path:
    env = os.getenv("IG_STATE_PATH", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return resolve_from_repo_root("data", "state", "instagram_sync.json")


def collection_index_path(slug: str) -> Path:
    return collection_raw_dir(slug) / "metadata.json"


def load_existing_index(slug: str) -> tuple[list[dict[str, Any]], str | None]:
    """Load existing collection index if present; return (items, fetched_at)."""
    path = collection_index_path(slug)
    if not path.exists():
        return [], None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return [], None
    items = payload.get("items") or []
    if not isinstance(items, list):
        items = []
    fetched_at = payload.get("fetched_at")
    return items, fetched_at


def write_collection_index(
    slug: str,
    fetched_at: str,
    items: list[dict[str, Any]],
) -> None:
    raw_dir = collection_raw_dir(slug)
    raw_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "instagram",
        "fetched_at": fetched_at,
        "items": items,
    }
    with open(raw_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def index_entry_from_item(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": metadata["item_id"],
        "source_url": metadata["source_url"],
        "taken_at": metadata["taken_at"],
    }


# ---------------------------------------------------------------------------
# Resolving the slug name of a collection
# ---------------------------------------------------------------------------


def resolve_slug(cl, collection_id: str, explicit_name: str | None) -> tuple[str, str]:
    """Return ``(slug, source)`` for ``collection_id``.

    Priority:
      1. ``--name`` from the CLI (if passed) — transliterated;
      2. ``name`` from ``data/state/instagram_sync.json`` (if a record exists);
      3. Name from ``iter_collections(...)`` (best-effort) on Instagram;
      4. ``cid`` as a fallback.
    """
    if explicit_name:
        return slugify_collection_name(explicit_name, fallback=collection_id), "cli"

    try:
        state = load_state(_default_state_path())
        col_state = state.collections.get(collection_id)
        if col_state and col_state.name:
            return (
                slugify_collection_name(col_state.name, fallback=collection_id)
                if not col_state.slug
                else col_state.slug,
                "state",
            )
    except Exception:
        pass

    fetched = resolve_collection_name(cl, collection_id)
    if fetched:
        return slugify_collection_name(fetched, fallback=collection_id), "api"

    return slugify_collection_name(collection_id, fallback=collection_id), "fallback"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="download-collection",
        description=(
            "Download media from an Instagram collection into the raw-project "
            "layout. By default walks the WHOLE collection via cursor "
            "pagination; supports --limit, --max-id / --output-cursor and "
            "--resume for incremental downloads. For a full sync of ALL "
            "collections, use ``sync-instagram``."
        ),
    )
    parser.add_argument(
        "--collection",
        required=True,
        type=str,
        help="Collection ID (numeric) or a string sentinel (e.g. ALL_MEDIA_AUTO_COLLECTION).",
    )
    parser.add_argument(
        "--name",
        dest="name",
        default=None,
        type=str,
        help=(
            "Explicit collection name (e.g. \"Furniture\"). If not set, the "
            "name is pulled from the state file, then via the Instagram "
            "API. The name is transliterated to an ASCII slug and used as "
            "the on-disk directory (data/raw/<slug>/)."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Maximum number of items to download in this run. By default no "
            "limit — all items in the collection are downloaded via cursor pagination."
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
        help="Path to a file where a JSON status (last_max_id / done) will be written on completion.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Skip items that already have ``data/raw/<slug>/<pk>.json`` on "
            "disk. Useful for incremental downloads of large collections."
        ),
    )
    parser.add_argument(
        "--workdir",
        dest="workdir",
        default=None,
        type=str,
        help=(
            "Directory to chdir into before running. Useful when the "
            "script is launched from cron / another environment. By "
            "default uses the repository root (parent of src/)."
        ),
    )
    parser.add_argument(
        "--concurrency",
        dest="concurrency",
        default=None,
        type=int,
        help=(
            "How many media files inside a single item to download in "
            "parallel. By default taken from IG_DOWNLOAD_CONCURRENCY "
            "(1 = sequential)."
        ),
    )
    parser.add_argument(
        "--no-humanize",
        dest="no_humanize",
        action="store_true",
        help="Disable human-like throttling (flat IG_DOWNLOAD_DELAY pauses).",
    )
    args = parser.parse_args()

    if args.workdir:
        os.chdir(Path(args.workdir).expanduser().resolve())

    cl, _cfg = init_client()
    collection_id = args.collection.strip()
    collection_key: Any = int(collection_id) if collection_id.isdigit() else collection_id

    slug, source = resolve_slug(cl, collection_id, args.name)
    sys.stderr.write(
        f"[download-collection] collection={collection_id} slug={slug!r} (resolved from {source})\n"
    )
    sys.stderr.flush()

    # Human-like throttling — singleton. --no-humanize turns it off on
    # the fly (for CI/tests where timing reproducibility matters).
    pacer = get_pacer()
    if args.no_humanize:
        pacer.set_enabled(False)
    # If --concurrency is passed — override the env value via a direct
    # DownloadPool construction (lazy import to keep the top imports clean).
    pool = get_pool(pacer)
    if args.concurrency is not None and args.concurrency != pool.max_workers:
        from src.parallel import DownloadPool  # noqa: WPS433

        pool = DownloadPool(pacer=pacer, max_workers=max(1, int(args.concurrency)))

    # Load the existing index if it is there and --resume is set.
    index, existing_fetched_at = load_existing_index(slug) if args.resume else ([], None)
    seen_item_ids: set[str] = {
        it["item_id"] for it in index if isinstance(it, dict) and "item_id" in it
    }

    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _ = existing_fetched_at  # noqa: F841 — kept for future extension.

    pager = CollectionMediasPager(cl, collection_key, start_max_id=args.max_id, pacer=pacer)
    processed = 0
    skipped = 0
    try:
        for m in pager:
            if args.limit is not None and processed >= int(args.limit):
                break

            item_id = str(m.pk)
            item_already = (
                args.resume
                and item_id in seen_item_ids
                and item_metadata_path(slug, m.pk).exists()
            )
            if item_already:
                skipped += 1
                continue

            md = write_item_metadata(collection_id, slug, m, fetched_at, pool=pool)
            index.append(index_entry_from_item(md))
            seen_item_ids.add(item_id)
            processed += 1

            write_collection_index(slug, fetched_at, index)
    finally:
        write_collection_index(slug, fetched_at, index)
        emit_status(
            last_max_id=pager.last_max_id,
            consumed=pager.consumed,
            done=pager.done,
            output_path=args.output_cursor,
        )
        sys.stderr.write(
            f"[download-collection] collection={collection_id} slug={slug!r} "
            f"processed={processed} skipped={skipped} "
            f"cursor={'<done>' if pager.done else pager.last_max_id!r}\n"
        )
        sys.stderr.flush()
    return 0
