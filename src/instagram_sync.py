from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from instagrapi import Client
from instagrapi.types import Collection, Media
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .humanizer import HumanizerConfig, SessionPacer
from .naming import slugify_collection_name
from .pagination import CollectionMediasPager, iter_collections
from .parallel import DownloadJob, DownloadPool, ParallelConfig
from .paths import resolve_from_repo_root

STATE_VERSION = 2


# ---------------------------------------------------------------------------
# HTTP session: retries + polite throttling
# ---------------------------------------------------------------------------
#
# During large-collection runs we occasionally hit connection timeouts
# against `scontent-…cdninstagram.com` (see tracebacks in the logs). We
# keep a shared ``requests.Session`` with a ``Retry`` adapter
# (connect/read/status), an extended timeout, and a minimum throttle of
# ``IG_DOWNLOAD_DELAY`` seconds between requests — this dampens "too
# fast" calls and transparently resumes after transient failures.


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


DEFAULT_DOWNLOAD_TIMEOUT: int = _env_int("IG_DOWNLOAD_TIMEOUT", 120)
DEFAULT_DOWNLOAD_DELAY: float = _env_float("IG_DOWNLOAD_DELAY", 1.0)
DEFAULT_DOWNLOAD_RETRIES: int = _env_int("IG_DOWNLOAD_RETRIES", 5)
DEFAULT_DOWNLOAD_BACKOFF: float = _env_float("IG_DOWNLOAD_BACKOFF", 0.5)

_session_lock = threading.Lock()
_shared_session: requests.Session | None = None
_throttle_lock = threading.Lock()
_last_request_at: float = 0.0
_pacer_lock = threading.Lock()
_shared_pacer: SessionPacer | None = None


