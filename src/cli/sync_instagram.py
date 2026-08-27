from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from src.client import init_client
from src.instagram_sync import (
    SyncState,
    get_pacer,
    get_pool,
    load_state,
    save_state,
    sync_all,
)
from src.paths import resolve_from_repo_root


DEFAULT_COLLECTION_LIST_FILENAME = "sync-collection-list.txt"


def _default_state_path() -> Path:
    env = os.getenv("IG_STATE_PATH", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return resolve_from_repo_root("data", "state", "instagram_sync.json")


def _split_collection_ids(raw: str) -> list[str]:
    """Split ``"111,222,333"`` into ``["111", "222", "333"]`` (with trim)."""
    return [s.strip() for s in raw.split(",") if s.strip()]


def _read_collection_file(path: Path) -> list[str]:
    """Read a file with a list of collection IDs.

    File format (one ID per line OR comma-separated; ``#`` is a comment):

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
        ids.extend(_split_collection_ids(stripped))
    return ids


def _collect_collection_ids(
    cli_values: list[str] | None, file_path: str | None
) -> list[str] | None:
    """Merge ``--collection`` (comma-separated supported) and ``--collection-file``.

    Returns ``None`` if both sources are empty (= sync ALL). Otherwise —
    a deduplicated list with order preserved.
    """
    raw: list[str] = []
    if cli_values:
        for value in cli_values:
            raw.extend(_split_collection_ids(value))
    if file_path:
        resolved = Path(file_path).expanduser().resolve()
        raw.extend(_read_collection_file(resolved))
    if not raw:
        return None
    seen: set[str] = set()
    result: list[str] = []
    for cid in raw:
        if cid and cid not in seen:
            seen.add(cid)
            result.append(cid)
    return result


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sync-instagram",
        description=(
            "Full sync of Instagram Saved Collections into a local directory. "
            "Supports incremental download of new items via a JSON state file: "
            "re-runs do not re-download already-synced items, they only pick "
            "up new ones."
        ),
    )
    p.add_argument(
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
    p.add_argument(
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
    p.add_argument(
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
    p.add_argument(
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
    p.add_argument(
        "--reset",
        dest="reset_all",
        action="store_true",
        help="Reset cursor/done for ALL collections in state (items are kept).",
    )
    p.add_argument(
        "--reset-collection",
        dest="reset_collections",
        action="append",
        default=None,
        type=str,
        help="Reset cursor/done for the given collection ID only. Can be passed multiple times.",
    )
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "Do not contact Instagram and do not write state: only show "
            "the current state and the plan."
        ),
    )
    p.add_argument(
        "--report-json",
        dest="report_json",
        default=None,
        type=str,
        help="Path to write a JSON sync report (in addition to the stderr log).",
    )
    p.add_argument(
        "--print-state",
        dest="print_state",
        action="store_true",
        help="After the sync, print the final state to stderr (for debugging).",
    )
    p.add_argument(
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
    p.add_argument(
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
    return p.parse_args()


def _emit_collection_start(c: Any) -> None:
    media_count = getattr(c, "media_count", 0)
    name = getattr(c, "name", "") or ""
    cid = str(getattr(c, "id", ""))
    try:
        from src.naming import slugify_collection_name
        slug = slugify_collection_name(name, fallback=cid) if cid else ""
    except Exception:
        slug = ""
    slug_part = f" slug={slug!r}" if slug else ""
    sys.stderr.write(
        f"[sync-instagram] -> collection={cid} name={name!r}{slug_part} media_count={media_count}\n"
    )
    sys.stderr.flush()


def _emit_progress(
    cid: str,
    item_id: str,
    processed: int,
    skipped: int,
    error: str | None = None,
) -> None:
    if error:
        sys.stderr.write(f"  ! collection={cid} item={item_id} error={error}\n")
    else:
        sys.stderr.write(
            f"  + collection={cid} item={item_id} processed={processed} skipped={skipped}\n"
        )
    sys.stderr.flush()


def main() -> int:
    args = _parse_args()

    state_path = (
        Path(args.state_path).expanduser().resolve()
        if args.state_path
        else _default_state_path()
    )

    workdir = (
        Path(args.workdir).expanduser().resolve() if args.workdir else None
    )
    if workdir is not None:
        os.chdir(workdir)

    # Auto-detect the collection filter: if the user passed neither
    # --collection nor --collection-file but sync-collection-list.txt
    # exists in the repo root — use it as the default list. If the
    # file is missing — the filter stays empty (= sync ALL).
    effective_collection_file = args.collection_file
    if not args.collections and not effective_collection_file:
        default_list = resolve_from_repo_root(DEFAULT_COLLECTION_LIST_FILENAME)
        if default_list.is_file():
            effective_collection_file = str(default_list)
            sys.stderr.write(
                f"[sync-instagram] found {DEFAULT_COLLECTION_LIST_FILENAME} "
                f"in the repo root — using it as the default collection "
                f"filter: {default_list}\n"
            )
            sys.stderr.flush()

    # Merge --collection (with comma support) and --collection-file
    # into a single deduplicated list. None = sync ALL.
    try:
        effective_filter = _collect_collection_ids(
            args.collections, effective_collection_file
        )
    except FileNotFoundError as exc:
        sys.stderr.write(f"[sync-instagram] ERROR: {exc}\n")
        sys.stderr.flush()
        return 2

    if args.dry_run:
        state = load_state(state_path)
        filter_repr = (
            "ALL"
            if effective_filter is None
            else f"{len(effective_filter)} selected: {','.join(effective_filter)}"
        )
        sys.stderr.write(
            f"[sync-instagram] DRY-RUN state_path={state_path} "
            f"filter={filter_repr} "
            f"collections_in_state={len(state.collections)} "
            f"last_full_sync_at={state.last_full_sync_at or '-'} "
            f"last_incremental_sync_at={state.last_incremental_sync_at or '-'}\n"
        )
        if state.collections:
            for cid, c in state.collections.items():
                cursor_repr = "<done>" if c.done else (c.cursor or "<fresh>")
                slug = c.slug or ""
                slug_part = f" slug={slug!r}" if slug else ""
                sys.stderr.write(
                    f"  - {cid}: name={c.name!r}{slug_part} type={c.type} "
                    f"known_items={len(c.items)} cursor={cursor_repr} "
                    f"last_synced_at={c.last_synced_at or '-'}\n"
                )
        else:
            sys.stderr.write("  (state is empty — the next run will perform a full sync)\n")
        sys.stderr.flush()
        return 0

    state = load_state(state_path)

    if args.reset_all:
        for c in state.collections.values():
            c.reset()
        sys.stderr.write("[sync-instagram] --reset: cursor/done cleared for all collections\n")
    if args.reset_collections:
        for cid in args.reset_collections:
            c = state.collections.get(cid)
            if c is not None:
                c.reset()
                sys.stderr.write(f"[sync-instagram] --reset-collection: {cid} reset\n")
            else:
                sys.stderr.write(
                    f"[sync-instagram] --reset-collection: {cid} not found in state (no-op)\n"
                )
    sys.stderr.flush()

    cl, _cfg = init_client()

    # Human-like throttling — singleton, settings are read from .env.
    # --no-humanize turns it off on the fly (for CI/tests where timing
    # reproducibility matters).
    pacer = get_pacer()
    if args.no_humanize:
        pacer.set_enabled(False)
    # --concurrency overrides the env value if provided explicitly.
    pool = get_pool(pacer)
    if args.concurrency is not None and args.concurrency != pool.max_workers:
        from src.parallel import DownloadPool  # noqa: WPS433

        pool = DownloadPool(pacer=pacer, max_workers=max(1, int(args.concurrency)))

    sys.stderr.write(
        f"[sync-instagram] pacer={'on' if pacer.enabled else 'off'} "
        f"base_delay={pacer.config.base_delay}s "
        f"concurrency={pool.max_workers} "
        f"ua_rotate={pacer.config.rotate_user_agent}\n"
    )
    sys.stderr.flush()

    # persist writes state to disk after EVERY downloaded item — protection
    # against data loss when the network drops mid-large-collection.
    def persist(s: SyncState) -> None:
        try:
            save_state(state_path, s)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[sync-instagram] WARN: failed to write state: {exc}\n")
            sys.stderr.flush()

    try:
        report = sync_all(
            cl,
            state,
            collection_filter=effective_filter,
            on_collection_start=_emit_collection_start,
            on_progress=_emit_progress,
            persist=persist,
            pacer=pacer,
            pool=pool,
        )
    finally:
        # Final state snapshot — even if sync_all raised, we save
        # everything that we managed to download.
        try:
            save_state(state_path, state)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[sync-instagram] WARN: final state write failed: {exc}\n")

    totals = report["totals"]
    sys.stderr.write(
        f"[sync-instagram] DONE "
        f"collections_seen={totals['collections_seen']} "
        f"processed={totals['processed']} skipped={totals['skipped']} "
        f"errors={totals['errors']} "
        f"started_at={report['started_at']} finished_at={report['finished_at']}\n"
    )
    sys.stderr.flush()

    if args.report_json:
        report_path = Path(args.report_json).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        sys.stderr.write(f"[sync-instagram] report saved: {report_path}\n")
        sys.stderr.flush()

    if args.print_state:
        sys.stderr.write(
            "[sync-instagram] final state:\n"
            + json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
            + "\n"
        )
        sys.stderr.flush()

    return 0
