# Devin Session Dashboard

A minimalist, offline dashboard for Devin Desktop's cached session metadata and account quota. It uses the local SQLite `state.vscdb` database, no third-party dependencies.

## Run

macOS:

```sh
python3 devin_dashboard.py
```

Windows:

```bat
py devin_dashboard.py
```

The app opens `http://localhost:8787` automatically. Keep the terminal running while using the dashboard and press `Ctrl-C` to stop it.

Useful options:

```sh
python3 devin_dashboard.py --no-browser
python3 devin_dashboard.py --port 9000
python3 devin_dashboard.py --db "/path/to/state.vscdb"
```

The database is auto-detected at:

- macOS: `~/Library/Application Support/Devin/User/globalStorage/state.vscdb`
- Windows: `%APPDATA%\\Devin\\User\\globalStorage\\state.vscdb`, then `%LOCALAPPDATA%`
- Linux: `~/.config/Devin/User/globalStorage/state.vscdb`

## Dashboard features

- Searchable, sortable session metadata table.
- Session title, provider, workspace, user-message count, per-session ACU consumed, estimated cost, input/output/cached token counts, active runtime, model generation time, created time, and updated time.
- ACU consumed and monthly limit, billing window, and overage balance.
- Current-month totals: session count, tokens in, tokens out, and top models by tokens, ACU, and session count.
- Optional estimated dollar spend. Enter a local `$ / ACU` price (default 1.25) and a monthly budget (default $75, which equals 60 ACU at the default price); the monthly budget drives the ACU limit. Both values are saved only in the browser's local storage.
- Manual refresh and optional 60-second auto-refresh.
- Per-row share menu: **Copy JSON** copies the full session record (pretty-printed, including the estimated cost) to the clipboard, and **Download .md file** saves the session details (with the estimated cost) as a Markdown file named after the title and created date.

## Where the data comes from

All data is read at request time from two local SQLite databases, both opened read-only:

- **Devin Desktop state** — `state.vscdb`, a VS Code-style key/value store whose `ItemTable` holds JSON blobs keyed by strings such as `windsurf.acp.sessioninfo.<sessionId>`.
- **Devin CLI sessions** — `~/.local/share/devin/cli/sessions.db` (override with `--cli-db`), whose `message_nodes` table stores one row per chat message with a `chat_message` JSON column.

The server merges both sources in `read_state()` and exposes the result as a single JSON payload at `GET /api/data`. The browser then renders everything client-side from that payload; no computation happens against a remote service.

### Dashboard cards

- **Plan card** — Plan name comes from the cached plan blob (`windsurf.reactSettings.cachedPlanInfoData*`, falling back to `windsurf.settings.cachedPlanInfo`) in `state.vscdb`. Estimated cost is computed in the browser as `ACU consumed × $/ACU` using the locally configured price.
- **ACU consumed card** — Two numbers are compared: the *server* ACU (`plan.consumed`) and the *local* ACU. The server value is taken from the `windsurfAuthStatus` blob: its `userStatusProtoBinaryBase64` field is base64-decoded and parsed as a raw protobuf message, reading fixed64 field 19 (ACU consumed) and field 20 (ACU limit); these override the cached-plan values when present. The local ACU is aggregated server-side from `sessions.db` by summing `metadata.committed_acu_cost` over all deduplicated messages whose `created_at` falls inside the plan's billing window (derived from the plan blob's `startTimestamp`/`endTimestamp`). The card displays `max(server, local)` and shows the difference as "other". The progress bar and percentage use `plan.limit`.
- **Sessions this month card** — Computed entirely in the browser: sessions whose `createdAt` (from the session-info blob's `_meta["cognition.ai/createdAt"]` or the `windsurf.acp.session/session_start_time/<id>` key) falls in the current calendar month. Token totals sum `inputTokens`/`outputTokens` over those sessions.
- **Model metrics card** — Also browser-side, over the same current-month sessions. The model per session comes from `sessions.db`: the `sessions.model` column, overridden by the most recently observed `metadata.generation_model` in that session's messages.

### Session table

Each row is one session, assembled by joining two sources on the session slug (the last `/`-separated segment of the session ID):

- **From `state.vscdb`**: `windsurf.acp.sessioninfo.<id>` provides title, provider (explicit `providerId` or parsed from the ID), and `cwd`; `info.updatedAt` provides Last updated; `_meta["cognition.ai/createdAt"]` (or the session-start-time key) provides Created; `windsurf.acp.session/userMessageCount/<id>` (falling back to `_meta["cognition.ai/userMessageCount"]`) provides the user-message count; `windsurfSpace.sessionWorkspace/<id>` provides the workspace label (or the basename of `cwd`).
- **From `sessions.db`**, aggregated per session with a SQL query over `message_nodes` (deduplicated by `message_id`): ACU (`SUM` of `metadata.committed_acu_cost`), tokens in/out (`SUM` of `metrics.input_tokens` / `metrics.output_tokens`), model generation time (`SUM` of `metrics.total_time_ms`), and active runtime. Active runtime sums the gaps between consecutive messages, capped at one hour each, and skips gaps preceding user messages, so idle time between sessions of work is excluded. Sessions with no local CLI rows (e.g. `devin-cloud`) show `—` for these columns.
- **Estimated cost column** — computed in the browser as `session ACU × $/ACU`; it is a local estimate, not a billing record.

Server-side, sessions whose title is `(untitled)` with zero user messages are hidden, and the list is sorted by last-updated time. The browser then applies your search text (matched against title, directory, session ID, provider, workspace, and model) and column sorting on top of that list.

## Privacy and limitations

- The server binds to `localhost` by default and serves no external assets.
- The app opens the SQLite database read-only and never reads `windsurfAuthStatus`, API keys, or credentials.
- Per-session ACU, token usage, active runtime, and model generation time are read from the local Devin CLI session store (`~/.local/share/devin/cli/sessions.db`, override with `--cli-db`). Input and output tokens are summed from `metrics.input_tokens` and `metrics.output_tokens`; cached tokens combine `cache_read_tokens` and `cache_creation_tokens`. Duplicate message snapshots are deduplicated by message ID. Active runtime excludes gaps before user messages and caps any single gap at one hour; model generation time sums `metrics.total_time_ms`. Sessions without local CLI data (for example `devin-cloud` sessions) show `—`. The dollar amount is an estimate based on the price you enter.
- Session conversation transcripts and tool details are not stored in the metadata records used by this dashboard.
- Cached values can lag behind the live Devin service; the page displays the local data timestamp.