def _build_session() -> requests.Session:
    retries = Retry(
        total=DEFAULT_DOWNLOAD_RETRIES,
        connect=DEFAULT_DOWNLOAD_RETRIES,
        read=DEFAULT_DOWNLOAD_RETRIES,
        status=DEFAULT_DOWNLOAD_RETRIES,
        backoff_factor=DEFAULT_DOWNLOAD_BACKOFF,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=("HEAD", "GET", "OPTIONS"),
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _get_session() -> requests.Session:
    """Lazily create the shared ``requests.Session`` with a Retry adapter."""
    global _shared_session
    with _session_lock:
        if _shared_session is None:
            _shared_session = _build_session()
        return _shared_session


def _throttle() -> None:
    """Guarantee at least ``DEFAULT_DOWNLOAD_DELAY`` seconds between requests.

    Kept for backwards compatibility (used by ``download_to_file`` as a
    "guaranteed minimum"). The real human-like throttling lives in
    ``SessionPacer`` and is called from ``DownloadPool``/CLI.
    """
    global _last_request_at
    delay = max(0.0, DEFAULT_DOWNLOAD_DELAY)
    if delay <= 0:
        return
    with _throttle_lock:
        now = time.monotonic()
        elapsed = now - _last_request_at
        wait = delay - elapsed
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


# ---------------------------------------------------------------------------
# Pacer / Pool — single points for human-like throttling and parallel
# downloads. Imported by the ``sync`` and ``download`` subcommands.
# ---------------------------------------------------------------------------


def get_pacer() -> SessionPacer:
    """Return the singleton ``SessionPacer`` configured from ``.env``.

    If the process is running in ``dry-run`` or with ``--no-humanize``,
    callers can override via ``set_enabled(False)`` after obtaining it.
    """
    global _shared_pacer
    with _pacer_lock:
        if _shared_pacer is None:
            _shared_pacer = SessionPacer(HumanizerConfig())
        return _shared_pacer


def reset_pacer() -> SessionPacer:
    """Recreate the singleton ``SessionPacer`` (a fresh "session")."""
    global _shared_pacer
    with _pacer_lock:
        _shared_pacer = SessionPacer(HumanizerConfig())
        return _shared_pacer


def get_pool(pacer: SessionPacer | None = None) -> DownloadPool:
    """Return a ``DownloadPool`` with the concurrency limit from ``.env``."""
    return DownloadPool(
        pacer=pacer or get_pacer(),
        max_workers=ParallelConfig().max_workers,
    )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class CollectionState:
    """State of a single collection in the sync-state file."""

    name: str = ""
    slug: str = ""
    type: str = ""
    media_count: int = 0
    cursor: str = ""
    done: bool = False
    last_synced_at: str = ""
    items: dict[str, dict[str, Any]] = field(default_factory=dict)

    def reset(self) -> None:
        """Reset cursor/done (for the --reset option). Items are kept."""
        self.cursor = ""
        self.done = False


@dataclass
class SyncState:
    """Full Instagram sync state."""

    version: int = STATE_VERSION
    last_full_sync_at: str = ""
    last_incremental_sync_at: str = ""
    collections: dict[str, CollectionState] = field(default_factory=dict)

    # --- serialization -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "last_full_sync_at": self.last_full_sync_at,
            "last_incremental_sync_at": self.last_incremental_sync_at,
            "collections": {
                cid: {
                    "name": c.name,
                    "slug": c.slug,
                    "type": c.type,
                    "media_count": c.media_count,
                    "cursor": c.cursor,
                    "done": c.done,
                    "last_synced_at": c.last_synced_at,
                    "items": c.items,
                }
                for cid, c in self.collections.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SyncState":
        if not isinstance(data, dict):
            return cls()
        cols_raw = data.get("collections") or {}
        cols: dict[str, CollectionState] = {}
        if isinstance(cols_raw, dict):
            for cid, cdata in cols_raw.items():
                if not isinstance(cdata, dict):
                    continue
                items_raw = cdata.get("items") or {}
                items = items_raw if isinstance(items_raw, dict) else {}
                cols[str(cid)] = CollectionState(
                    name=str(cdata.get("name", "")),
                    slug=str(cdata.get("slug", "")),
                    type=str(cdata.get("type", "")),
                    media_count=int(cdata.get("media_count") or 0),
                    cursor=str(cdata.get("cursor", "")),
                    done=bool(cdata.get("done", False)),
                    last_synced_at=str(cdata.get("last_synced_at", "")),
                    items=items,
                )
        return cls(
            version=int(data.get("version", STATE_VERSION) or STATE_VERSION),
            last_full_sync_at=str(data.get("last_full_sync_at", "")),
            last_incremental_sync_at=str(data.get("last_incremental_sync_at", "")),
            collections=cols,
        )


def load_state(path: Path) -> SyncState:
    """Read state from a JSON file. Missing/broken file → empty state."""
    if not path.exists():
        return SyncState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SyncState()
    return SyncState.from_dict(data)


def save_state(path: Path, state: SyncState) -> None:
    """Atomically write state to disk (via ``*.tmp`` + ``replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Raw layout helpers (used in sync_collection too)
# ---------------------------------------------------------------------------
#
# Layout of ``data/raw`` (no intermediate "instagram" directory):
#
#   data/raw/<slug>/
#       metadata.json          # collection index (written by download)
#       <pk>.json              # per-item metadata
#       <pk>_<idx>.<ext>       # media file(s) right in the collection dir
#
# ``<slug>`` is a transliterated ASCII collection name (see ``src.naming``),
# not its numeric ``cid``: "Furniture" → ``furniture``,
# "Textures: for home" → ``textures-for-home``. All functions in this
# block take a ready-made slug; slug computation lives in
# ``sync_collection`` (where the source ``Collection`` is available) or
# in the CLI wrappers (``insta-boards download``).


def collection_raw_dir(slug: str) -> Path:
    """Raw-data directory for one collection (``data/raw/<slug>``)."""
    return resolve_from_repo_root("data", "raw", slug)


def item_metadata_path(slug: str, item_pk: int | str) -> Path:
    return collection_raw_dir(slug) / f"{item_pk}.json"


def media_path(slug: str, item_pk: int | str, idx: int, ext: str) -> Path:
    suffix = ext if ext.startswith(".") else f".{ext}"
    return collection_raw_dir(slug) / f"{item_pk}_{idx}{suffix}"


def guess_ext(url: str, fallback: str) -> str:
    p = urlparse(url)
    suffix = Path(p.path).suffix
    return suffix if suffix else fallback


def download_to_file(url: str, target: Path) -> bool:
    """Download ``url`` → ``target`` via the shared session with Retry+throttle.

    Returns ``True`` if the file was already on disk. Transient network
    errors are automatically retried by ``urllib3.util.retry``; a fully
    failed download propagates so the sync loop records the item as
    "errored" and the next run resumes it.
    """
    if target.exists() and target.stat().st_size > 0:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    _throttle()
    with _get_session().get(url, stream=True, timeout=DEFAULT_DOWNLOAD_TIMEOUT) as r:
        r.raise_for_status()
        with open(target, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    return False


def media_entries(media: Media) -> list[dict[str, Any]]:
    """Flatten a ``Media`` (including carousels) into a flat url list."""
    entries: list[dict[str, Any]] = []
    resources = getattr(media, "resources", None)
    if resources:
        for idx, r in enumerate(resources):
            url = r.video_url or r.thumbnail_url
            if not url:
                continue
            entries.append(
                {
                    "type": "video" if r.video_url else "image",
                    "url": str(url),
                    "index": idx,
                }
            )
        return entries

    url = media.video_url or media.thumbnail_url
    if url:
        entries.append(
            {
                "type": "video" if media.video_url else "image",
                "url": str(url),
                "index": 0,
            }
        )
    return entries


def _repo_root() -> Path:
    return resolve_from_repo_root()


def _relpath(p: Path) -> str:
    """Path relative to the repository root (forward-slashes for cross-platform safety)."""
    root = _repo_root()
    try:
        return str(p.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def write_item_metadata(
    collection_id: str,
    collection_slug: str,
    media: Media,
    fetched_at: str,
    *,
    pool: DownloadPool | None = None,
) -> dict[str, Any]:
    """Download media files and write ``<pk>.json`` to ``data/raw/<slug>/``.

    ``collection_id`` is the numeric (or string) Instagram id;
    ``collection_slug`` is the transliterated collection name used as the
    on-disk directory. Both are stored in ``<pk>.json`` — that gives a
    convenient reverse mapping (slug → cid) for later manual inspection.

    If ``pool`` is passed (or ``IG_DOWNLOAD_CONCURRENCY>1`` globally),
    files inside a single item are downloaded in parallel within the
    configured limit.
    """
    raw_dir = collection_raw_dir(collection_slug)
    raw_dir.mkdir(parents=True, exist_ok=True)

    entries = media_entries(media)
    if not entries:
        # An item with no available URLs — write an "empty" metadata.json
        # so that ``state.items[item_id]`` is still updated and we do not
        # loop on it.
        taken_at = getattr(media, "taken_at", None)
        code = getattr(media, "code", None)
        source_url = f"https://www.instagram.com/p/{code}/" if code else None
        metadata: dict[str, Any] = {
            "source": "instagram",
            "collection_id": str(collection_id),
            "collection_slug": collection_slug,
            "item_id": str(media.pk),
            "source_url": source_url,
            "taken_at": int(taken_at.timestamp()) if taken_at else None,
            "fetched_at": fetched_at,
            "media": [],
        }
        md_target = raw_dir / f"{media.pk}.json"
        md_target.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return metadata

    # Build tasks for DownloadPool.
    jobs: list[DownloadJob] = []
    for m in entries:
        url = m["url"]
        ext = guess_ext(url, ".mp4" if m["type"] == "video" else ".jpg")
        filename = f"{media.pk}_{m['index']}{ext if ext.startswith('.') else '.' + ext}"
        target = raw_dir / filename
        jobs.append(DownloadJob(url=url, target=target, label=filename))

    # Decide which path to take: in parallel (pool given or limit > 1)
    # or sequentially (legacy behaviour, so dry-runs/tests do not race).
    use_pool = pool is not None or ParallelConfig().max_workers > 1
    if use_pool:
        download_pool = pool or get_pool()
        results = download_pool.download_many(jobs)
    else:
        from .parallel import DownloadResult  # local import to avoid cycles

        download = _resolve_download_fn()
        results: list[DownloadResult] = []
        for job in jobs:
            if job.target.exists() and job.target.stat().st_size > 0:
                results.append(DownloadResult(job=job, ok=True, cached=True, elapsed=0.0))
                continue
            get_pacer().wait()
            try:
                download(job.url, job.target)
            except Exception as exc:  # noqa: BLE001
                results.append(DownloadResult(job=job, ok=False, error=str(exc)))
            else:
                results.append(DownloadResult(job=job, ok=True, cached=False))

    # Collect the final list of media metadata. If at least one download
    # failed, propagate outward: sync_collection will record state and
    # move on (the next run will resume the failed items).
    downloaded: list[dict[str, Any]] = []
    first_error: str | None = None
    for m, res in zip(entries, results):
        if not res.ok and first_error is None:
            first_error = res.error or "download failed"
        target = res.job.target
        url = m["url"]
        downloaded.append(
            {
                "type": m["type"],
                "url": url,
                "filename": target.name,
                "path": _relpath(target),
            }
        )

    taken_at = getattr(media, "taken_at", None)
    code = getattr(media, "code", None)
    source_url = f"https://www.instagram.com/p/{code}/" if code else None

    metadata = {
        "source": "instagram",
        "collection_id": str(collection_id),
        "collection_slug": collection_slug,
        "item_id": str(media.pk),
        "source_url": source_url,
        "taken_at": int(taken_at.timestamp()) if taken_at else None,
        "fetched_at": fetched_at,
        "media": downloaded,
    }
    md_target = raw_dir / f"{media.pk}.json"
    md_target.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if first_error is not None:
        # Re-raise so sync-collection marks the item as errored and
        # continues. The <pk>.json is already on disk — the next run
        # will not re-download it (see known_items logic).
        raise RuntimeError(first_error)
    return metadata


def _resolve_download_fn() -> Callable[[str, Path], bool]:
    """Return the current download function (for the sequential path)."""
    return download_to_file


# ---------------------------------------------------------------------------
# Sync engine
# ---------------------------------------------------------------------------


ProgressCallback = Callable[..., None]


def sync_collection(
    client: Client,
    collection: Collection,
    state: SyncState,
    *,
    on_progress: ProgressCallback | None = None,
    persist: Callable[[SyncState], None] | None = None,
    pacer: SessionPacer | None = None,
    pool: DownloadPool | None = None,
) -> dict[str, Any]:
    """Sync a single collection, updating ``state`` along the way.

    Algorithm:
      1. Take the stored ``CollectionState`` (if any).
      2. Decide the starting ``cursor``:
         * if a previous run finished (``done=true``) — start from ``""``
           and stop at the first already-known ``item_id``
           (Instagram returns items from newest to oldest);
         * if a previous run was interrupted (``done=false``) — resume
           from the stored ``cursor``.
      3. For each ``media``:
         * if ``pk`` is already in ``state.items`` — skip;
         * otherwise download and write into ``state.items``.
      4. Update ``cursor``/``done``/``last_synced_at`` in state.

    Parameters
    ----------
    client:
        Authenticated ``instagrapi.Client``.
    collection:
        Collection (result of ``iter_collections`` / ``client.collections()``).
    state:
        Global ``SyncState`` (mutated in-place).
    on_progress:
        Optional callback ``(cid, item_id, processed, skipped, error=...)``.
    persist:
        Optional callback invoked after EVERY downloaded item — lets
        the CLI write state incrementally (so a crash does not lose
        progress).
    pacer:
        ``SessionPacer`` for human-like throttling between chunk
        requests. ``None`` falls back to the singleton from
        ``get_pacer()``.
    pool:
        ``DownloadPool`` for parallel media downloads inside one item.
        ``None`` falls back to the singleton from ``get_pool()``.

    Returns
    -------
    dict with per-collection statistics.
    """
    cid = str(collection.id)
    col_state = state.collections.get(cid) or CollectionState()
    col_state.name = getattr(collection, "name", "") or col_state.name
    col_state.type = str(getattr(collection, "type", "") or col_state.type)
    col_state.media_count = int(getattr(collection, "media_count", 0) or col_state.media_count)
    # Slug for the on-disk layout — recompute from the current name so
    # that renaming a collection in Instagram is reflected on disk.
    col_state.slug = slugify_collection_name(col_state.name, fallback=cid)

    start_cursor = "" if col_state.done else col_state.cursor
    known_items: set[str] = set(col_state.items.keys())

    active_pacer = pacer or get_pacer()
    active_pool = pool or get_pool(active_pacer)
    pager = CollectionMediasPager(
        client, collection.id, start_max_id=start_cursor, pacer=active_pacer
    )

    processed = 0
    skipped = 0
    errors = 0
    new_item_ids: list[str] = []
    fetched_at = _now_iso()

    try:
        for media in pager:
            item_id = str(media.pk)

            if item_id in known_items:
                skipped += 1
                # We do NOT stop iterating even if the collection was
                # fully synced before (``done=True``): item ordering in
                # ``collection_medias_v1_chunk`` is not guaranteed, and
                # new items may appear AFTER an already-known one in the
                # head chunk. To reliably pick up new items, walk the
                # whole collection and download only what is missing
                # from ``known_items``.
                continue

            try:
                md = write_item_metadata(cid, col_state.slug, media, fetched_at, pool=active_pool)
                col_state.items[item_id] = {
                    "taken_at": md["taken_at"],
                    "source_url": md["source_url"],
                    "synced_at": fetched_at,
                    "files": [m["filename"] for m in md["media"]],
                }
                known_items.add(item_id)
                new_item_ids.append(item_id)
                processed += 1
                if on_progress:
                    on_progress(cid, item_id, processed, skipped)
                if persist is not None:
                    persist(state)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                if on_progress:
                    on_progress(cid, item_id, processed, skipped, error=str(exc))
    finally:
        col_state.cursor = pager.last_max_id
        col_state.done = bool(pager.done)
        col_state.last_synced_at = fetched_at
        state.collections[cid] = col_state

    return {
        "collection_id": cid,
        "name": col_state.name,
        "slug": col_state.slug,
        "type": col_state.type,
        "processed": processed,
        "skipped": skipped,
        "errors": errors,
        "new_items": new_item_ids,
        "cursor": col_state.cursor,
        "done": col_state.done,
        "total_known": len(col_state.items),
    }


def sync_all(
    client: Client,
    state: SyncState,
    *,
    collection_filter: list[str] | None = None,
    on_collection_start: Callable[[Collection], None] | None = None,
    on_progress: ProgressCallback | None = None,
    persist: Callable[[SyncState], None] | None = None,
    pacer: SessionPacer | None = None,
    pool: DownloadPool | None = None,
) -> dict[str, Any]:
    """Sync all collections (or a subset given by ``collection_filter``).

    Parameters
    ----------
    pacer:
        ``SessionPacer`` (see ``get_pacer()``). Used both for throttling
        between ``iter_collections`` calls and for the inter-chunk /
        inter-collection pauses.
    pool:
        ``DownloadPool`` (see ``get_pool()``). Used by ``sync_collection``
        for parallel per-item media downloads.

    Returns
    -------
    dict with the summary report (``started_at``, ``finished_at``,
    ``collections``, ``totals``).
    """
    started_at = _now_iso()
    filter_set = set(collection_filter) if collection_filter else None

    active_pacer = pacer or get_pacer()
    active_pool = pool or get_pool(active_pacer)

    target_collections: list[Collection] = []
    for c in iter_collections(client, pacer=active_pacer):
        cid = str(c.id)
        if filter_set and cid not in filter_set:
            continue
        target_collections.append(c)

    results: list[dict[str, Any]] = []
    for c in target_collections:
        # Inter-collection pause — a user usually "lingers" on one
        # collection for ~a minute before switching to the next. We
        # skip it for the first element (so startup is faster).
        if results and active_pacer.enabled:
            active_pacer.wait()
        if on_collection_start is not None:
            on_collection_start(c)
        result = sync_collection(
            client,
            c,
            state,
            on_progress=on_progress,
            persist=persist,
            pacer=active_pacer,
            pool=active_pool,
        )
        results.append(result)

    finished_at = _now_iso()
    state.last_incremental_sync_at = finished_at

    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "collections": results,
        "totals": {
            "processed": sum(r["processed"] for r in results),
            "skipped": sum(r["skipped"] for r in results),
            "errors": sum(r["errors"] for r in results),
            "collections_seen": len(results),
        },
    }
