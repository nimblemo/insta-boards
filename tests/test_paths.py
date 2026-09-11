"""Tests for ``src.paths.repo_root`` resolution."""

from __future__ import annotations

from pathlib import Path

import src.paths as paths


def _fake_module(tmp_path: Path, *parts: str) -> Path:
    """Create a fake ``paths.py`` at ``tmp_path/parts...`` and return it."""
    fake = tmp_path.joinpath(*parts)
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text("", encoding="utf-8")
    return fake


def test_ig_repo_root_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("IG_REPO_ROOT", str(tmp_path))
    assert paths.repo_root() == tmp_path.resolve()


def test_ig_repo_root_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("IG_REPO_ROOT", "~")
    assert paths.repo_root() == tmp_path.resolve()


def test_falls_back_to_cwd_without_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("IG_REPO_ROOT", raising=False)
    fake = _fake_module(tmp_path, "site-packages", "src", "paths.py")
    monkeypatch.setattr(paths, "__file__", str(fake))

    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert paths.repo_root() == Path.cwd()


def test_ignores_ancestor_with_pyproject_but_no_source(tmp_path, monkeypatch):
    """An unrelated ``pyproject.toml`` must not hijack the root."""
    monkeypatch.delenv("IG_REPO_ROOT", raising=False)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "pyproject.toml").write_text(
        "[project]\nname = 'something-else'\n", encoding="utf-8"
    )
    fake = _fake_module(
        tmp_path, "unrelated", "site-packages", "src", "paths.py"
    )
    monkeypatch.setattr(paths, "__file__", str(fake))

    workdir = fake.parent / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert paths.repo_root() == Path.cwd()


def test_accepts_real_checkout_ancestor(tmp_path, monkeypatch):
    monkeypatch.delenv("IG_REPO_ROOT", raising=False)
    repo = tmp_path / "checkout"
    (repo / "src").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'insta-boards'\n", encoding="utf-8"
    )
    fake = _fake_module(repo, "src", "paths.py")
    monkeypatch.setattr(paths, "__file__", str(fake))
    monkeypatch.chdir(tmp_path)

    assert paths.repo_root() == repo.resolve()
