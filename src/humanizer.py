"""Human-like behaviour simulation: pause distributions, User-Agent rotation,
"micro-" and "session" breaks.

Why this exists
---------------

Instagram is fairly aggressive at flagging automated downloads. A bot parser
that hits the API every ``IG_DOWNLOAD_DELAY`` seconds is easy to tell apart
from a real human — a real user:

* has log-normally distributed timing between actions (sometimes fast,
  sometimes slow, never "to the second");
* takes long pauses every now and then (a chat, a coffee, another tab);
* shows "fatigue" after a peak of activity (~10–30 actions in a row) —
  pauses get longer as focus fades;
* does not change the User-Agent every second, but it MAY change between
  "sessions" (e.g. between parser restarts).

This module centralises all of those details so ``instagram_sync`` can use
them without knowing any of the internals.

Third-party packages used
-------------------------

* ``fake-useragent`` — fallback User-Agent rotation (if installed);
  otherwise we return a realistic default, similar to what instagrapi ships.
* ``random`` + ``math`` — log-normal distribution for pauses.
"""

from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config from .env
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class HumanizerConfig:
    """Human-like simulation settings, read from ``.env``."""

    enabled: bool = field(default_factory=lambda: _env_bool("IG_HUMANIZE", True))
    # Base pause between requests (seconds). Used as the "median" of the
    # log-normal distribution.
    base_delay: float = field(default_factory=lambda: _env_float("IG_DOWNLOAD_DELAY", 1.0))
    # Sigma of the log-normal distribution (higher = "noisier" pauses).
    sigma: float = field(default_factory=lambda: _env_float("IG_HUMANIZE_SIGMA", 0.55))
    # Hard min/max for the pause (seconds), to avoid 0s or "a whole day".
    min_delay: float = field(default_factory=lambda: _env_float("IG_HUMANIZE_MIN", 0.4))
    max_delay: float = field(default_factory=lambda: _env_float("IG_HUMANIZE_MAX", 8.0))
    # Every N requests — a "micro break" (the user looked away for a second).
    micro_break_every: int = field(default_factory=lambda: _env_int("IG_HUMANIZE_MICRO_EVERY", 12))
    micro_break_min: float = field(default_factory=lambda: _env_float("IG_HUMANIZE_MICRO_MIN", 2.5))
    micro_break_max: float = field(default_factory=lambda: _env_float("IG_HUMANIZE_MICRO_MAX", 6.0))
    # Every M requests — a "session break" (a long one, like after a coffee).
    session_break_every: int = field(default_factory=lambda: _env_int("IG_HUMANIZE_SESSION_EVERY", 80))
    session_break_min: float = field(default_factory=lambda: _env_float("IG_HUMANIZE_SESSION_MIN", 15.0))
    session_break_max: float = field(default_factory=lambda: _env_float("IG_HUMANIZE_SESSION_MAX", 45.0))
    # User-Agent rotation between sessions.
    rotate_user_agent: bool = field(default_factory=lambda: _env_bool("IG_USER_AGENT_ROTATE", False))


# ---------------------------------------------------------------------------
# Log-normal pause
# ---------------------------------------------------------------------------


def _lognormal_sample(median: float, sigma: float) -> float:
    """Return a log-normally distributed value with the given median.

    The log-normal distribution models the time-between-actions of a real
    user pretty well: most values cluster around a typical pause, but the
    "long" tail is much heavier than the "fast" tail.
    """
    if median <= 0:
        return 0.0
    mu = math.log(median)
    return math.exp(random.gauss(mu, sigma))


def human_pause(
    base_delay: float,
    *,
    sigma: float = 0.55,
    min_delay: float = 0.4,
    max_delay: float = 8.0,
) -> float:
    """Sleep ``base_delay`` ± noise and return the actual delay applied.

    If ``base_delay <= 0`` — sleep 0 (passthrough, so the "disabled" mode
    is not broken).
    """
    if base_delay <= 0:
        return 0.0
    delay = _lognormal_sample(median=base_delay, sigma=sigma)
    delay = max(min_delay, min(max_delay, delay))
    time.sleep(delay)
    return delay


# ---------------------------------------------------------------------------
# User-Agent
# ---------------------------------------------------------------------------

# "Safe" defaults — match what instagrapi uses out of the box for a fresh
# ``Client()``. They are needed as a fallback when ``fake-useragent`` is
# not installed or the network is unavailable.
_FALLBACK_UAS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)


