"""Regression tests for the download-tuning ``IG_*`` environment variables.

These variables used to be read at *import time* into module-level constants
(``DEFAULT_DOWNLOAD_*``) in ``src.instagram_sync``. Because that module is
imported long before ``load_env()`` ever runs, any value supplied through a
``.env`` file (or set after import) was silently ignored. The accessors are
now lazy — these tests lock that behaviour in.
"""

from __future__ import annotations

import src.instagram_sync as ig

_ENV_VARS = (
    "IG_DOWNLOAD_TIMEOUT",
    "IG_DOWNLOAD_DELAY",
    "IG_DOWNLOAD_RETRIES",
    "IG_DOWNLOAD_BACKOFF",
)


def test_download_accessors_read_env_at_call_time(monkeypatch):
    """Values set *after* import must be reflected by the accessors."""
    monkeypatch.setenv("IG_DOWNLOAD_TIMEOUT", "999")
    monkeypatch.setenv("IG_DOWNLOAD_DELAY", "2.5")
    monkeypatch.setenv("IG_DOWNLOAD_RETRIES", "7")
    monkeypatch.setenv("IG_DOWNLOAD_BACKOFF", "1.25")

    assert ig.download_timeout() == 999
    assert ig.download_delay() == 2.5
    assert ig.download_retries() == 7
    assert ig.download_backoff() == 1.25


def test_download_accessors_fall_back_to_defaults(monkeypatch):
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    assert ig.download_timeout() == 120
    assert ig.download_delay() == 1.0
    assert ig.download_retries() == 5
    assert ig.download_backoff() == 0.5


def test_download_accessors_ignore_invalid_values(monkeypatch):
    monkeypatch.setenv("IG_DOWNLOAD_TIMEOUT", "not-a-number")
    monkeypatch.setenv("IG_DOWNLOAD_DELAY", "")

    assert ig.download_timeout() == 120
    assert ig.download_delay() == 1.0


def test_eager_module_level_constants_are_gone():
    """The eager constants must not come back — they caused the bug."""
    for name in (
        "DEFAULT_DOWNLOAD_TIMEOUT",
        "DEFAULT_DOWNLOAD_DELAY",
        "DEFAULT_DOWNLOAD_RETRIES",
        "DEFAULT_DOWNLOAD_BACKOFF",
    ):
        assert not hasattr(ig, name), (
            f"{name} should be replaced by a lazy accessor"
        )


def test_throttle_reads_delay_at_call_time(monkeypatch):
    """``_throttle`` must read the delay lazily (and never raise NameError)."""
    monkeypatch.setenv("IG_DOWNLOAD_DELAY", "0")
    # delay == 0 → returns immediately without sleeping.
    ig._throttle()


def test_get_pool_respects_reuse_pool_env(monkeypatch):
    """``IG_DOWNLOAD_POOL_REUSE`` must actually reach the pool (dead-knob regression)."""
    monkeypatch.setenv("IG_DOWNLOAD_POOL_REUSE", "0")
    assert ig.get_pool().reuse_pool is False


def test_get_pool_reuse_pool_default(monkeypatch):
    monkeypatch.delenv("IG_DOWNLOAD_POOL_REUSE", raising=False)
    assert ig.get_pool().reuse_pool is True
