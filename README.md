# Velocity Tactical Board

A tactical whiteboard for small-sided football. Coaches sign up, manage a squad, draw formations and actions on a pitch, save boards and scenarios, share read-only links, and optionally ask an LLM to review a board.

- Drag players and the ball around a horizontal or vertical pitch
- Draw passes, runs, dribbles, shots and presses
- Make animated frames to show how a move should flow
- Save boards and scenarios, and share them with a link
- Optional AI coach: each coach adds their own API key (stored encrypted)

The backend is a single Python file using only the standard library, plus SQLite. The frontend is plain HTML and JavaScript with no build step.

## Requirements

- Python 3 (tested with 3.12)
- `cryptography` (only needed to save LLM API keys; the rest works without it)

## Quick start

```bash
pip install -r v2/requirements.txt

export VLFC_V2_MASTER_KEY='a-long-random-secret'   # keep this secret and backed up
export VLFC_V2_COOKIE_SECURE=0                     # only for plain-http local testing
python3 v2/server_v2.py
```

The API starts on `http://127.0.0.1:3020` and creates the SQLite database on first run. Check it with:

```bash
curl http://127.0.0.1:3020/api/v2/health
```

## Serving the pages

The server only serves the API. The HTML pages must be served from the same origin, so put a reverse proxy in front of both: static files for the pages, and `/api/` forwarded to the Python server. For example, with nginx:

```nginx
server {
    listen 443 ssl;
    server_name board.example.com;
    # ssl_certificate and ssl_certificate_key go here

    root /path/to/velocity-tactical-board;

    location /api/ {
        proxy_pass http://127.0.0.1:3020;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Session cookies are `Secure` by default, so use HTTPS in production.

**Known gap:** after login, the app redirects to `/v2.html`, and the editor's "Dashboard" link points there too. That file exists only in `v2/`, so serving the repo root as shown above leaves those links broken until the path is fixed.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `VLFC_V2_MASTER_KEY` | (none) | Encrypts coaches' LLM API keys. Required before any key can be saved. |
| `VLFC_V2_HOST` | `127.0.0.1` | Address the API listens on. |
| `VLFC_V2_PORT` | `3020` | Port the API listens on. |
| `VLFC_V2_DB` | `v2/vlfc-v2.sqlite3` | SQLite database path. Keep it outside the web root in production. |
| `VLFC_V2_COOKIE_SECURE` | `1` | Set to `0` only for plain-http testing. |
| `VLFC_V2_SESSION_DAYS` | `14` | How long a login lasts. |
| `VLFC_V2_ALLOWED_LLM_HOSTS` | (none) | Comma-separated LLM hosts allowed to resolve to private addresses (for a model on your own network). |

## LLM analysis

Each coach can enter a base URL, model, provider and API key in the settings. The default base URL is `https://api.anthropic.com/v1`.

- Provider `anthropic` uses the Messages API format (`{base}/messages`).
- Provider `openai` uses the Responses API format (`{base}/responses`).

Base URL rules:

- It must use `https`.
- Hosts that resolve to private, loopback or link-local addresses are refused, unless listed in `VLFC_V2_ALLOWED_LLM_HOSTS`.
- Redirects are not followed, so the API key is never sent to a different host.
- Changing the base URL clears the saved key, so a key is never sent to an endpoint it wasn't entered for.

## Tests

```bash
cd v2
python3 -m unittest test_server_v2 -v
```

The tests need no running server.

## Project layout

- `v2/server_v2.py`: the whole backend (routes, auth, board validation, LLM calls)
- `v2/test_server_v2.py`: unit tests for the backend
- `index.html`: login, signup, board list, squad and LLM settings
- `v2-board.html`: the pitch editor, scenario list, frames and AI panel. Also used for read-only share links.
- `v2/`: requirements, server and test code, and older copies of the HTML pages
- `CLAUDE.md`: architecture notes for AI coding assistants

## Security notes

- The SQLite database holds emails, password hashes and encrypted LLM keys. It is git-ignored; never commit it or publish it.
- Board share links are public to anyone who has them. Don't put sensitive information in player notes.
- Login is not rate-limited yet, so run the app behind a proxy that limits requests if it's exposed to the internet.
