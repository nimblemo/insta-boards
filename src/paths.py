from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()


def resolve_from_repo_root(*parts: str) -> Path:
    return repo_root().joinpath(*parts)
