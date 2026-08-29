"""``insta-boards list items`` — JSONL of items in a single collection."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any

from instagrapi.types import Media, Resource

from src.cli._common import add_pagination_args, log
from src.client import dump_jsonl, init_client
from src.pagination import CollectionMediasPager, emit_status


PROG = "list items"


def as_utc_iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def media_urls(media: Media) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    resources: list[Resource] | None = getattr(media, "resources", None)
    if resources:
        for idx, r in enumerate(resources):
            url = r.video_url or r.thumbnail_url
            if not url:
                continue
            out.append(
                {
                    "type": "video" if r.video_url else "image",
                    "url": str(url),
                    "index": idx,
                }
            )
        return out

    url = media.video_url or media.thumbnail_url
    if url:
        out.append(
            {
                "type": "video" if media.video_url else "image",
                "url": str(url),
                "index": 0,
            }
        )
    return out


def normalize_media(media: Media, collection_id: str) -> dict[str, Any]:
    taken_at = getattr(media, "taken_at", None)
    code = getattr(media, "code", None)

    source_url = f"https://www.instagram.com/p/{code}/" if code else None
    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "source": "instagram",
        "collection_id": str(collection_id),
        "item_id": str(media.pk),
        "source_url": source_url,
        "taken_at": int(taken_at.timestamp()) if taken_at else None,
        "taken_at_iso": as_utc_iso(taken_at),
        "fetched_at": fetched_at,
        "media": media_urls(media),
    }


def add_arguments(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "items",
        prog=f"insta-boards {PROG}",
        description=(
            "Print a JSONL description of every item in an Instagram "
            "collection. By default walks the whole collection via cursor "
            "pagination; --limit and explicit resume via --max-id / "
            "--output-cursor are supported."
        ),
    )
    p.add_argument(
        "--collection",
        required=True,
        type=str,
        help="Collection ID (numeric) or a string sentinel (e.g. ALL_MEDIA_AUTO_COLLECTION).",
    )
    add_pagination_args(p)
    p.set_defaults(_handler=lambda args: run(args, p))


def run(args: Any, _parser: argparse.ArgumentParser) -> int:
    cl, _cfg = init_client()
    collection_id = args.collection.strip()
    collection_key: Any = int(collection_id) if collection_id.isdigit() else collection_id

    pager = CollectionMediasPager(cl, collection_key, start_max_id=args.max_id)
    emitted = 0
    try:
        for m in pager:
            if args.limit is not None and emitted >= int(args.limit):
                # Hit the limit, but the cursor is already stored in
                # pager.last_max_id (cursor of the last fully-read chunk).
                break
            dump_jsonl([normalize_media(m, collection_id)])
            emitted += 1
    finally:
        emit_status(
            last_max_id=pager.last_max_id,
            consumed=pager.consumed,
            done=pager.done,
            output_path=args.output_cursor,
        )
        log(
            PROG,
            f"collection={collection_id} processed={emitted} "
            f"cursor={'<done>' if pager.done else pager.last_max_id!r}",
        )
    return 0
