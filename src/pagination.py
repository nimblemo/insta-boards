from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterator

from instagrapi import Client
from instagrapi.extractors import extract_collection
from instagrapi.types import Collection, Media

from .humanizer import SessionPacer


def emit_status(
    *,
    last_max_id: str,
    consumed: int,
    done: bool,
    output_path: str | Path | None = None,
    stream: Any = None,
) -> None:
    """Write a small JSON status line describing where a pager stopped.

    Always emitted to stderr so it never pollutes JSONL on stdout. If
    ``output_path`` is provided, the same payload is also written to disk
    (as a single JSON object), making it easy to ``$(cat state.json)`` into
    a subsequent ``--max-id`` argument.
    """
    payload = {
        "consumed": consumed,
        "last_max_id": last_max_id,
        "done": bool(done),
    }
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    out = stream if stream is not None else sys.stderr
    try:
        if hasattr(out, "reconfigure"):
            out.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    except Exception:
        pass
    print(line, file=out, flush=True)

    if output_path:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(line + "\n", encoding="utf-8")


class CollectionMediasPager:
    """Iterator over medias in a single Instagram collection.

    Wraps instagrapi's `collection_medias_v1_chunk` so we can paginate through
    large collections lazily and resume from an explicit cursor.

    Parameters
    ----------
    client:
        An authenticated `instagrapi.Client`.
    collection_pk:
        Numeric id of the collection, or one of the string sentinels
        (`ALL_MEDIA_AUTO_COLLECTION`, `liked`, `...`).
    start_max_id:
        Opaque cursor returned by a previous run (via `last_max_id`). Pass it
        to resume iteration from the next item.

    Attributes (read after iteration)
    ---------------------------------
    last_max_id:
        The cursor Instagram gave for the last chunk we consumed. Empty
        string means the collection was fully drained.
    done:
        ``True`` once no more chunks are available.
    consumed:
        Total number of medias yielded.
    """

    def __init__(
        self,
        client: Client,
        collection_pk: Any,
        start_max_id: str = "",
        pacer: SessionPacer | None = None,
    ) -> None:
        self._client = client
        self._collection_pk = collection_pk
        self._next_max_id: str = start_max_id or ""
        self._chunk: list[Media] = []
        self._idx = 0
        self.last_max_id: str = start_max_id or ""
        self.done: bool = False
        self.consumed: int = 0
        # Human-like throttling between chunk requests (NOT between items
        # inside a chunk). If ``pacer`` is not provided, requests go
        # through with no extra pauses (retry/throttle still live on the
        # ``client`` side).
        self._pacer = pacer

    @property
    def collection_pk(self) -> Any:
        return self._collection_pk

    def __iter__(self) -> Iterator[Media]:
        return self

    def __next__(self) -> Media:
        if self._idx >= len(self._chunk):
            if self.done:
                raise StopIteration
            # Human-like pause BEFORE each chunk request. For carousels
            # this will not fire (the chunk is already in memory), but
            # between chunks — the main network call — this is what we
            # need to throttle.
            if self._pacer is not None:
                self._pacer.wait()
            try:
                items, next_max_id = self._client.collection_medias_v1_chunk(
                    self._collection_pk, max_id=self._next_max_id
                )
            except Exception:
                # Surface partial progress so callers can resume.
                self.done = True
                self.last_max_id = self._next_max_id
                raise

            if not items:
                self.done = True
                self.last_max_id = ""
                raise StopIteration

            self._chunk = items
            self._idx = 0
            self._next_max_id = next_max_id or ""
            self.last_max_id = self._next_max_id
            if not self._next_max_id:
                self.done = True

        item = self._chunk[self._idx]
        self._idx += 1
        self.consumed += 1
        return item


def iter_collections(
    client: Client,
    pacer: SessionPacer | None = None,
) -> Iterator[Collection]:
    """Iterate over ALL collections of the logged-in account.

    Uses Instagram's `more_available` / `next_max_id` cursor so we never
    silently truncate results — the iterator always drains the full list of
    collections (across `ALL_MEDIA_AUTO_COLLECTION`, `PRODUCT_AUTO_COLLECTION`
    and `MEDIA` types, matching instagrapi's own `Client.collections()`).

    Parameters
    ----------
    pacer:
        Optional ``SessionPacer`` for human-like throttling between calls
        to ``collections/list/``. If not provided, no extra pauses are
        inserted.
    """
    next_max_id = ""
    collection_types = '["ALL_MEDIA_AUTO_COLLECTION","PRODUCT_AUTO_COLLECTION","MEDIA"]'
    while True:
        if pacer is not None:
            pacer.wait()
        result = client.private_request(
            "collections/list/",
            params={"collection_types": collection_types, "max_id": next_max_id},
        )
        items = result.get("items", []) or []
        for raw in items:
            yield extract_collection(raw)
        if not result.get("more_available"):
            return
        next_max_id = result.get("next_max_id", "") or ""
        if not next_max_id:
            return