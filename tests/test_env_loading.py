"""End-to-end tests for ``.env`` discovery and precedence.

They reproduce the packaged-binary scenario: the CLI is launched from an
arbitrary working directory, with ``IG_REPO_ROOT`` pointing at it and a
``.env`` sitting next to it. No network and no credentials are involved —
``sync --dry-run`` never contacts Instagram.
"""

from __future__ import annotations

import os

import pytest

import src.cli.app as app
from src.client import load_env

_IG_VARS = (
    "IG_REPO_ROOT",
    "IG_STATE_PATH",
    "IG_SETTINGS_PATH",
    "IG_USERNAME",
    "IG_PASSWORD",
    "IG_SESSIONID",
    "IG_PROXY",
    "IG_2FA_CODE",
)


@pytest.fixture
def env(monkeypatch):
    """A ``monkeypatch`` that also undoes variables python-dotenv introduces.

    ``monkeypatch`` only reverts the variables it touched itself, so a
    ``load_dotenv`` call that adds brand-new keys would otherwise leak into
    other tests.
    """
    snapshot = dict(os.environ)
    for name in _IG_VARS:
        monkeypatch.delenv(name, raising=False)
    yield monkeypatch
    os.environ.clear()
    os.environ.update(snapshot)


def test_dotenv_in_working_directory_is_honoured(tmp_path, env, capsys):
    """``sync --dry-run`` must pick up ``IG_STATE_PATH`` from ``./.env``."""
    custom_state = tmp_path / "custom-state.json"
    env.setenv("IG_REPO_ROOT", str(tmp_path))
    env.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"IG_STATE_PATH={custom_state.as_posix()}\n",
        encoding="utf-8",
    )

    rc = app.main(["sync", "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0
    assert str(custom_state.resolve()) in captured.err


def test_load_env_populates_missing_variables(tmp_path, env):
    env.setenv("IG_REPO_ROOT", str(tmp_path))
    env.chdir(tmp_path)
    (tmp_path / ".env").write_text("IG_DOWNLOAD_RETRIES=42\n", encoding="utf-8")

    load_env()

    assert os.environ["IG_DOWNLOAD_RETRIES"] == "42"


def test_load_env_does_not_override_real_environment(tmp_path, env):
    """Real environment variables win over ``.env``."""
    env.setenv("IG_REPO_ROOT", str(tmp_path))
    env.chdir(tmp_path)
    (tmp_path / ".env").write_text("IG_DOWNLOAD_TIMEOUT=111\n", encoding="utf-8")
    env.setenv("IG_DOWNLOAD_TIMEOUT", "222")

    load_env()

    assert os.environ["IG_DOWNLOAD_TIMEOUT"] == "222"


def test_empty_higher_priority_dotenv_does_not_mask_lower(tmp_path, env):
    """An empty ``KEY=`` in the higher-priority ``.env`` must not mask a real value.

    Regression: python-dotenv treats ``KEY=`` as a *present* key, so the old
    two-call ``load_dotenv`` implementation let the repo's empty
    ``IG_STATE_PATH=`` shadow the value from the working directory.
    """
    repo_dir = tmp_path / "repo"
    wd_dir = tmp_path / "wd"
    repo_dir.mkdir()
    wd_dir.mkdir()
    (repo_dir / ".env").write_text("IG_STATE_PATH=\n", encoding="utf-8")
    (wd_dir / ".env").write_text(
        "IG_STATE_PATH=D:/tmp/from-wd.json\n", encoding="utf-8"
    )
    env.setenv("IG_REPO_ROOT", str(repo_dir))
    env.chdir(wd_dir)

    load_env()

    assert os.environ["IG_STATE_PATH"] == "D:/tmp/from-wd.json"


def test_repo_dotenv_beats_workdir_dotenv(tmp_path, env):
    """The repo-root ``.env`` has higher priority than the CWD ``.env``."""
    repo_dir = tmp_path / "repo"
    wd_dir = tmp_path / "wd"
    repo_dir.mkdir()
    wd_dir.mkdir()
    (repo_dir / ".env").write_text("IG_DOWNLOAD_RETRIES=11\n", encoding="utf-8")
    (wd_dir / ".env").write_text("IG_DOWNLOAD_RETRIES=33\n", encoding="utf-8")
    env.delenv("IG_DOWNLOAD_RETRIES", raising=False)
    env.setenv("IG_REPO_ROOT", str(repo_dir))
    env.chdir(wd_dir)

    load_env()

    assert os.environ["IG_DOWNLOAD_RETRIES"] == "11"


def test_real_env_beats_both_dotenv_files(tmp_path, env):
    """A real environment variable still wins over both ``.env`` files."""
    repo_dir = tmp_path / "repo"
    wd_dir = tmp_path / "wd"
    repo_dir.mkdir()
    wd_dir.mkdir()
    (repo_dir / ".env").write_text("IG_DOWNLOAD_TIMEOUT=111\n", encoding="utf-8")
    (wd_dir / ".env").write_text("IG_DOWNLOAD_TIMEOUT=333\n", encoding="utf-8")
    env.setenv("IG_REPO_ROOT", str(repo_dir))
    env.chdir(wd_dir)
    env.setenv("IG_DOWNLOAD_TIMEOUT", "999")

    load_env()

    assert os.environ["IG_DOWNLOAD_TIMEOUT"] == "999"
