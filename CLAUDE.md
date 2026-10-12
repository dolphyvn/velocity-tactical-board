# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Velocity Tactical Board" v2: a tactical whiteboard for small-sided football coaches. Coaches sign up, manage teams and squads, draw formations and actions on a pitch, save boards and scenario snapshots, share read-only links, and request LLM tactical analysis of a board.

Stack: a single stdlib `http.server` + SQLite backend (`v2/server_v2.py`) and vanilla HTML/JS frontends with inline CSS and JS. There is no build step, package manifest, linter, or test suite.

## Commands

```bash
pip install -r v2/requirements.txt          # only `cryptography`; optional, but needed to save LLM API keys

VLFC_V2_MASTER_KEY='<long-random-secret>' python3 v2/server_v2.py
# serves the API on http://127.0.0.1:3020 and creates the SQLite schema on startup

curl http://127.0.0.1:3020/api/v2/health    # smoke test

cd v2 && python3 -m unittest test_server_v2 -v   # unit tests (stdlib only; no server needed)
```

Environment variables read by the server:
- `VLFC_V2_HOST` (default `127.0.0.1`), `VLFC_V2_PORT` (default `3020`)
- `VLFC_V2_DB` (default `v2/vlfc-v2.sqlite3`)
- `VLFC_V2_MASTER_KEY`: the key used to Fernet-encrypt coaches' LLM API keys. Without it, saving an LLM key fails.
- `VLFC_V2_COOKIE_SECURE` (default `1`): session cookies are `Secure`. Set it to `0` when you test over plain http on anything other than localhost.
- `VLFC_V2_SESSION_DAYS` (default `14`)

The repo does not include a static file server. The HTML pages use root-absolute URLs (`/api/v2/...`, `/v2.html`, `/v2-board.html`), so they must be served from the same origin as the API, for example through a reverse proxy.

There are no lint, test, or build commands. Verify backend changes by running the server and calling endpoints with curl. Verify frontend changes in a browser against the running server.

## Backend architecture (`v2/server_v2.py`, one file)

- **Routing:** a single `Handler(BaseHTTPRequestHandler)` dispatches on `path_parts()` with explicit list comparisons in `do_GET`, `do_POST`, `do_PUT`, and `do_DELETE`. There is no router object. Add new routes by matching the same pattern in the right method handler.
- **Public routes:** `/api/v2/health`, `/api/v2/share/{token}`, and `/api/v2/scenario-share/{token}` do not require login. Everything else calls `require_user()`.
- **CSRF guard:** every non-GET request must send `X-VLFC-Request: v2`. `require_mutation_header()` enforces it, and the frontends' `api()` helper sets it. Any new mutating route needs the same check.
- **Auth:** passwords use PBKDF2-SHA256 (310k iterations). The session token is random, and only its SHA-256 is stored in `sessions`. The cookie is `vlfc_v2_session`, HttpOnly and SameSite=Lax.
- **Data model:** `users` → `teams` → `players`; `users` → `boards` (optional `team_id`) → `scenarios`. A board's whole pitch state is one `state_json` blob. A scenario is a snapshot of that blob with its own `share_token`.
- **Board state gate:** `clean_board_state()` is the only write path for board and scenario JSON. It whitelists fields, clamps lengths, limits actions (200) and frames (100), and increments `version` on every save. Any new field must be added there, or it will be silently dropped.
- **Coordinates:** horizontal pitch is 1200×700, vertical is 700×1000 (these numbers appear in `TACTICAL_PROMPT` too).
- **LLM analysis:** `POST /api/v2/boards/{id}/analyse` → `analyse_with_settings()`. Each coach stores provider, base URL, model, and auth mode in `llm_settings`. The default base URL is `https://api.anthropic.com/v1`. Base URL rules (`validate_base_url`):
- Must be `https`. Hosts that resolve to private, loopback, link-local or other non-global addresses are refused, unless listed in `VLFC_V2_ALLOWED_LLM_HOSTS` (comma-separated).
- Redirects are not followed, so the API key is never re-sent to another host.
- Changing the base URL clears the saved key unless a new one is supplied in the same request.
- Upstream error bodies are logged on the server only.
- Known residual gap: the host is resolved at save and at each analyse call, not pinned to the connection, so DNS rebinding between the check and the request is not fully prevented. Provider `anthropic` posts to `{base}/messages`, and provider `openai` posts to `{base}/responses`. `router_parse()` tolerates SSE-style or multi-JSON responses. The model must return the JSON shape defined in `TACTICAL_PROMPT`.

## Frontend

- `index.html`: login and signup, plus the board list and team/squad management.
- `v2-board.html`: the pitch editor. Its URL mode decides what it does:
  - `?board=ID` opens an owned board. Without a session it redirects to `/v2.html?next=...`.
  - `?share=TOKEN` opens a read-only viewer.
  - `?scenario=TOKEN` opens a read-only scenario viewer.
- Each HTML file is self-contained and has its own copy of the `api()` helper. Shared code is not extracted, so changes to one page's helper do not reach the others.
- The root `index.html` and `v2-board.html` are the current versions. The `v2/*.html` copies are older: they lack scenario support and the `?next=` login redirect. Confirm with the user before editing `v2/*.html`.

## Known issues

- **Scenario update is unreachable.** `v2-board.html` `overwriteScenario()` sends `PUT /api/v2/scenarios/{id}`. The update branch sits inside `do_GET` (around line 605), and the GET branch above it matches first. `do_PUT` has no scenario route, so the PUT returns 404. Fix by moving the handler into `do_PUT`.
- **`v2-board.html` links to `/v2.html`**, which exists only under `v2/`. Links break if the repo root is served as-is.
- **The SQLite database must never be committed.** It holds coach emails, password hashes and encrypted LLM keys. It was removed from history, but there is no `.gitignore` yet, so `git add -A` would re-add it. Add one before committing.
- **Legacy v1 files** (`server.py`, `server_anthropic.py`, `board.html`, `friday.html`, `board-backup.html`, `index-backup.html`) are deleted in the working tree but still in git history (commit `301fbcd`). Do not restore them without asking.