def _get_fake_ua() -> str | None:
    """Try to obtain a random User-Agent via ``fake-useragent``.

    Imported lazily so the parser does not fail if the package is not
    installed (it is optional — see ``pyproject.toml``).
    """
    try:
        from fake_useragent import UserAgent  # type: ignore
    except Exception:
        return None
    try:
        ua = UserAgent(browsers=["Chrome", "Firefox", "Safari", "Edge"], os=["Windows", "MacOS", "Linux"])
        return str(ua.random)
    except Exception as exc:  # noqa: BLE001
        # fake-useragent tries to download a JSON from githubusercontent.com
        # and fails when the network is unreachable / behind a corporate
        # firewall. Not critical — we have the fallback list.
        log.debug("fake-useragent unavailable: %s", exc)
        return None


def pick_user_agent(*, use_fake: bool = True) -> str:
    """Return a User-Agent for the current parser "session".

    If ``use_fake=True`` and ``fake-useragent`` is installed — pick a
    random current UA. Otherwise — pick randomly from the built-in
    fallback list.
    """
    if use_fake:
        ua = _get_fake_ua()
        if ua:
            return ua
    return random.choice(_FALLBACK_UAS)


# ---------------------------------------------------------------------------
# SessionPacer — single point of throttling
# ---------------------------------------------------------------------------


class SessionPacer:
    """Thread-safe session throttler with human-like pause distribution.

    Replaces a bare ``time.sleep`` in ``instagram_sync._throttle``: instead
    of "exactly N seconds between requests" we use a log-normal
    distribution and occasionally insert micro- and session-level breaks.
    Behaviour is configured through ``HumanizerConfig`` (usually read from
    ``.env``).

    Examples
    --------
    >>> pacer = SessionPacer()                         # defaults from .env
    >>> pacer.wait()                                   # before the first request
    >>> for media in pager:
    ...     pacer.wait()                               # between requests
    """

    def __init__(self, config: HumanizerConfig | None = None) -> None:
        self._config = config or HumanizerConfig()
        self._lock = threading.Lock()
        self._request_count = 0
        # Thread-safe User-Agent "generator": when rotation is enabled,
        # the UA is updated only when a brand new SessionPacer is created —
        # i.e. on a "new Instagram visit", as if a new browser tab was
        # opened.
        self._user_agent: str | None = None

    # --- Config -----------------------------------------------------------

    @property
    def config(self) -> HumanizerConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def set_enabled(self, enabled: bool) -> None:
        """Enable/disable human-like simulation at runtime."""
        with self._lock:
            self._config.enabled = bool(enabled)

    # --- User-Agent -------------------------------------------------------

    def get_user_agent(self) -> str:
        """Return (and cache when needed) the session's User-Agent."""
        with self._lock:
            if self._user_agent is None:
                self._user_agent = pick_user_agent(use_fake=self._config.rotate_user_agent)
            return self._user_agent

    def reset_user_agent(self) -> str:
        """Drop the cache and return a new UA (simulating a "new session")."""
        with self._lock:
            self._user_agent = pick_user_agent(use_fake=self._config.rotate_user_agent)
            return self._user_agent

    # --- Main throttling ---------------------------------------------------

    def wait(self) -> float:
        """Block execution for a human-like pause. Returns the sleep time.

        Logic:
          1. If disabled — pause = ``base_delay`` (exactly, like before).
          2. If enabled — base pause is log-normally distributed.
          3. Every ``micro_break_every`` requests — a short "micro break".
          4. Every ``session_break_every`` requests — a long "session break".
        """
        with self._lock:
            self._request_count += 1
            count = self._request_count
            cfg = self._config

        if not cfg.enabled or cfg.base_delay <= 0:
            # Disabled: classic behaviour (flat pause). Keeps dry-run tests
            # and the "aggressive" mode working exactly as before.
            if cfg.base_delay > 0:
                time.sleep(cfg.base_delay)
                return cfg.base_delay
            return 0.0

        # --- human-like branch -----------------------------------------
        # 1) Base inter-request sleep (log-normal).
        delay = _lognormal_sample(median=cfg.base_delay, sigma=cfg.sigma)
        delay = max(cfg.min_delay, min(cfg.max_delay, delay))

        # 2) Micro break: every N requests — a short pause.
        if cfg.micro_break_every > 0 and count % cfg.micro_break_every == 0:
            delay += random.uniform(cfg.micro_break_min, cfg.micro_break_max)

        # 3) Session break: every M requests — a long pause.
        if (
            cfg.session_break_every > 0
            and count % cfg.session_break_every == 0
            and cfg.micro_break_every != cfg.session_break_every
        ):
            delay += random.uniform(cfg.session_break_min, cfg.session_break_max)

        time.sleep(delay)
        return delay

    # --- Decorator / utilities --------------------------------------------

    def wrap(self, fn: Callable[..., "T"], *args: "T", **kwargs: "T") -> "T":
        """Convenience wrapper: ``pacer.wait()`` + ``fn(*args, **kwargs)``."""
        self.wait()
        return fn(*args, **kwargs)
