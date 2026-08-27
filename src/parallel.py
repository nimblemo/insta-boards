"""Concurrency control for parallel media-file downloads.

Why this exists
---------------

In carousel posts Instagram returns up to 10 media files per single
``media`` entry, and a single collection can have hundreds of items.
Downloading them strictly one-by-one is slow (especially for video).
But "opening" 50 connections at once is a recipe for both Instagram
rate-limits and suspicious traffic patterns.

We introduce a **bounded thread pool with a semaphore**: at most
``IG_DOWNLOAD_CONCURRENCY`` downloads run in parallel. Every download
still goes through the shared ``SessionPacer`` — so even parallel
downloads look like a "series of careful clicks" rather than a DDoS.

Thread safety
-------------

* ``DownloadPool`` is created once and shared across calls.
* ``requests.Session`` is already thread-safe (``Session.request`` holds
  an internal ``RLock``); we reuse it.
* ``SessionPacer.wait()`` is protected by ``threading.Lock`` — the
  request counter and the sleeping thread do not step on each other.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .humanizer import SessionPacer

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class ParallelConfig:
    """Concurrency settings, read from ``.env``."""

    # How many simultaneous downloads we allow. 1 = classic sequential
    # mode (the historical default); 2-4 = a reasonable compromise
    # between speed and rate-limit risk; >8 — almost-guaranteed "IP ban"
    # after a few hundred files.
    max_workers: int = field(default_factory=lambda: _env_int("IG_DOWNLOAD_CONCURRENCY", 1))
    # Pool lifetime: if True, the pool is created lazily and reused
    # across calls. If False, every download_many creates its own pool
    # (useful for isolated CLI runs).
    reuse_pool: bool = field(default_factory=lambda: _env_int("IG_DOWNLOAD_POOL_REUSE", 1) == 1)


# Global singleton pool so we do not spawn fresh threads on every item.
_pool_lock = threading.Lock()
_shared_pool: ThreadPoolExecutor | None = None
_shared_pool_workers: int = 0


def _get_or_create_pool(max_workers: int) -> ThreadPoolExecutor:
    """Return the singleton pool, creating it lazily on first use.

    If ``max_workers`` changed since the last call — the old pool is
    shut down and a new one is created. This is the scenario where the
    CLI flag ``--concurrency`` overrides the limit at runtime.
    """
    global _shared_pool, _shared_pool_workers
    with _pool_lock:
        if _shared_pool is None or _shared_pool_workers != max_workers:
            if _shared_pool is not None:
                _shared_pool.shutdown(wait=True, cancel_futures=True)
            _shared_pool = ThreadPoolExecutor(
                max_workers=max(1, max_workers),
                thread_name_prefix="ig-dl",
            )
            _shared_pool_workers = max_workers
        return _shared_pool


def _shutdown_shared_pool() -> None:
    """Force-stop the singleton pool (mostly needed for tests)."""
    global _shared_pool
    with _pool_lock:
        if _shared_pool is not None:
            _shared_pool.shutdown(wait=True, cancel_futures=True)
            _shared_pool = None


@dataclass
class DownloadJob:
    """A single "download url -> target" task."""

    url: str
    target: Path
    label: str = ""  # for logs / errors


@dataclass
class DownloadResult:
    """Result of a single ``DownloadJob``."""

    job: DownloadJob
    ok: bool
    cached: bool = False  # True if the file was already on disk
    error: str | None = None
    elapsed: float = 0.0


class DownloadPool:
    """Thread-safe download pool with a concurrency limit.

    Usage
    -----

    >>> pool = DownloadPool(pacer=SessionPacer(), max_workers=4)
    >>> results = pool.download_many([
    ...     DownloadJob(url="https://.../0.jpg", target=Path("a_0.jpg")),
    ...     DownloadJob(url="https://.../1.jpg", target=Path("a_1.jpg")),
    ... ])

    A singleton process pool is used by default, but this can be
    overridden with ``reuse_pool=False``.

    Every download goes through ``pacer.wait()`` — so even with
    ``max_workers=4`` the overall request rhythm stays human-like.
    """

    def __init__(
        self,
        pacer: SessionPacer,
        *,
        max_workers: int = 1,
        reuse_pool: bool = True,
        download_fn: Callable[[str, Path], bool] | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._pacer = pacer
        self._max_workers = max(1, int(max_workers))
        self._reuse_pool = reuse_pool
        # By default we download via ``instagram_sync.download_to_file``
        # (lazy import to avoid a circular dependency).
        self._download_fn = download_fn

    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def pacer(self) -> SessionPacer:
        return self._pacer

    def _resolve_download_fn(self) -> Callable[[str, Path], bool]:
        if self._download_fn is not None:
            return self._download_fn
        from .instagram_sync import download_to_file  # noqa: WPS433
        return download_to_file

    def download_one(self, job: DownloadJob) -> DownloadResult:
        """Download a single file. Locked via ``pacer.wait()``."""
        import time as _time

        download = self._resolve_download_fn()
        if job.target.exists() and job.target.stat().st_size > 0:
            return DownloadResult(job=job, ok=True, cached=True, elapsed=0.0)
        # Human-like pause BEFORE every download.
        self._pacer.wait()
        started = _time.monotonic()
        try:
            download(job.url, job.target)
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(
                job=job,
                ok=False,
                error=str(exc),
                elapsed=_time.monotonic() - started,
            )
        return DownloadResult(
            job=job,
            ok=True,
            cached=False,
            elapsed=_time.monotonic() - started,
        )

    def download_many(
        self,
        jobs: Iterable[DownloadJob],
        *,
        fail_fast: bool = False,
    ) -> list[DownloadResult]:
        """Download a list of ``DownloadJob`` in parallel (within the limit).

        Parameters
        ----------
        jobs:
            Iterable of jobs. If empty — returns ``[]``.
        fail_fast:
            If ``True``, the first exception cancels the remaining tasks.
            Default ``False`` — all tasks complete and errors are
            collected in ``DownloadResult.error``.

        Returns
        -------
        ``list[DownloadResult]`` in the same order as ``jobs``.
        """
        job_list = list(jobs)
        if not job_list:
            return []

        # The ``max_workers=1`` case: run strictly sequentially, no
        # thread-pool overhead.
        if self._max_workers == 1:
            results: list[DownloadResult] = []
            for job in job_list:
                res = self.download_one(job)
                results.append(res)
                if not res.ok and fail_fast:
                    break
            return self._align(job_list, results, fail_fast)

        # Parallel mode: submit + as_completed.
        pool = (
            _get_or_create_pool(self._max_workers)
            if self._reuse_pool
            else ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="ig-dl")
        )
        try:
            future_to_idx = {pool.submit(self.download_one, j): i for i, j in enumerate(job_list)}
            results_by_idx: dict[int, DownloadResult] = {}
            for fut in as_completed(future_to_idx):
                idx = future_to_idx[fut]
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001
                    res = DownloadResult(
                        job=job_list[idx],
                        ok=False,
                        error=str(exc),
                    )
                results_by_idx[idx] = res
                if not res.ok and fail_fast:
                    # Drain already-running futures so we do not leave
                    # them dangling.
                    for f, i in future_to_idx.items():
                        if i != idx and f not in (fut,):
                            try:
                                results_by_idx[i] = f.result(timeout=0.01)
                            except Exception:
                                pass
                    break
        finally:
            if not self._reuse_pool:
                pool.shutdown(wait=True, cancel_futures=True)

        ordered = [results_by_idx.get(i) or DownloadResult(job=job_list[i], ok=False, error="cancelled") for i in range(len(job_list))]
        return ordered

    @staticmethod
    def _align(
        jobs: list[DownloadJob],
        results: list[DownloadResult],
        fail_fast: bool,
    ) -> list[DownloadResult]:
        """Pad a truncated fail_fast result with stubs for the skipped jobs."""
        if len(results) >= len(jobs):
            return results
        padded = list(results)
        for i in range(len(results), len(jobs)):
            padded.append(DownloadResult(job=jobs[i], ok=False, error="cancelled"))
        return padded
