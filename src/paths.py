"""Resolution of the on-disk project root.

In a packaged ``uvbox`` binary the code lives inside ``site-packages``, so
there is no source checkout above it and the working directory wins — which
is exactly what we want (``data`` / ``.env`` / state stay relative to where
the user launched the binary).

Resolution order (see :func:`repo_root`):

1. the ``IG_REPO_ROOT`` environment variable (absolute or ``~`` path);
2. the nearest ancestor directory that is a *real* ``insta-boards`` checkout
   — it must contain **both** ``pyproject.toml`` and ``src/paths.py``;
3. :func:`pathlib.Path.cwd` as the final fallback.
"""

from __future__ import annotations

import os
from pathlib import Path

# Marker files that together identify this project's source checkout.
_PYPROJECT_MARKER = "pyproject.toml"
_SOURCE_MARKER = Path("src") / "paths.py"


def _explicit_root() -> Path | None:
    """Return ``IG_REPO_ROOT`` (resolved) if it is set and non-empty."""
    raw = os.getenv("IG_REPO_ROOT", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _looks_like_checkout(candidate: Path) -> bool:
    """True if ``candidate`` really is an ``insta-boards`` source checkout.

    Requiring both ``pyproject.toml`` and ``src/paths.py`` prevents an
    unrelated project (or an arbitrary directory that merely happens to
    contain a ``pyproject.toml`` somewhere above ``site-packages``) from
    hijacking the data root.
    """
    return (candidate / _PYPROJECT_MARKER).is_file() and (
        candidate / _SOURCE_MARKER
    ).is_file()


def repo_root() -> Path:
    """Resolve the project root directory.

    Priority: ``IG_REPO_ROOT`` → nearest real checkout → ``Path.cwd()``.
    """
    explicit = _explicit_root()
    if explicit is not None:
        return explicit

    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if _looks_like_checkout(parent):
            return parent
    return Path.cwd()


def resolve_from_repo_root(*parts: str) -> Path:
    return repo_root().joinpath(*parts)
