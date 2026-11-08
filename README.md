# Garmin Connect MCP

Remote MCP server for Garmin Connect. Gives AI assistants access to your Garmin health and activity data over the internet — no local server or app required.

Built on top of [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) and [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect).

## What makes this different from Taxuspt/garmin_mcp

Taxuspt's original server runs locally on your machine and requires a locally installed AI desktop client. This version runs as a cloud service, protected by GitHub OAuth, so it works from any MCP-compatible AI assistant on any device — without running anything locally.

## What it does

Exposes ~150 Garmin Connect tools so AI assistants can query your health data directly:

- Health & Wellness: sleep, HRV, stress, body battery, heart rate, SpO2, respiration, recovery time
- Activities: runs, rides, swims — with splits, weather, HR zones, FIT file analysis
- Training: readiness, status, VO2max, load, running tolerance, acclimation, trends
- Body composition, weight, hydration, energy balance
- Workouts (incl. builders and Garmin Coach), gear, nutrition, courses, calendar events, challenges

The tool modules are kept in sync with
[Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp); this repo adds the remote
server layer (GitHub OAuth, persistent token stores, filesystem-tool block).

### Choosing which tools are exposed

Every tool definition is sent to the AI assistant with each conversation, so ~150 tools
cost noticeably more context than a focused set. Use `GARMIN_ENABLED_TOOLS` (allowlist)
or `GARMIN_DISABLED_TOOLS` (denylist) — comma-separated tool names — to expose only what
you need. Unknown names are logged at startup.

### Blocked filesystem tools

`download_activity_file`, `download_course_gpx`, `set_fit_download_dir` and
`upload_course` read or write arbitrary paths on the server. That is fine for a local
single-user install, but on a remote server one (possibly prompt-injected) call could
overwrite the OAuth token store, the Garmin token or the application code. They are
never registered here, whatever the tool filter says.

## How it works

```
AI assistant → /authorize → GitHub login (+ 2FA) → /auth/callback
             → username verified → MCP access token issued → MCP connection
```

Access is protected by **GitHub OAuth** — only the GitHub account set in
`GITHUB_ALLOWED_USER_ID` can authenticate. GitHub login with 2FA is required
once every 30 days; tokens are persisted to a token store (file or Redis). No credentials are stored
in the AI assistant's configuration.

1. The AI assistant detects the MCP server requires OAuth
2. A browser window opens — you log in to GitHub with 2FA
3. The server verifies your GitHub account matches `GITHUB_ALLOWED_USER_ID`
4. The AI assistant receives a 30-day access token and a 30-day refresh token

> **Restart note:** OAuth tokens are persisted to the token store, so
> the AI assistant does **not** need to re-authenticate after a container
> restart. The `_pending` OAuth state is in-memory only — if the container
> restarts mid-login flow, just click Connect again.

## Deploy your own

You will need:

- A Docker host reachable over HTTPS (any VPS or container platform)
- Persistent storage for OAuth tokens: a mounted volume (recommended on your own
  server) or, on platforms without persistent disks, an Upstash Redis database
- A GitHub OAuth App for authentication

### Step 1 — Token store

On a server with persistent storage, set `TOKEN_STORE_FILE` to a path inside a
mounted volume (e.g. `/state/token_store.json`). The file is written atomically
with owner-only permissions — keep the volume outside any git checkout, it
contains live access tokens.

On platforms without persistent disks, create an Upstash Redis database instead
and note its **REST URL** and **REST token**. Free databases may be deleted after
a period of inactivity — prefer the file store when you can.

### Step 2 — GitHub OAuth App (one-time)

Create a GitHub OAuth App at **Settings → Developer settings → OAuth Apps**:

- **Application name:** anything (e.g. `My MCP Servers`)
- **Homepage URL:** `https://your-service-url`
- **Callback URL:** `https://your-service-url/auth/callback`

Note the **Client ID** and generate a **Client Secret**.

### Step 3 — Garmin token

Run `generate_token.py` locally to obtain your Garmin token.

> **Important:** You must run this from inside the project folder, and you must
> use `uv run` — not `python3`. `uv run` uses the project's own virtual
> environment, which installs the exact same library versions as your deployment.
> Using `python3` directly may use a different version on your machine, producing
> a token format the server can't read.

```bash
cd /path/to/garmin-connect-mcp   # must be in this folder
~/.local/bin/uv run --python 3.12 python generate_token.py
```

The script will prompt for your Garmin email, password, and MFA code. On
success it writes the base64-encoded token to **`token.txt`** in the project
folder — do not copy from the terminal (the long base64 string wraps and truncates).

