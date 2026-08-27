from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import BadPassword, LoginRequired, ProxyAddressIsBlocked, TwoFactorRequired

from .paths import resolve_from_repo_root


# Default "human-like" User-Agent strings. Used as a fallback when
# ``fake-useragent`` is not installed or cannot download its database.
# These are current desktop-browser UAs. instagrapi ships similar ones by
# default, but they are fixed — we keep a small pool here so that a "new
# session" can pick a different one from the previous run.
_FALLBACK_USER_AGENTS: tuple[str, ...] = (
    # Chrome 120 / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Safari 17 / macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    # Firefox 121 / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) "
    "Gecko/20100101 Firefox/121.0",
    # Chrome 120 / Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Edge 120 / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
)


@dataclass(frozen=True)
class IgConfig:
    username: str | None
    password: str | None
    proxy: str | None
    settings_path: Path
    sessionid: str | None
    two_fa_code: str | None


def load_env() -> None:
    """Load environment variables from ``.env`` files in priority order.

    A later file does NOT override values from an earlier one — it only adds
    missing keys. Sources (highest priority first):
      1. ``<repo_root>/.env``
      2. ``<cwd>/.env``
    """
    repo_env = resolve_from_repo_root(".env")
    cwd_env = Path.cwd().joinpath(".env")

    load_dotenv(repo_env, override=False)
    load_dotenv(cwd_env, override=False)


def get_config() -> IgConfig:
    settings_path = os.getenv("IG_SETTINGS_PATH", "").strip()
    if settings_path:
        resolved_settings_path = Path(settings_path).expanduser().resolve()
    else:
        resolved_settings_path = resolve_from_repo_root("secrets", "instagrapi.settings.json")

    return IgConfig(
        username=os.getenv("IG_USERNAME"),
        password=os.getenv("IG_PASSWORD"),
        proxy=os.getenv("IG_PROXY") or None,
        settings_path=resolved_settings_path,
        sessionid=os.getenv("IG_SESSIONID") or None,
        two_fa_code=os.getenv("IG_2FA_CODE") or None,
    )


def ensure_dir_for_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _pick_user_agent() -> str:
    """Return a User-Agent for a fresh ``Client()``.

    Priority:
      1. ``fake-useragent.UserAgent().random`` (if installed and available);
      2. A random pick from ``_FALLBACK_USER_AGENTS`` (current desktop UAs).
    """
    try:
        from fake_useragent import UserAgent  # type: ignore

        ua = UserAgent(browsers=["Chrome", "Firefox", "Safari", "Edge"], os=["Windows", "MacOS", "Linux"])
        return str(ua.random)
    except Exception:
        import random

        return random.choice(_FALLBACK_USER_AGENTS)


def _apply_user_agent(cl: Client, *, rotate: bool = True) -> None:
    """Replace the User-Agent with a fresh one if rotation is enabled.

    instagrapi stores the UA in ``cl.user_agent`` and uses it from
    ``cl.private_request()`` / ``cl.public_request()`` / ``cl.http``. We
    swap it BEFORE the first request so each "new session" looks like a
    fresh browser visit.

    If ``rotate=False``, the UA installed by instagrapi is left untouched
    (the default behaviour before any rotation support was added).
    """
    if not rotate:
        return
    new_ua = _pick_user_agent()
    try:
        cl.user_agent = new_ua
    except Exception:
        # On very old instagrapi versions the attribute may be read-only —
        # in that case just fall back to the default.
        pass
    # Also update ``cl.settings['user_agent']`` — some code paths read
    # the UA from there.
    try:
        if isinstance(getattr(cl, "settings", None), dict):
            cl.settings["user_agent"] = new_ua
    except Exception:
        pass


def init_client() -> tuple[Client, IgConfig]:
    load_env()
    cfg = get_config()

    cl = Client()
    if cfg.proxy and cfg.proxy.strip():
        cl.set_proxy(cfg.proxy.strip())

    # User-Agent rotation (toggleable via IG_USER_AGENT_ROTATE in .env).
    # We apply the rotation AFTER loading saved settings: if settings.json
    # still has the previous UA, instagrapi would otherwise restore it
    # verbatim. So we load first, then patch the UA on top.
    rotate_ua = os.getenv("IG_USER_AGENT_ROTATE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
        "y",
        "t",
    )

    if cfg.settings_path.exists():
        cl.load_settings(str(cfg.settings_path))
        if rotate_ua:
            _apply_user_agent(cl, rotate=True)

    if cfg.sessionid and cfg.sessionid.strip():
        cl.login_by_sessionid(cfg.sessionid.strip())
        ensure_dir_for_file(cfg.settings_path)
        cl.dump_settings(str(cfg.settings_path))
        return cl, cfg

    if cfg.username and cfg.password and cfg.username.strip() and cfg.password.strip():
        verification_code = cfg.two_fa_code.strip() if cfg.two_fa_code else None
        try:
            if verification_code:
                cl.login(cfg.username.strip(), cfg.password.strip(), verification_code=verification_code)
            else:
                cl.login(cfg.username.strip(), cfg.password.strip())
        except TwoFactorRequired as e:
            raise RuntimeError(
                "Instagram requires 2FA: set the one-time code in IG_2FA_CODE and re-run."
            ) from e
        except ProxyAddressIsBlocked as e:
            raise RuntimeError(
                "Instagram blocked the IP/proxy. Set IG_PROXY (a clean one, not shared/free) and re-run.",
            ) from e
        except BadPassword as e:
            raise RuntimeError(
                "Login via IG_USERNAME/IG_PASSWORD failed. If the account only offers "
                "'Log in with Facebook', use IG_SESSIONID (the sessionid cookie from web) "
                "or set a separate Instagram password, or try IG_PROXY (sometimes the IP is blacklisted).",
            ) from e
        # After login() instagrapi may have reset the UA to its default —
        # if rotation is enabled, apply it once more.
        if rotate_ua:
            _apply_user_agent(cl, rotate=True)
        ensure_dir_for_file(cfg.settings_path)
        cl.dump_settings(str(cfg.settings_path))
        return cl, cfg

    try:
        cl.get_timeline_feed()
    except LoginRequired as e:
        raise RuntimeError(
            "No valid instagrapi session. Set IG_USERNAME/IG_PASSWORD or IG_SESSIONID.",
        ) from e

    ensure_dir_for_file(cfg.settings_path)
    cl.dump_settings(str(cfg.settings_path))
    return cl, cfg


def dump_jsonl(items: Iterable[dict[str, Any]]) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    except Exception:
        pass
    for it in items:
        line = json.dumps(it, ensure_ascii=False, separators=(",", ":"))
        print(line)
