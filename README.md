# insta-boards

Local sync of **Instagram Saved Collections** into the filesystem.
Built on top of [`instagrapi`](https://github.com/subzeroid/instagrapi) with
cursor-based pagination, incremental resume and human-like throttling.

***

## Quick start

```bash
uvx --from . sync-instagram
```

### CLI commands

| Command               | Purpose                                              |
| --------------------- | ---------------------------------------------------- |
| `sync-instagram`      | Full sync of all collections (state-aware)           |
| `list-collections`    | JSONL of all collections (id, name, type, count)     |
| `list-items`          | JSONL of items in a single collection                |
| `download-collection` | Download a single collection into `data/raw/<slug>/` |

***

## Usage examples

```bash
# === Full sync (default) ===
uvx --from . sync-instagram                               # all collections, resumes new ones
uvx --from . sync-instagram --dry-run                     # plan only, no API/state writes

# === Filtering collections (--collection / --collection-file) ===
uvx --from . sync-instagram --collection 18427410172124759
uvx --from . sync-instagram --collection 18427410172124759,18143529535276037
uvx --from . sync-instagram --collection 111 --collection 222
uvx --from . sync-instagram --collection-file sync-collection-list.txt

# sync-collection-list.txt format: one ID per line
# (or comma-separated); lines starting with `#` are comments:
#   18143529535276037
#   18427410172124759,17974021309692829   # comma-separated works too
#   17885180684721115
#   18040747580635287
#   17953107941571440
#   27452983781048115
#   2458445887976375

# === Resetting progress ===
uvx --from . sync-instagram --reset                       # cursor/done for ALL collections
uvx --from . sync-instagram --reset-collection 18427410172124759

# === Inspection: list collections ===
uvx --from . list-collections
uvx --from . list-collections --limit 50

# === Inspection: items of one collection ===
uvx --from . list-items --collection 18427410172124759
uvx --from . list-items --collection 18427410172124759 --limit 20
uvx --from . list-items --collection 18427410172124759 \
    --max-id "QV9fX0ZBS0VfQ1VSU09S" \
    --output-cursor .state/list-items.cursor.json

# === Download a single collection ===
uvx --from . download-collection --collection 18427410172124759
uvx --from . download-collection --collection 18427410172124759 --resume
uvx --from . download-collection --collection 18427410172124759 \
    --max-id "QV9fX0ZBS0VfQ1VSU09S" \
    --output-cursor .state/dwl.cursor.json --resume
uvx --from . download-collection --collection 18427410172124759 --name "Furniture"

# === Parallel downloads and human-like mode ===
uvx --from . sync-instagram --concurrency 3               # 3 carousel files in parallel
uvx --from . sync-instagram --no-humanize                 # flat pauses (CI/tests)

# === Reporting and debugging ===
uvx --from . sync-instagram --report-json logs/sync-report.json
uvx --from . sync-instagram --print-state
```

<br />

### Filtering collections: `--collection` / `--collection-file`

**Three forms** are supported — they are merged into a single deduplicated list:

| Form                  | Example                                      |
| --------------------- | -------------------------------------------- |
| One collection        | `--collection 18427410172124759`             |
| Comma-separated list  | `--collection 111,222,333`                   |
| Repeat `--collection` | `--collection 111 --collection 222`          |
| File with a list      | `--collection-file sync-collection-list.txt` |

***

## Configuration via `.env`

<br />

| Variable           | Purpose                                                                                |
| ------------------ | -------------------------------------------------------------------------------------- |
| `IG_USERNAME`      | login (when no saved session is present)                                               |
| `IG_PASSWORD`      | password                                                                               |
| `IG_2FA_CODE`      | one-time TOTP code (if Instagram requires 2FA)                                         |
| `IG_SESSIONID`     | `sessionid` cookie from web Instagram (alternative for "Login with Facebook" accounts) |
| `IG_PROXY`         | proxy (`http://user:pass@host:port` or `socks5://host:port`)                           |
| `IG_SETTINGS_PATH` | path to the instagrapi session file. Default `<repo>/secrets/instagrapi.settings.json` |
| `IG_STATE_PATH`    | path to the JSON state file. Default `<repo>/data/state/instagram_sync.json`           |

### Network and retries (HTTP)

| Variable              | Purpose                                                                    | Default |
| --------------------- | -------------------------------------------------------------------------- | ------- |
| `IG_DOWNLOAD_TIMEOUT` | per-request HTTP timeout in seconds                                        | `120`   |
| `IG_DOWNLOAD_RETRIES` | how many times to retry `connect`/`read`/`status`                          | `5`     |
| `IG_DOWNLOAD_BACKOFF` | exponential backoff multiplier between retries (`backoff * 2**n`)          | `0.5`   |
| `IG_DOWNLOAD_DELAY`   | base pause between SUCCESSFUL downloads (sec) — median of human-like curve | `1.0`   |

&#x20;

### Parallel downloads (concurrency)

| Variable                  | Purpose                                         | Default |
| ------------------------- | ----------------------------------------------- | ------- |
| `IG_DOWNLOAD_CONCURRENCY` | max simultaneous downloads inside a single item | `1`     |
| `IG_DOWNLOAD_POOL_REUSE`  | reuse the singleton pool across calls (`1`/`0`) | `1`     |

**Humanizer**

| Variable                    | Purpose                                        | Default |
| --------------------------- | ---------------------------------------------- | ------- |
| `IG_HUMANIZE`               | enable/disable human-like simulation (`1`/`0`) | `1`     |
| `IG_DOWNLOAD_DELAY`         | base (median) pause between requests (sec)     | `1.0`   |
| `IG_HUMANIZE_SIGMA`         | sigma of the log-normal distribution           | `0.55`  |
| `IG_HUMANIZE_MIN`           | minimum pause (sec)                            | `0.4`   |
| `IG_HUMANIZE_MAX`           | maximum pause (sec)                            | `8.0`   |
| `IG_HUMANIZE_MICRO_EVERY`   | how often to insert a micro-break (requests)   | `12`    |
| `IG_HUMANIZE_MICRO_MIN`     | minimum micro-break (sec)                      | `2.5`   |
| `IG_HUMANIZE_MICRO_MAX`     | maximum micro-break (sec)                      | `6.0`   |
| `IG_HUMANIZE_SESSION_EVERY` | how often to insert a session break (requests) | `80`    |
| `IG_HUMANIZE_SESSION_MIN`   | minimum session break (sec)                    | `15.0`  |
| `IG_HUMANIZE_SESSION_MAX`   | maximum session break (sec)                    | `45.0`  |
| `IG_USER_AGENT_ROTATE`      | enable/disable User-Agent rotation (`1`/`0`)   | `0`     |

***

## Authentication rules

The scripts follow this login order:

1. If `IG_SETTINGS_PATH` exists — load the saved session.
2. If `IG_SESSIONID` is set — `login_by_sessionid()`, then save settings.
3. If `IG_USERNAME`/`IG_PASSWORD` are set:
   - with `IG_2FA_CODE` set — `login(..., verification_code=…)`,
   - otherwise plain `login()`,
   - then save settings.
4. If nothing is set — try the loaded session, otherwise a `LoginRequired` error is raised.

**Common issues:**

- "You can log in with your linked Facebook account" → use
  `IG_SESSIONID` (the `sessionid` web cookie) or set a separate IG
  password, or rotate the IP via `IG_PROXY`.
- 2FA → set `IG_2FA_CODE` (TOTP from your Authenticator) and re-run.