Then provide the token to the server in one of two ways:

- **Token directory (recommended):** decode it into a file named
  `garmin_tokens.json` and mount its directory into the container at
  `/root/.garminconnect`. `garminconnect` writes refreshed tokens back to this
  file, so the session survives restarts.
  ```bash
  mkdir -p ./garmin-tokens && base64 -d < token.txt > ./garmin-tokens/garmin_tokens.json
  ```
- **Environment variable:** paste the contents of `token.txt` into
  `GARMINTOKENS_BASE64`. On Render, configure Upstash as described below so
  refreshed tokens survive restarts. Without Upstash, this value remains a
  fixed bootstrap and updated tokens are lost when the process stops.

Delete `token.txt` afterwards — it contains your Garmin session.

### Step 4 — Deploy

1. Fork this repo
2. Build and run the Docker image, e.g.
   ```bash
   docker build -t garmin-connect-mcp .
   docker run -d --env-file .env -p 8000:8000 \
     -v "$PWD/garmin-tokens:/root/.garminconnect" \
     -v "$PWD/state:/state" -e TOKEN_STORE_FILE=/state/token_store.json \
     garmin-connect-mcp
   ```
3. Put it behind an HTTPS reverse proxy and set `SERVER_URL` to the public URL
4. Set the environment variables listed below (in `.env` — never commit it)

### Step 5 — Connect to your AI assistant

In your MCP-compatible AI assistant, add this server as a remote MCP connection:

- **URL:** `https://your-service-url/mcp`
- Authentication: leave empty — the server handles OAuth automatically

