# insta-boards

> Local sync of **Instagram Saved Collections** into the filesystem.
> Built on top of [`instagrapi`](https://github.com/subzeroid/instagrapi) with
> cursor-based pagination, incremental resume and human-like throttling.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](#requirements)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![instagrapi](https://img.shields.io/badge/instagrapi-2.9.10-orange.svg)](https://github.com/subzeroid/instagrapi)
[![Repo](https://img.shields.io/badge/repo-nimblemo%2Finsta--boards-blueviolet.svg)](https://github.com/nimblemo/insta-boards)

`insta-boards` is a small, opinionated CLI for backing up every
**Saved Collection** of an Instagram account into a plain, human-readable
on-disk layout. It walks the official mobile API via `instagrapi`,
downloads each media item (single image, video, or carousel) and keeps an
incremental state file so re-runs only pick up new items.

The default output is a flat `data/raw/<slug>/` tree, where `<slug>` is
the transliterated collection name, and a JSON state file under
`data/state/instagram_sync.json` that tracks cursors, fetched items and
last-sync timestamps.

***

## Highlights

- **Full + incremental sync** of every Saved Collection (state-aware, JSON-on-disk, no DB).
- **Cursor-based pagination** that survives restarts and partial failures.
- **Human-like throttling**: log-normal delays, periodic micro- and session-breaks, optional User-Agent rotation.
- **Parallel carousel downloads** with a bounded thread pool that still respects the pacer.
- **Resilient HTTP layer**: shared `requests.Session` with `urllib3.Retry` for `connect`/`read`/`status` errors.
- **Idempotent re-runs**: per-item metadata + content-addressed files make `--resume` safe to interrupt.
- **Single CLI with four subcommands** — `sync`, `list boards`, `list items`, `download` — for full sync, inspection, and per-collection download.
- **Pure stdlib +** **`uvx`**: no global install, runs anywhere Python 3.11+ is available.

***

## Table of contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Configuration](#configuration)
  - [Authentication](#authentication)
  - [Network & HTTP retries](#network--http-retries)
  - [Parallel downloads](#parallel-downloads)
  - [Humanizer](#humanizer)
- [Output structure](#output-structure)
- [How it works](#how-it-works)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgments](#acknowledgments)

***

## Requirements

- **Python** ≥ 3.11
- **`uv`** / **`uvx`** for the recommended one-shot install
  ([install guide](https://docs.astral.sh/uv/getting-started/installation/))
- A working **Instagram** account (credentials, `sessionid` cookie, or
  pre-saved `instagrapi` settings — see [Authentication](#authentication))
- Outbound HTTPS to `i.instagram.com`, `scontent-*.cdninstagram.com` and
  the API endpoints used by `instagrapi`

The project is **Windows / macOS / Linux** friendly; the on-disk layout
uses forward slashes for cross-platform reproducibility.

***

## Installation

**From** **[PyPI](https://pypi.org/project/insta-boards/)** (recommended for users):

```bash
uv tool install insta-boards
insta-boards --help
```

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) (or substitute `pipx` / `pip` if you prefer).

For development, run from a local checkout using `uvx`, which creates a
temporary, isolated environment and does not pollute the global Python:

```bash
# From the repository root
uvx --from . insta-boards sync --dry-run
```

To install the project as a long-lived tool, use `uv tool`:

```bash
uv tool install .                # installs the `insta-boards` binary
insta-boards sync --dry-run      # available globally
```

For development, install in editable mode with all extras:

```bash
git clone https://github.com/nimblemo/insta-boards.git
cd insta-boards
uv sync                          # creates .venv with all dependencies
uv run insta-boards sync --dry-run
```

***

## Quick start

1. **Clone and configure credentials.** Create a `.env` file in the repo
   root (see [Configuration](#configuration) for the full list).

   The most reliable way to authenticate is via a browser `sessionid`
   cookie — log into [instagram.com](https://www.instagram.com/) in your
   browser, open DevTools → Application → Cookies → `https://www.instagram.com`
   and copy the value of the `sessionid` cookie. The password-only flow
   often fails for accounts that were created through Facebook or have
   2FA enabled.
   ```dotenv
   IG_USERNAME=your_login
   IG_PASSWORD=your_password
   IG_SESSIONID=your_sessionid_cookie_from_browser
   ```
2. **Preview the plan** (no API writes, no downloads):
   ```bash
   uvx --from . insta-boards sync --dry-run
   ```
3. **Run the full sync.** State, cursors and downloaded files are
   written under `data/`:
   ```bash
   uvx --from . insta-boards sync
   ```
4. **Re-run any time** — only new items are downloaded; the state file
   short-circuits items already known.

***

## Commands

The package exposes a single binary — `insta-boards` — with four
subcommands, all defined in `pyproject.toml` under `[project.scripts]`:

| Subcommand                 | Purpose                                                |
| -------------------------- | ------------------------------------------------------ |
| `insta-boards sync`        | Full sync of all collections (state-aware, resumable). |
| `insta-boards list boards` | JSONL of all collections (id, name, type, count).      |
| `insta-boards list items`  | JSONL of items in a single collection.                 |
| `insta-boards download`    | Download a single collection into `data/raw/<slug>/`.  |

The shape follows the kubectl / gh / aws / uv convention: a single binary
with verb-first subcommands, so users coming from those tools feel at
home. `insta-boards --help` lists every subcommand; each one also has
its own `--help`.

### `sync` — full sync

```bash
# Default: every collection on the account, resumes new items only
uvx --from . insta-boards sync

# Plan only, no API/state writes
uvx --from . insta-boards sync --dry-run

# Restrict to a known set of collections
uvx --from . insta-boards sync --collection 18427410172124759
uvx --from . insta-boards sync --collection 111,222
uvx --from . insta-boards sync --collection 111 --collection 222
uvx --from . insta-boards sync --collection-file sync-collection-list.txt

# Reset progress (keep the items, drop the cursor)
uvx --from . insta-boards sync --reset
uvx --from . insta-boards sync --reset-collection 18427410172124759

# Concurrency and humanizer toggles
uvx --from . insta-boards sync --concurrency 3
uvx --from . insta-boards sync --no-humanize

# Reporting and debugging
uvx --from . insta-boards sync --report-json logs/sync-report.json
uvx --from . insta-boards sync --print-state
```

Format of `sync-collection-list.txt` (one ID per line, comma-separated is
also accepted, lines starting with `#` are comments).

> **Default location.** When a `sync-collection-list.txt` file is present
> in the repository root, `sync` automatically uses it as the collection
> filter — you do not need to pass `--collection-file` explicitly. If
> neither `--collection`, `--collection-file` nor this default file is
> provided, the sync walks **all** collections on the account.

```text
# favorites
18143529535276037
18427410172124759,17974021309692829
17885180684721115
18040747580635287
17953107941571440
27452983781048115
2458445887976375
```

### `list boards` — inspect the account

```bash
uvx --from . insta-boards list boards           # walks every collection via cursor
uvx --from . insta-boards list boards --limit 50
```

### `list items` — inspect one collection

```bash
# Full walk
uvx --from . insta-boards list items --collection 18427410172124759

# Cap the number of items
uvx --from . insta-boards list items --collection 18427410172124759 --limit 20

# Resume from a previously-saved cursor
uvx --from . insta-boards list items --collection 18427410172124759 \
    --max-id "QV9fX0ZBS0VfQ1VSU09S" \
    --output-cursor .state/items.cursor.json
```

### `download` — download one collection

```bash
# Plain download of a single collection
uvx --from . insta-boards download --collection 18427410172124759

# Incremental: skip items that already have <pk>.json on disk
uvx --from . insta-boards download --collection 18427410172124759 --resume

# Resume from a saved cursor
uvx --from . insta-boards download --collection 18427410172124759 \
    --max-id "QV9fX0ZBS0VfQ1VSU09S" \
    --output-cursor .state/dwl.cursor.json --resume

# Override the directory name explicitly
uvx --from . insta-boards download --collection 18427410172124759 --name "Furniture"
```

***

## Configuration

All configuration is read from environment variables (and optionally a
`.env` file in the repo root, or the current working directory). Variables
not set fall back to safe defaults.

### Authentication

| Variable           | Purpose                                                                                  | Default                                   |
| ------------------ | ---------------------------------------------------------------------------------------- | ----------------------------------------- |
| `IG_USERNAME`      | Login (when no saved session is present).                                                | —                                         |
| `IG_PASSWORD`      | Password.                                                                                | —                                         |
| `IG_2FA_CODE`      | One-time TOTP code (if Instagram requires 2FA).                                          | —                                         |
| `IG_SESSIONID`     | `sessionid` cookie from web Instagram (alternative for "Log in with Facebook" accounts). | —                                         |
| `IG_PROXY`         | Proxy URL (`http://user:pass@host:port` or `socks5://host:port`).                        | —                                         |
| `IG_SETTINGS_PATH` | Path to the `instagrapi` session file.                                                   | `<repo>/secrets/instagrapi.settings.json` |
| `IG_STATE_PATH`    | Path to the JSON sync state file.                                                        | `<repo>/data/state/instagram_sync.json`   |

**Login order.** The CLI follows this precedence at startup:

1. If `IG_SETTINGS_PATH` exists — load the saved session.
2. If `IG_SESSIONID` is set — `login_by_sessionid()`, then save settings.
3. If `IG_USERNAME` / `IG_PASSWORD` are set:
   - with `IG_2FA_CODE` set — `login(..., verification_code=…)`,
   - otherwise plain `login()`,
   - then save settings.
4. If nothing is set — try the loaded session, otherwise a `LoginRequired` error is raised.

### Network & HTTP retries

| Variable              | Purpose                                                                             | Default |
| --------------------- | ----------------------------------------------------------------------------------- | ------- |
| `IG_DOWNLOAD_TIMEOUT` | Per-request HTTP timeout in seconds.                                                | `120`   |
| `IG_DOWNLOAD_RETRIES` | How many times to retry `connect` / `read` / `status`.                              | `5`     |
| `IG_DOWNLOAD_BACKOFF` | Exponential backoff multiplier between retries (`backoff * 2**n`).                  | `0.5`   |
| `IG_DOWNLOAD_DELAY`   | Base pause between **successful** downloads (sec) — median of the human-like curve. | `1.0`   |

### Parallel downloads

| Variable                  | Purpose                                            | Default |
| ------------------------- | -------------------------------------------------- | ------- |
| `IG_DOWNLOAD_CONCURRENCY` | Max simultaneous downloads inside a single item.   | `1`     |
| `IG_DOWNLOAD_POOL_REUSE`  | Reuse the singleton pool across calls (`1` / `0`). | `1`     |

### Humanizer

| Variable                    | Purpose                                             | Default |
| --------------------------- | --------------------------------------------------- | ------- |
| `IG_HUMANIZE`               | Enable / disable human-like simulation (`1` / `0`). | `1`     |
| `IG_HUMANIZE_SIGMA`         | Sigma of the log-normal pause distribution.         | `0.55`  |
| `IG_HUMANIZE_MIN`           | Minimum pause (sec).                                | `0.4`   |
| `IG_HUMANIZE_MAX`           | Maximum pause (sec).                                | `8.0`   |
| `IG_HUMANIZE_MICRO_EVERY`   | Insert a micro-break every N requests.              | `12`    |
| `IG_HUMANIZE_MICRO_MIN`     | Minimum micro-break (sec).                          | `2.5`   |
| `IG_HUMANIZE_MICRO_MAX`     | Maximum micro-break (sec).                          | `6.0`   |
| `IG_HUMANIZE_SESSION_EVERY` | Insert a session break every N requests.            | `80`    |
| `IG_HUMANIZE_SESSION_MIN`   | Minimum session break (sec).                        | `15.0`  |
| `IG_HUMANIZE_SESSION_MAX`   | Maximum session break (sec).                        | `45.0`  |
| `IG_USER_AGENT_ROTATE`      | Enable / disable User-Agent rotation (`1` / `0`).   | `0`     |

***

## Output structure

By default, the sync writes into the `data/` tree next to the repository
root. Slugs are produced by transliterating the original collection name
to ASCII (e.g. `Furniture` → `furniture`, `Textures: for home` →
`textures-for-home`).

```text
data/
├── raw/
│   └── <slug>/
│       ├── metadata.json          # collection index (fetched_at, items[])
│       ├── <pk>.json              # per-item metadata
│       └── <pk>_<idx>.<ext>       # media files (jpg, mp4, …) per carousel index
└── state/
    └── instagram_sync.json        # global sync state, one record per collection
```

`metadata.json` (one per collection) is written by `download`
and contains the `fetched_at` timestamp plus a flat list of items:

```json
{
  "source": "instagram",
  "fetched_at": "2026-08-27T19:42:01Z",
  "items": [
    { "item_id": "3234567890123456789", "source_url": "https://www.instagram.com/p/CxYzAbC/", "taken_at": 1693152121 }
  ]
}
```

`<pk>.json` holds the normalised per-item record (collection id, source
url, taken\_at, fetched\_at, media entries with type / url / index). See
`src.instagram_sync.media_entries` and
`src.cli.commands.list_items.normalize_media`
for the exact schema.

`instagram_sync.json` is the global sync state used by `insta-boards sync`:
per-collection cursor, `done` flag, `last_synced_at`, and the dictionary
of known items. State is written **after every item** so a network drop
mid-run never loses progress.

***

## How it works

```
┌──────────────┐     ┌────────────────┐     ┌──────────────┐
│ instagrapi   │ ──▶ │ Collection-    │ ──▶ │ DownloadPool │ ──▶ data/raw/<slug>/
│ Client (API) │     │ MediasPager    │     │ + Pacer      │
└──────────────┘     └────────────────┘     └──────────────┘
       │                     │                      │
       ▼                     ▼                      ▼
 instagrapi.Session   SyncState (JSON)     requests.Session + Retry
                      ← persisted after
                        every successful
                        item
```

- **`instagrapi.Client`** — handles login, 2FA, proxy and `sessionid`
  fallback. The session is dumped to `secrets/instagrapi.settings.json`
  on success so subsequent runs skip authentication.
- **`CollectionMediasPager`** — a small iterator that walks the cursor
  API (`more_available` / `next_max_id`) for one collection, yielding
  `Media` objects lazily and remembering `last_max_id` for resume.
- **`SyncState`** — a single JSON file under `data/state/`. The
  orchestrator reads the cursor and known items at start, calls the
  pager, and persists state after every successful item download.
- **`SessionPacer`** **(Humanizer)** — log-normal pause distribution with
  micro- and session-breaks. Thread-safe; consulted by every download.
- **`DownloadPool`** — bounded `ThreadPoolExecutor` for parallel
  carousel downloads. Every worker still goes through the pacer.

See [`src/humanizer.py`](src/humanizer.py) and
[`src/parallel.py`](src/parallel.py) for the design notes.

***

## Project layout

```text
.
├── pyproject.toml
├── README.md
└── src/
    ├── __init__.py
    ├── client.py            # auth, env loading, User-Agent rotation
    ├── humanizer.py         # SessionPacer + HumanizerConfig
    ├── instagram_sync.py    # state, pagers, HTTP session, raw layout
    ├── naming.py            # slug helpers
    ├── pagination.py        # iter_collections, CollectionMediasPager
    ├── parallel.py          # DownloadPool (bounded thread pool)
    ├── paths.py             # repo-root resolution
    └── cli/
        ├── app.py           # single entry point: insta-boards <subcommand>
        ├── _common.py       # shared argparse groups, pacer/pool setup
        └── commands/
            ├── sync.py          # insta-boards sync
            ├── list_boards.py   # insta-boards list boards
            ├── list_items.py    # insta-boards list items
            └── download.py      # insta-boards download
```

Runtime artefacts created at first run:

```text
secrets/
└── instagrapi.settings.json
logs/                            # only if --report-json is set
.state/                          # only if --output-cursor is set
data/
├── raw/<slug>/...
└── state/instagram_sync.json
```

***

## Troubleshooting

**"You can log in with your linked Facebook account"**
The password is not accepted because the account was created via
Facebook. Set `IG_SESSIONID` (the `sessionid` cookie from web
Instagram), set a separate IG password, or rotate the IP via `IG_PROXY`.

**2FA is requested at every run**
Set `IG_2FA_CODE` to the current TOTP from your Authenticator app and
re-run. The session is persisted to `secrets/instagrapi.settings.json`,
so subsequent runs should not ask for 2FA.

**`ProxyAddressIsBlocked`** **/** **`LoginRequired`**
Instagram flagged the current IP. Switch to a clean residential proxy
via `IG_PROXY` and re-run.

**A download stalls mid-collection**
The run can be safely interrupted (`Ctrl-C`) and resumed. Re-run the
same command — the state file already short-circuits all completed
items. If the cursor is stale, use `--reset` or
`--reset-collection <id>` to start that collection from the beginning.

**`fake-useragent`** **cannot download its database**
Rotation falls back to a small bundled pool of current desktop
User-Agents. Set `IG_USER_AGENT_ROTATE=0` to disable rotation entirely.

**Tests / CI need deterministic timing**
Pass `--no-humanize` to fall back to flat `IG_DOWNLOAD_DELAY` pauses.

***

## Roadmap

- Pluggable storage backends (S3-compatible, SQLite index) behind
  the `data/raw/<slug>/` layout.
- Configurable media-quality policy (highest vs. best per type).
- A `doctor` subcommand to validate `.env`, session and proxy.
- Optional `watch` mode: poll the Saved Collections endpoint on a
  schedule and run an incremental sync.
- A web UI for browsing the local mirror.

***

## Contributing

Issues and pull requests are welcome. For local development:

```bash
git clone https://github.com/nimblemo/insta-boards.git
cd insta-boards
uv sync
uv run insta-boards sync --dry-run
```

Please run the existing test suite (if any) and add a focused test for
new behaviour. Keep changes minimal and respect the existing module
boundaries (`client`, `humanizer`, `instagram_sync`, `parallel`,
`pagination`).

***

## Releasing

Releases are fully automated via GitHub Actions (`.github/workflows/`):

- **`ci.yml`** — runs on every push to `main` and on every pull request.
  Matrix-builds the sdist + wheel on Python 3.11 / 3.12 / 3.13 and
  smoke-tests every subcommand.
- **`release.yml`** — runs on a published GitHub Release, or manually
  via the *Run workflow* button. Builds the wheel, smoke-tests it, and
  pushes it to PyPI (or TestPyPI if you pick that target) using
  [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
  (OIDC). **No API tokens are stored in GitHub secrets.**

### One-time setup: register the trusted publisher on PyPI

Trusted Publishing means PyPI trusts a specific GitHub Actions workflow
to upload on your behalf, so you never have to copy a token. Register
it once, for both indexes:

1. **Create the project on PyPI** (first release only):
   - Go to <https://pypi.org/manage/projects/publish/> and reserve the
     name `insta-boards` (or click *publish* manually the first time
     to claim it).
2. **Register the GitHub Actions workflow as a** ***trusted publisher***:
   - On PyPI: <https://pypi.org/manage/account/publishing/> → *Add a
     new pending publisher* with:
     - **Owner**: `nimblemo`
     - **Repository**: `insta-boards`
     - **Workflow filename**: `release.yml`
     - **Environment name**: `pypi`
   - Repeat for TestPyPI: <https://test.pypi.org/manage/account/publishing/>
     with environment name `testpypi`.
3. **Create the matching environments in GitHub**:
   - *Settings → Environments → New environment* → name it `pypi` (and
     `testpypi`). Optional: add protection rules (required reviewers,
     branch restrictions) so a stray tag cannot publish.

### Cutting a release

1. Bump `version` in `pyproject.toml` (e.g. `0.1.0` → `0.2.0`).
2. Commit and push: `git commit -am "release: v0.2.0" && git push`.
3. *GitHub → Releases → Draft a new release* → tag `v0.2.0` against
   `main` → *Publish release*.
4. The `Release` workflow picks it up, builds, smoke-tests, and uploads
   the wheel + sdist to PyPI.

For a dry-run against TestPyPI first, use *Actions → Release → Run
workflow → target = testpypi*.

### Verifying a release

```bash
# published wheel — installed in a throw-away venv
uv tool install insta-boards
insta-boards --help
```

***

## License

[MIT](LICENSE) — see the [LICENSE](LICENSE) file for the full text.

***

## Acknowledgments

- [`subzeroid/instagrapi`](https://github.com/subzeroid/instagrapi) —
  the unofficial Instagram API client that powers every request.
- [`fake-useragent`](https://pypi.org/project/fake-useragent/) — the
  optional User-Agent provider used when available.
- [`uv`](https://github.com/astral-sh/uv) — the package manager used
  to ship a single `uvx --from .` entry point.

