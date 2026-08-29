"""``insta-boards download <id>`` — download a single collection."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from instagrapi.types import Media

from src.cli._common import (
    add_concurrency_args,
    add_pagination_args,
    add_workdir_arg,
    default_state_path,
    log,
    make_pacer_and_pool,
)
from src.client import init_client
from src.instagram_sync import (
    collection_raw_dir,
    item_metadata_path,
    load_state,
    write_item_metadata,
)
from src.naming import resolve_collection_name, slugify_collection_name
from src.pagination import CollectionMediasPager, emit_status


PROG = "download"


# ---------------------------------------------------------------------------
# Index helpers (collection-level metadata.json)
# ---------------------------------------------------------------------------


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
# Slug resolution
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
        state = load_state(default_state_path())
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


def add_arguments(parser: argparse.ArgumentParser) -> None:
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
        "--resume",
        action="store_true",
        help=(
            "Skip items that already have ``data/raw/<slug>/<pk>.json`` on "
            "disk. Useful for incremental downloads of large collections."
        ),
    )
    add_pagination_args(parser)
    add_workdir_arg(parser)
    add_concurrency_args(parser)
    parser.set_defaults(_handler=lambda args: run(args, parser))


def run(args: Any, _parser: argparse.ArgumentParser) -> int:
    if args.workdir:
        os.chdir(Path(args.workdir).expanduser().resolve())

    cl, _cfg = init_client()
    collection_id = args.collection.strip()
    collection_key: Any = int(collection_id) if collection_id.isdigit() else collection_id

    slug, source = resolve_slug(cl, collection_id, args.name)
    log(
        PROG,
        f"collection={collection_id} slug={slug!r} (resolved from {source})",
    )

    pacer, pool = make_pacer_and_pool(
        PROG, no_humanize=args.no_humanize, concurrency=args.concurrency
    )

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
        log(
            PROG,
            f"collection={collection_id} slug={slug!r} "
            f"processed={processed} skipped={skipped} "
            f"cursor={'<done>' if pager.done else pager.last_max_id!r}",
        )
    return 0