**For Claude:** paste the URL into the connector dialog at [claude.ai](https://claude.ai). Claude Desktop and mobile sync automatically from the web connector.

## Configuration reference

| Env var | Required | Rotates | Description |
|---|---|---|---|
| `GARMINTOKENS_BASE64` | — | Bootstrap only | Base64 DI session JSON from `generate_token.py`, used only when no Garmin Redis record exists |
| `GARMIN_TOKEN_STORE_KEY` | — | Never | Dedicated Garmin Redis key (default `mcp:garmin:credentials`); must differ from `TOKEN_STORE_KEY` |
| `GITHUB_CLIENT_ID` | ✅ | Never | GitHub OAuth App client ID |
| `GITHUB_CLIENT_SECRET` | ✅ | Never | GitHub OAuth App client secret |
| `GITHUB_ALLOWED_USER_ID` | ✅ | Never | Immutable numeric GitHub user ID allowed to connect (preferred) |
| `GITHUB_ALLOWED_USER` | — | Never | GitHub username — legacy fallback, used only if `GITHUB_ALLOWED_USER_ID` is unset |
| `SERVER_URL` | ✅ | Never | Public base URL of this service |
| `TOKEN_STORE_FILE` | ✅* | Never | Path of the OAuth token store file (in a mounted volume); takes precedence over Redis |
| `UPSTASH_REDIS_REST_URL` | ✅* | Never | HTTPS REST endpoint, used for Garmin persistence and, unless a file store is set, Claude OAuth |
| `UPSTASH_REDIS_REST_TOKEN` | ✅* | Never | Upstash Redis REST token |
| `TOKEN_STORE_KEY` | — | Never | Redis key for the OAuth token store (default `mcp:garmin:token_store`) |
| `PORT` | — | Never | Listen port inside the container (default `8000`) |
| `GARMIN_IS_CN` | — | — | Set `true` for Garmin Connect China |
| `GARMIN_ENABLED_TOOLS` | — | — | Comma-separated allowlist of tool names; if set, only these are exposed |
| `GARMIN_DISABLED_TOOLS` | — | — | Comma-separated denylist (ignored when an allowlist is set) |
| `GARMIN_MCP_CALL_TIMEOUT` | — | — | Seconds before a stalled Garmin request is abandoned (default `90`, `0` disables) |

\* Set either `TOKEN_STORE_FILE` or both Upstash variables. Without either, tokens
are kept in memory only and every restart requires reconnecting the AI assistant.

## Garmin token persistence on Render Free

Render Free has an ephemeral filesystem and cannot attach a persistent disk.
Use the existing Upstash database with separate keys for the two kinds of token:

| Setting | Purpose |
|---|---|
| `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` | Existing database REST connection; token must allow GET and SET |
| `TOKEN_STORE_KEY` | Claude OAuth state; keep its current value (default `mcp:garmin:token_store`) |
| `GARMIN_TOKEN_STORE_KEY` | Garmin DI credentials; default `mcp:garmin:credentials` |
| `GARMINTOKENS_BASE64` | Initial seed only, generated with the pinned `garminconnect==0.3.6` |

On Render Free, leave `TOKEN_STORE_FILE` unset: a local file would override Redis
for Claude OAuth and disappear when the service restarts. It does not override
Redis for Garmin credentials. Keep these values in Render Environment settings,
never in source control or chat. The database contains bearer credentials;
restrict access to its REST token and use a separate Garmin key for each service
and Garmin account, including previews. Do not let two instances rotate the same
Garmin session concurrently: the lock is per process, not distributed.

At startup, the server reads the Garmin key first. Only an absent key permits
bootstrap from the environment (or the local token file). A store outage or
invalid saved record stops startup rather than silently restoring an old token.
The bootstrap is saved before the first API call. DI token rotation is saved
immediately, even if a later profile request fails. Unchanged state causes no
Redis write. GET/SET failures have one bounded transport retry; a failed save
keeps the new state in memory, and the next call retries the save before making
another Garmin request. Application code adds no replay of Garmin writes; the
pinned library retains its existing HTTP-401 retry behavior.

The server reports access-token expiry when available; **refresh-token expiry is
unknown**. Version 0.3.6 uses native DI tokens, not the older Garth OAuth format.
There is no background refresh while Render is spun down. Restart recovery can
refresh a still-valid session on demand; this cannot revive a revoked/expired
refresh token or bypass MFA or Garmin anti-bot restrictions.

### Manual reauthentication, only when required

1. Run `generate_token.py` locally using the project version as in Step 3.
   Do not print or paste the token into a terminal log, issue, PR, or chat.
2. With deployment approval, replace `GARMINTOKENS_BASE64` in Render's secret
   settings and choose a **new, unused** `GARMIN_TOKEN_STORE_KEY`, distinct from
   the Claude OAuth key. This intentionally bootstraps a fresh Garmin session
   without overwriting or discarding the Claude OAuth store. Merely changing the
   environment token does not override an existing Garmin record.
3. Redeploy with approval, verify login, and delete `token.txt`. Remove obsolete
   Garmin keys from Upstash when no instance uses them. Never delete the Claude
   OAuth key as part of Garmin renewal.

### Validation and operational limits

`pytest tests/unit tests/integration` runs with mocked Garmin HTTP and Redis;
it verifies rotation, restart restoration with a stale bootstrap, failure
classification, pending-save recovery, and existing Claude OAuth persistence.
It does not access your Garmin account or production Redis. After approving a
production deployment, make a read-only MCP call, restart the service, then
repeat the call with the original bootstrap unchanged. Inspect only fixed error
codes and success metadata; never export the token record to logs.

There is a small unavoidable gap between Garmin issuing a rotated token and
Redis acknowledging it. A crash in this gap, an extended Redis outage, expired
sessions, or loss/eviction of the Redis record may require manual reauthentication.
Free tier limits, cold starts and provider changes remain applicable. OAuth
callback access logging is disabled in the application so one-time codes are
not included in Uvicorn request logs; infrastructure logging is managed by the
hosting provider.

## Architecture

- **Transport:** Streamable HTTP (MCP 1.x) via FastMCP + uvicorn
- **Resilience:** every Garmin call runs with a timeout, so one stalled request cannot hang the server
- **Auth:** GitHub OAuth 2.0 — server acts as Authorization Server, GitHub as Identity Provider
- **User restriction:** GitHub user ID verified against `GITHUB_ALLOWED_USER_ID` on every login (immutable; `GITHUB_ALLOWED_USER` is a legacy username fallback)
- **Token lifetime:** 30-day access token, 30-day refresh token (rotated on each refresh)
- **Token persistence:** `TOKEN_STORE_FILE` (atomic, 0600) or Upstash Redis — tokens survive container restarts
- **Garmin auth:** OAuth via `garminconnect` (pinned in `pyproject.toml` — the
  token format can change between releases, so upgrade deliberately and generate
  tokens with the same version)

## Contributing

This code was built with AI assistance ([Claude Code](https://claude.ai/code)) — vibe-coded with the best intentions. Security has been a priority throughout, but the code has not been independently audited. Use it at your own risk. If you spot a bug, a vulnerability, or an opportunity to improve anything, issues and pull requests are very welcome.

## Credits

- [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) — original local MCP server this remote version was adapted from (MIT)
- [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect) — Python library powering Garmin Connect API access
- Built with [Claude Code](https://claude.ai/code)
