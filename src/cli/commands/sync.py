"""``insta-boards sync`` — full state-aware sync of all collections.

CLI flags and behaviour are kept identical to the previous single-binary
``sync-instagram`` command; only the dispatch surface has changed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from src.cli._common import (
    DEFAULT_COLLECTION_LIST_FILENAME,
    collect_collection_ids,
    default_state_path,
    log,
    make_pacer_and_pool,
)
from src.client import init_client
from src.instagram_sync import SyncState, load_state, save_state, sync_all
from src.paths import resolve_from_repo_root


PROG = "sync"


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
    log(
        PROG,
        f"-> collection={cid} name={name!r}{slug_part} media_count={media_count}",
    )


def _emit_progress(
    cid: str,
    item_id: str,
    processed: int,
    skipped: int,
    error: str | None = None,
) -> None:
    if error:
        log(PROG, f"  ! collection={cid} item={item_id} error={error}")
    else:
        log(
            PROG,
            f"  + collection={cid} item={item_id} "
            f"processed={processed} skipped={skipped}",
        )


def run(args: Any) -> int:
    state_path = (
        Path(args.state_path).expanduser().resolve()
        if args.state_path
        else default_state_path()
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
            log(
                PROG,
                f"found {DEFAULT_COLLECTION_LIST_FILENAME} in the repo root — "
                f"using it as the default collection filter: {default_list}",
            )

    # Merge --collection (with comma support) and --collection-file
    # into a single deduplicated list. None = sync ALL.
    try:
        effective_filter = collect_collection_ids(
            args.collections, effective_collection_file
        )
    except FileNotFoundError as exc:
        log(PROG, f"ERROR: {exc}")
        return 2

    if args.dry_run:
        state = load_state(state_path)
        filter_repr = (
            "ALL"
            if effective_filter is None
            else f"{len(effective_filter)} selected: {','.join(effective_filter)}"
        )
        log(
            PROG,
            f"DRY-RUN state_path={state_path} "
            f"filter={filter_repr} "
            f"collections_in_state={len(state.collections)} "
            f"last_full_sync_at={state.last_full_sync_at or '-'} "
            f"last_incremental_sync_at={state.last_incremental_sync_at or '-'}",
        )
        if state.collections:
            for cid, c in state.collections.items():
                cursor_repr = "<done>" if c.done else (c.cursor or "<fresh>")
                slug = c.slug or ""
                slug_part = f" slug={slug!r}" if slug else ""
                log(
                    PROG,
                    f"  - {cid}: name={c.name!r}{slug_part} type={c.type} "
                    f"known_items={len(c.items)} cursor={cursor_repr} "
                    f"last_synced_at={c.last_synced_at or '-'}",
                )
        else:
            log(
                PROG,
                "(state is empty — the next run will perform a full sync)",
            )
        return 0

    state = load_state(state_path)

    if args.reset_all:
        for c in state.collections.values():
            c.reset()
        log(PROG, "--reset: cursor/done cleared for all collections")
    if args.reset_collections:
        for cid in args.reset_collections:
            c = state.collections.get(cid)
            if c is not None:
                c.reset()
                log(PROG, f"--reset-collection: {cid} reset")
            else:
                log(PROG, f"--reset-collection: {cid} not found in state (no-op)")
    sys.stderr.flush()

    cl, _cfg = init_client()

    # Human-like throttling — singleton, settings are read from .env.
    # --no-humanize turns it off on the fly (for CI/tests where timing
    # reproducibility matters).
    pacer, pool = make_pacer_and_pool(
        PROG, no_humanize=args.no_humanize, concurrency=args.concurrency
    )

    # persist writes state to disk after EVERY downloaded item — protection
    # against data loss when the network drops mid-large-collection.
    def persist(s: SyncState) -> None:
        try:
            save_state(state_path, s)
        except Exception as exc:  # noqa: BLE001
            log(PROG, f"WARN: failed to write state: {exc}")

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
            log(PROG, f"WARN: final state write failed: {exc}")

    totals = report["totals"]
    log(
        PROG,
        f"DONE collections_seen={totals['collections_seen']} "
        f"processed={totals['processed']} skipped={totals['skipped']} "
        f"errors={totals['errors']} "
        f"started_at={report['started_at']} finished_at={report['finished_at']}",
    )

    if args.report_json:
        report_path = Path(args.report_json).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(PROG, f"report saved: {report_path}")

    if args.print_state:
        sys.stderr.write(
            f"[{PROG}] final state:\n"
            + json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
            + "\n"
        )
        sys.stderr.flush()

    return 0
