#!/usr/bin/env python3
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:
    Fernet = None
    InvalidToken = Exception

HOST = os.environ.get("VLFC_V2_HOST", "127.0.0.1")
PORT = int(os.environ.get("VLFC_V2_PORT", "3020"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("VLFC_V2_DB", os.path.join(BASE_DIR, "vlfc-v2.sqlite3"))
MASTER_KEY = os.environ.get("VLFC_V2_MASTER_KEY", "")
COOKIE_NAME = "vlfc_v2_session"
COOKIE_SECURE = os.environ.get("VLFC_V2_COOKIE_SECURE", "1") != "0"
SESSION_DAYS = int(os.environ.get("VLFC_V2_SESSION_DAYS", "14"))
lock = threading.RLock()

def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def now():
    return int(time.time())

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          email TEXT NOT NULL UNIQUE COLLATE NOCASE,
          coach_name TEXT NOT NULL,
          password_hash TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions(
          token_hash TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS teams(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          format TEXT NOT NULL DEFAULT '6v6',
          colour TEXT NOT NULL DEFAULT 'red',
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS players(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT '',
          specs TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS boards(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
          name TEXT NOT NULL,
          share_token TEXT NOT NULL UNIQUE,
          state_json TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS llm_settings(
          user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          provider TEXT NOT NULL DEFAULT 'anthropic',
          base_url TEXT NOT NULL DEFAULT 'https://api.anthropic.com/v1',
          model TEXT NOT NULL DEFAULT '',
          auth_mode TEXT NOT NULL DEFAULT 'x-api-key',
          api_key_enc TEXT NOT NULL DEFAULT '',
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scenarios(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          share_token TEXT NOT NULL UNIQUE,
          state_json TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        );
        """)
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))

def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
    return f"pbkdf2_sha256$310000${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(dk).decode()}"

def verify_password(password, encoded):
    try:
        alg, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if alg != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()

def make_session(user_id):
    token = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                  (token_hash(token), user_id, now() + SESSION_DAYS * 86400, now()))
    return token

def fernet():
    if Fernet is None:
        raise RuntimeError("Python package 'cryptography' is required for encrypted LLM API keys")
    if not MASTER_KEY:
        raise RuntimeError("VLFC_V2_MASTER_KEY is not configured")
    derived = hashlib.sha256(MASTER_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))

def encrypt_secret(value):
    if not value:
        return ""
    return fernet().encrypt(value.encode()).decode()

def decrypt_secret(value):
    if not value:
        return ""
    try:
        return fernet().decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("Unable to decrypt saved LLM key; check VLFC_V2_MASTER_KEY") from e

def default_board_state():
    return {
        "version": 0,
        "orientation": "horizontal",
        "positions": {"ball": {"x": 600, "y": 350}},
        "players": {},
        "actions": [],
        "frames": [],
        "context": {
            "velocity_team": "A",
            "possession": "velocity",
            "phase": "Build-up",
            "game_state": "Drawing",
            "scenario": "General analysis"
        }
    }

def read_json_body(handler, limit=500000):
    try:
        n = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        n = 0
    if n <= 0 or n > limit:
        raise ValueError("Invalid request size")
    return json.loads(handler.rfile.read(n).decode("utf-8"))

def clean_board_state(data, current_version=0):
    orientation = data.get("orientation", "horizontal")
    if orientation not in ("horizontal", "vertical"):
        orientation = "horizontal"
    positions = {}
    for pid, pos in (data.get("positions") or {}).items():
        if pid != "ball":
            if not isinstance(pid, str) or len(pid) > 32:
                continue
        if not isinstance(pos, dict):
            continue
        try:
            positions[pid] = {"x": float(pos["x"]), "y": float(pos["y"])}
        except Exception:
            pass
    players = {}
    for pid, info in (data.get("players") or {}).items():
        if not isinstance(pid, str) or len(pid) > 32 or not isinstance(info, dict):
            continue
        players[pid] = {
            "name": str(info.get("name", pid)).strip()[:60] or pid,
            "role": str(info.get("role", "")).strip()[:120],
            "specs": str(info.get("specs", "")).strip()[:1000],
            "team_player_id": info.get("team_player_id")
        }
    actions = []
    for item in (data.get("actions") or [])[:200]:
        if not isinstance(item, dict):
            continue
        action_type = str(item.get("type", "")).strip().lower()
        if action_type not in ("pass", "shot", "dribble", "run", "press", "custom"):
            continue
        try:
            actions.append({
                "id": str(item.get("id", secrets.token_hex(4)))[:40],
                "type": action_type,
                "x1": float(item["x1"]), "y1": float(item["y1"]),
                "x2": float(item["x2"]), "y2": float(item["y2"]),
                "step": max(1, min(99, int(item.get("step", len(actions)+1)))),
                "label": str(item.get("label", "")).strip()[:80]
            })
        except Exception:
            continue

    frames = []
    for frame_index, frame in enumerate((data.get("frames") or [])[:100], start=1):
        if not isinstance(frame, dict):
            continue

        frame_positions = {}
        for pid, pos in (frame.get("positions") or {}).items():
            if pid != "ball":
                if not isinstance(pid, str) or len(pid) > 32:
                    continue
            if not isinstance(pos, dict):
                continue
            try:
                frame_positions[pid] = {
                    "x": float(pos["x"]),
                    "y": float(pos["y"])
                }
            except Exception:
                continue

        frame_actions = []
        for item in (frame.get("actions") or [])[:200]:
            if not isinstance(item, dict):
                continue
            action_type = str(item.get("type", "")).strip().lower()
            if action_type not in ("pass", "shot", "dribble", "run", "press", "custom"):
                continue
            try:
                frame_actions.append({
                    "id": str(item.get("id", secrets.token_hex(4)))[:40],
                    "type": action_type,
                    "x1": float(item["x1"]),
                    "y1": float(item["y1"]),
                    "x2": float(item["x2"]),
                    "y2": float(item["y2"]),
                    "step": max(1, min(99, int(item.get("step", len(frame_actions) + 1)))),
                    "label": str(item.get("label", "")).strip()[:80]
                })
            except Exception:
                continue

        frames.append({
            "id": str(frame.get("id", f"frame_{frame_index}"))[:40],
            "name": str(frame.get("name", f"Frame {frame_index}")).strip()[:120] or f"Frame {frame_index}",
            "duration_ms": max(500, min(15000, int(frame.get("duration_ms", 1800)))),
            "positions": frame_positions,
            "actions": frame_actions
        })

    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    safe_context = {
        "velocity_team": str(context.get("velocity_team", "A"))[:10],
        "possession": str(context.get("possession", "velocity"))[:30],
        "phase": str(context.get("phase", "Build-up"))[:60],
        "game_state": str(context.get("game_state", "Drawing"))[:30],
        "scenario": str(context.get("scenario", "General analysis"))[:100],
    }
    return {
        "version": int(current_version) + 1,
        "orientation": orientation,
        "positions": positions,
        "players": players,
        "actions": actions,
        "frames": frames,
        "context": safe_context
    }

def router_parse(raw_text):
    text = (raw_text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    candidates = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            value = json.loads(line)
            if isinstance(value, (dict, list)):
                candidates.append(value)
        except Exception:
            pass
    dec = json.JSONDecoder()
    i = 0
    while i < len(text):
        try:
            value, end = dec.raw_decode(text, i)
            if isinstance(value, (dict, list)):
                candidates.append(value)
            i = end
        except Exception:
            i += 1
    if not candidates:
        raise RuntimeError("LLM router returned no usable JSON")
    for value in reversed(candidates):
        if isinstance(value, dict) and (
            isinstance(value.get("content"), list)
            or isinstance(value.get("output"), list)
            or isinstance(value.get("choices"), list)
        ):
            return value
    return candidates[-1]

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would re-send the API key headers to whatever host a redirect points at.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

_LLM_OPENER = urllib.request.build_opener(_NoRedirect)

ALLOWED_LLM_HOSTS = {h.strip().lower() for h in os.environ.get("VLFC_V2_ALLOWED_LLM_HOSTS", "").split(",") if h.strip()}

def validate_base_url(url):
    # Public https endpoints are allowed (custom models included). Hosts that resolve to
    # private, loopback, link-local or otherwise non-global addresses are refused, unless
    # the operator listed them in VLFC_V2_ALLOWED_LLM_HOSTS.
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise ValueError("LLM base URL must be an https:// URL")
    if host in ALLOWED_LLM_HOSTS:
        return url
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError("LLM base URL host could not be resolved")
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise ValueError("LLM base URL must not point to a private or internal address")
    return url

def request_json(url, body, headers):
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers=headers,
        method="POST"
    )
    try:
        with _LLM_OPENER.open(req, timeout=60) as r:
            return router_parse(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        # Upstream detail stays in the server log; the browser gets only the status code.
        detail = e.read().decode("utf-8", errors="replace")
        print(f"LLM API error {e.code}: {detail[:1200]}")
        raise RuntimeError(f"LLM API returned HTTP {e.code}")
    except urllib.error.URLError:
        print("LLM connection failed")
        raise RuntimeError("LLM connection failed")

def parse_model_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        return json.loads(text)
    except Exception:
        dec = json.JSONDecoder()
        for i, ch in enumerate(text):
            if ch not in "{[":
                continue
            try:
                value, _ = dec.raw_decode(text[i:])
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
    raise RuntimeError("Model did not return usable JSON")

TACTICAL_PROMPT = """You are a practical small-sided football tactical coach.
Analyse the exact board state, player names/roles/specs, ball position, possession, phase, game state and scenario.
Prioritise simple executable advice. Distinguish formation from playing style.
For defensive issues: protect centre, nearest presses, next covers, far-side tucks in.
For build-up: use width, passing angles, a safety player, and the goalkeeper when appropriate.
Return ONLY valid JSON:
{
 "summary":"short assessment",
 "scores":{"spacing":0,"defensive_security":0,"passing_options":0,"transition_safety":0},
 "strengths":["..."],
 "problems":[{"severity":"critical|important|development","player_id":"ID","message":"..."}],
 "recommendations":[{"player_id":"ID","action":"move|hold|press|drop|widen|narrow|support|reset","reason":"...","suggested_x":0,"suggested_y":0}],
 "team_instructions":["..."]
}
Only reference player IDs actually present in the board state. Prefer 1-3 important movements rather than moving everybody.
Horizontal pitch: 1200x700. Vertical pitch: 700x1000.
"""

def analyse_with_settings(settings, state):
    api_key = decrypt_secret(settings["api_key_enc"])
    if not api_key:
        raise RuntimeError("No LLM API key saved for this coach")
    provider = settings["provider"].lower()
    base_url = validate_base_url(settings["base_url"].rstrip("/"))
    model = settings["model"]
    auth_mode = settings["auth_mode"].lower()
    headers = {"Content-Type": "application/json"}
    if auth_mode in ("x-api-key", "both"):
        headers["x-api-key"] = api_key
    if auth_mode in ("bearer", "both"):
        headers["Authorization"] = "Bearer " + api_key

    user_text = json.dumps({"task": "Analyse this tactical board", "board_state": state}, ensure_ascii=False)

    if provider in ("anthropic", "claude"):
        headers["anthropic-version"] = "2023-06-01"
        raw = request_json(
            base_url + "/messages",
            {
                "model": model,
                "max_tokens": 1800,
                "temperature": 0.2,
                "system": TACTICAL_PROMPT,
                "messages": [{"role": "user", "content": user_text}]
            },
            headers
        )
        chunks = [
            x.get("text", "") for x in raw.get("content", [])
            if isinstance(x, dict) and x.get("type") == "text"
        ]
        return parse_model_json("\n".join(chunks))

    if provider in ("openai", "responses"):
        raw = request_json(
            base_url + "/responses",
            {
                "model": model,
                "instructions": TACTICAL_PROMPT,
                "input": user_text,
                "max_output_tokens": 1800
            },
            headers
        )
        chunks = []
        for item in raw.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        chunks.append(content.get("text", ""))
        return parse_model_json("\n".join(chunks))

    raise RuntimeError("Unsupported provider. Use anthropic or openai.")

class Handler(BaseHTTPRequestHandler):
    server_version = "VLFC-V2/0.1"

    def send_json(self, status, data, extra_headers=None):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def user(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = jar.get(COOKIE_NAME)
        if not morsel:
            return None
        th = token_hash(morsel.value)
        with db() as c:
            row = c.execute("""
              SELECT u.id,u.email,u.coach_name
              FROM sessions s JOIN users u ON u.id=s.user_id
              WHERE s.token_hash=? AND s.expires_at>?
            """, (th, now())).fetchone()
            return dict(row) if row else None

    def require_user(self):
        u = self.user()
        if not u:
            self.send_json(401, {"error": "Login required"})
            return None
        return u

    def require_mutation_header(self):
        if self.headers.get("X-VLFC-Request") != "v2":
            self.send_json(403, {"error": "Missing request header"})
            return False
        return True

    def path_parts(self):
        return [p for p in urlparse(self.path).path.split("/") if p]

    def do_GET(self):
        parts = self.path_parts()

        if parts == ["api", "v2", "health"]:
            return self.send_json(200, {
                "ok": True,
                "service": "Velocity Tactical Board v2",
                "db": DB_PATH,
                "encrypted_llm_keys": bool(Fernet and MASTER_KEY)
            })

        if parts == ["api", "v2", "me"]:
            u = self.user()
            return self.send_json(200, {"user": u})

        # Public board viewer endpoint: intentionally does not require login.
        if len(parts) == 4 and parts[:3] == ["api", "v2", "share"]:
            token = parts[3]
            with db() as c:
                row = c.execute("SELECT id,name,state_json,updated_at FROM boards WHERE share_token=?", (token,)).fetchone()
            if not row:
                return self.send_json(404, {"error": "Board not found"})
            return self.send_json(200, {
                "id": row["id"], "name": row["name"],
                "state": json.loads(row["state_json"]),
                "updated_at": row["updated_at"]
            })

        if len(parts) == 4 and parts[:3] == ["api", "v2", "scenario-share"]:
            token = parts[3]
            with db() as c:
                row = c.execute("""
                    SELECT s.id,s.name,s.description,s.state_json,s.updated_at,b.name AS board_name
                    FROM scenarios s JOIN boards b ON b.id=s.board_id
                    WHERE s.share_token=?
                """, (token,)).fetchone()
            if not row:
                return self.send_json(404, {"error": "Scenario not found"})
            return self.send_json(200, {
                "id": row["id"], "name": row["name"], "description": row["description"],
                "board_name": row["board_name"], "state": json.loads(row["state_json"]),
                "updated_at": row["updated_at"]
            })

        u = self.require_user()
        if not u:
            return

        if parts == ["api", "v2", "teams"]:
            with db() as c:
                rows = c.execute("SELECT id,name,format,colour,created_at FROM teams WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()
            return self.send_json(200, {"teams": [dict(r) for r in rows]})

        if len(parts) == 5 and parts[:3] == ["api", "v2", "teams"] and parts[4] == "players":
            team_id = int(parts[3])
            with db() as c:
                owner = c.execute("SELECT id FROM teams WHERE id=? AND user_id=?", (team_id, u["id"])).fetchone()
                if not owner:
                    return self.send_json(404, {"error": "Team not found"})
                rows = c.execute("SELECT id,name,role,specs FROM players WHERE team_id=? ORDER BY name", (team_id,)).fetchall()
            return self.send_json(200, {"players": [dict(r) for r in rows]})

        if parts == ["api", "v2", "boards"]:
            with db() as c:
                rows = c.execute("""
                  SELECT b.id,b.name,b.team_id,b.share_token,b.updated_at,t.name AS team_name
                  FROM boards b LEFT JOIN teams t ON t.id=b.team_id
                  WHERE b.user_id=? ORDER BY b.updated_at DESC
                """, (u["id"],)).fetchall()
            return self.send_json(200, {"boards": [dict(r) for r in rows]})

        if len(parts) == 4 and parts[:3] == ["api", "v2", "boards"]:
            board_id = int(parts[3])
            with db() as c:
                row = c.execute("SELECT id,name,team_id,share_token,state_json,updated_at FROM boards WHERE id=? AND user_id=?", (board_id, u["id"])).fetchone()
            if not row:
                return self.send_json(404, {"error": "Board not found"})
            return self.send_json(200, {
                "id": row["id"], "name": row["name"], "team_id": row["team_id"],
                "share_token": row["share_token"], "state": json.loads(row["state_json"]),
                "updated_at": row["updated_at"]
            })

        if len(parts) == 5 and parts[:3] == ["api", "v2", "boards"] and parts[4] == "scenarios":
            board_id = int(parts[3])
            with db() as c:
                if not c.execute("SELECT id FROM boards WHERE id=? AND user_id=?", (board_id,u["id"])).fetchone():
                    return self.send_json(404,{"error":"Board not found"})
                rows=c.execute("SELECT id,name,description,share_token,created_at,updated_at FROM scenarios WHERE board_id=? AND user_id=? ORDER BY updated_at DESC",(board_id,u["id"])).fetchall()
            return self.send_json(200,{"scenarios":[dict(r) for r in rows]})

        if len(parts) == 4 and parts[:3] == ["api", "v2", "scenarios"]:
            sid=int(parts[3])
            with db() as c:
                row=c.execute("SELECT id,board_id,name,description,share_token,state_json,created_at,updated_at FROM scenarios WHERE id=? AND user_id=?",(sid,u["id"])).fetchone()
            if not row:return self.send_json(404,{"error":"Scenario not found"})
            return self.send_json(200,{"id":row["id"],"board_id":row["board_id"],"name":row["name"],"description":row["description"],"share_token":row["share_token"],"state":json.loads(row["state_json"]),"created_at":row["created_at"],"updated_at":row["updated_at"]})

        if len(parts) == 4 and parts[:3] == ["api", "v2", "scenarios"]:
            sid=int(parts[3])
            try:
                d=read_json_body(self)
                with db() as c:
                    row=c.execute("SELECT state_json FROM scenarios WHERE id=? AND user_id=?",(sid,u["id"])).fetchone()
                    if not row:return self.send_json(404,{"error":"Scenario not found"})
                    fields=[]; vals=[]
                    if "name" in d:
                        n=str(d["name"]).strip()[:120]
                        if not n:raise ValueError("Scenario name required")
                        fields.append("name=?");vals.append(n)
                    if "description" in d:fields.append("description=?");vals.append(str(d["description"]).strip()[:1000])
                    if isinstance(d.get("state"),dict):
                        old=json.loads(row["state_json"]); new=clean_board_state(d["state"],old.get("version",0));fields.append("state_json=?");vals.append(json.dumps(new,separators=(",",":")))
                    fields.append("updated_at=?");vals.append(now());vals.extend([sid,u["id"]])
                    c.execute("UPDATE scenarios SET "+",".join(fields)+" WHERE id=? AND user_id=?",tuple(vals))
                return self.send_json(200,{"ok":True})
            except Exception as e:return self.send_json(400,{"error":str(e)})

        if parts == ["api", "v2", "settings", "llm"]:
            with db() as c:
                row = c.execute("SELECT provider,base_url,model,auth_mode,api_key_enc,updated_at FROM llm_settings WHERE user_id=?", (u["id"],)).fetchone()
            if not row:
                return self.send_json(200, {
                    "provider": "anthropic",
                    "base_url": "https://api.anthropic.com/v1",
                    "model": "",
                    "auth_mode": "x-api-key",
                    "has_api_key": False
                })
            return self.send_json(200, {
                "provider": row["provider"], "base_url": row["base_url"],
                "model": row["model"], "auth_mode": row["auth_mode"],
                "has_api_key": bool(row["api_key_enc"])
            })

        return self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        parts = self.path_parts()
        if not self.require_mutation_header():
            return

        if parts == ["api", "v2", "auth", "signup"]:
            try:
                data = read_json_body(self)
                email = str(data.get("email", "")).strip().lower()
                name = str(data.get("coach_name", "")).strip()[:80]
                password = str(data.get("password", ""))
                if "@" not in email or len(password) < 10 or not name:
                    return self.send_json(400, {"error": "Use a valid email, coach name, and password of at least 10 characters"})
                with db() as c:
                    cur = c.execute("INSERT INTO users(email,coach_name,password_hash,created_at) VALUES(?,?,?,?)",
                                    (email, name, hash_password(password), now()))
                    uid = cur.lastrowid
                token = make_session(uid)
                cookie = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS*86400}"
                if COOKIE_SECURE:
                    cookie += "; Secure"
                return self.send_json(201, {"ok": True}, [("Set-Cookie", cookie)])
            except sqlite3.IntegrityError:
                return self.send_json(409, {"error": "Email already registered"})
            except Exception as e:
                return self.send_json(400, {"error": str(e)})

        if parts == ["api", "v2", "auth", "login"]:
            try:
                data = read_json_body(self)
                email = str(data.get("email", "")).strip().lower()
                password = str(data.get("password", ""))
                with db() as c:
                    row = c.execute("SELECT id,password_hash FROM users WHERE email=?", (email,)).fetchone()
                if not row or not verify_password(password, row["password_hash"]):
                    return self.send_json(401, {"error": "Invalid email or password"})
                token = make_session(row["id"])
                cookie = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS*86400}"
                if COOKIE_SECURE:
                    cookie += "; Secure"
                return self.send_json(200, {"ok": True}, [("Set-Cookie", cookie)])
            except Exception as e:
                return self.send_json(400, {"error": str(e)})

        if parts == ["api", "v2", "auth", "logout"]:
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            morsel = jar.get(COOKIE_NAME)
            if morsel:
                with db() as c:
                    c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(morsel.value),))
            cookie = f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            if COOKIE_SECURE:
                cookie += "; Secure"
            return self.send_json(200, {"ok": True}, [("Set-Cookie", cookie)])

        u = self.require_user()
        if not u:
            return

        if parts == ["api", "v2", "teams"]:
            try:
                d = read_json_body(self)
                name = str(d.get("name", "")).strip()[:100]
                fmt = str(d.get("format", "6v6")).strip()[:20]
                colour = str(d.get("colour", "red")).strip()[:20]
                if not name:
                    raise ValueError("Team name required")
                with db() as c:
                    cur = c.execute("INSERT INTO teams(user_id,name,format,colour,created_at) VALUES(?,?,?,?,?)",
                                    (u["id"], name, fmt, colour, now()))
                return self.send_json(201, {"ok": True, "id": cur.lastrowid})
            except Exception as e:
                return self.send_json(400, {"error": str(e)})

        if len(parts) == 5 and parts[:3] == ["api", "v2", "teams"] and parts[4] == "players":
            try:
                team_id = int(parts[3])
                d = read_json_body(self)
                with db() as c:
                    owner = c.execute("SELECT id FROM teams WHERE id=? AND user_id=?", (team_id, u["id"])).fetchone()
                    if not owner:
                        return self.send_json(404, {"error": "Team not found"})
                    name = str(d.get("name", "")).strip()[:80]
                    if not name:
                        raise ValueError("Player name required")
                    cur = c.execute("INSERT INTO players(team_id,name,role,specs,created_at) VALUES(?,?,?,?,?)",
                                    (team_id, name, str(d.get("role",""))[:120], str(d.get("specs",""))[:1000], now()))
                return self.send_json(201, {"ok": True, "id": cur.lastrowid})
            except Exception as e:
                return self.send_json(400, {"error": str(e)})

        if parts == ["api", "v2", "boards"]:
            try:
                d = read_json_body(self)
                name = str(d.get("name", "")).strip()[:120]
                team_id = d.get("team_id")
                if not name:
                    raise ValueError("Board name required")
                if team_id is not None:
                    team_id = int(team_id)
                    with db() as c:
                        if not c.execute("SELECT id FROM teams WHERE id=? AND user_id=?", (team_id,u["id"])).fetchone():
                            return self.send_json(404, {"error": "Team not found"})
                share = secrets.token_urlsafe(10)
                state = default_board_state()
                with db() as c:
                    cur = c.execute("""INSERT INTO boards(user_id,team_id,name,share_token,state_json,created_at,updated_at)
                                      VALUES(?,?,?,?,?,?,?)""",
                                    (u["id"], team_id, name, share, json.dumps(state,separators=(",",":")), now(), now()))
                return self.send_json(201, {"ok": True, "id": cur.lastrowid, "share_token": share})
            except Exception as e:
                return self.send_json(400, {"error": str(e)})

        if len(parts) == 5 and parts[:3] == ["api", "v2", "boards"] and parts[4] == "scenarios":
            board_id=int(parts[3])
            try:
                d=read_json_body(self); name=str(d.get("name","")).strip()[:120]; desc=str(d.get("description","")).strip()[:1000]
                if not name: raise ValueError("Scenario name required")
                with db() as c:
                    b=c.execute("SELECT state_json FROM boards WHERE id=? AND user_id=?",(board_id,u["id"])).fetchone()
                    if not b:return self.send_json(404,{"error":"Board not found"})
                    current=json.loads(b["state_json"]); snapshot=clean_board_state(d.get("state") if isinstance(d.get("state"),dict) else current,current.get("version",0))
                    token=secrets.token_urlsafe(12)
                    cur=c.execute("INSERT INTO scenarios(user_id,board_id,name,description,share_token,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(u["id"],board_id,name,desc,token,json.dumps(snapshot,separators=(",",":")),now(),now()))
                return self.send_json(201,{"ok":True,"id":cur.lastrowid,"share_token":token})
            except Exception as e:return self.send_json(400,{"error":str(e)})

        if len(parts) == 5 and parts[:3] == ["api", "v2", "boards"] and parts[4] == "analyse":
            board_id = int(parts[3])
            try:
                with db() as c:
                    b = c.execute("SELECT state_json FROM boards WHERE id=? AND user_id=?", (board_id,u["id"])).fetchone()
                    s = c.execute("SELECT * FROM llm_settings WHERE user_id=?", (u["id"],)).fetchone()
                if not b:
                    return self.send_json(404, {"error": "Board not found"})
                if not s:
                    return self.send_json(400, {"error": "Configure LLM settings first"})
                analysis = analyse_with_settings(dict(s), json.loads(b["state_json"]))
                return self.send_json(200, {"ok": True, "analysis": analysis})
            except Exception as e:
                return self.send_json(500, {"ok": False, "error": str(e)})

        return self.send_json(404, {"error": "Not found"})

    def do_PUT(self):
        parts = self.path_parts()
        if not self.require_mutation_header():
            return
        u = self.require_user()
        if not u:
            return

        if len(parts) == 5 and parts[:3] == ["api", "v2", "boards"] and parts[4] == "state":
            board_id = int(parts[3])
            try:
                d = read_json_body(self)
                with db() as c:
                    row = c.execute("SELECT state_json FROM boards WHERE id=? AND user_id=?", (board_id,u["id"])).fetchone()
                    if not row:
                        return self.send_json(404, {"error": "Board not found"})
                    old = json.loads(row["state_json"])
                    new = clean_board_state(d, old.get("version",0))
                    c.execute("UPDATE boards SET state_json=?,updated_at=? WHERE id=?",
                              (json.dumps(new,separators=(",",":")), now(), board_id))
                return self.send_json(200, {"ok": True, "version": new["version"]})
            except Exception as e:
                return self.send_json(400, {"error": str(e)})

        if parts == ["api", "v2", "settings", "llm"]:
            try:
                d = read_json_body(self)
                provider = str(d.get("provider","anthropic")).strip().lower()
                base_url = str(d.get("base_url","")).strip().rstrip("/")
                model = str(d.get("model","")).strip()
                auth_mode = str(d.get("auth_mode","x-api-key")).strip().lower()
                if provider not in ("anthropic","openai") or not base_url or not model or auth_mode not in ("x-api-key","bearer","both"):
                    raise ValueError("Invalid LLM settings")
                validate_base_url(base_url)
                with db() as c:
                    old = c.execute("SELECT api_key_enc, base_url FROM llm_settings WHERE user_id=?", (u["id"],)).fetchone()
                    enc = old["api_key_enc"] if old else ""
                    # A saved key is only ever sent to the endpoint it was saved for.
                    if old and old["base_url"] != base_url and not d.get("api_key"):
                        enc = ""
                    if d.get("api_key"):
                        enc = encrypt_secret(str(d["api_key"]).strip())
                    c.execute("""INSERT INTO llm_settings(user_id,provider,base_url,model,auth_mode,api_key_enc,updated_at)
                                 VALUES(?,?,?,?,?,?,?)
                                 ON CONFLICT(user_id) DO UPDATE SET
                                 provider=excluded.provider,base_url=excluded.base_url,model=excluded.model,
                                 auth_mode=excluded.auth_mode,api_key_enc=excluded.api_key_enc,updated_at=excluded.updated_at""",
                              (u["id"],provider,base_url,model,auth_mode,enc,now()))
                return self.send_json(200, {"ok": True, "has_api_key": bool(enc)})
            except Exception as e:
                return self.send_json(400, {"error": str(e)})

        return self.send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        parts = self.path_parts()
        if not self.require_mutation_header():
            return
        u = self.require_user()
        if not u:
            return

        if len(parts) == 4 and parts[:3] == ["api","v2","players"]:
            pid = int(parts[3])
            with db() as c:
                c.execute("""DELETE FROM players WHERE id=? AND team_id IN
                             (SELECT id FROM teams WHERE user_id=?)""", (pid,u["id"]))
            return self.send_json(200, {"ok": True})

        if len(parts) == 4 and parts[:3] == ["api","v2","scenarios"]:
            sid=int(parts[3])
            with db() as c:c.execute("DELETE FROM scenarios WHERE id=? AND user_id=?",(sid,u["id"]))
            return self.send_json(200,{"ok":True})

        if len(parts) == 4 and parts[:3] == ["api","v2","boards"]:
            bid = int(parts[3])
            with db() as c:
                c.execute("DELETE FROM boards WHERE id=? AND user_id=?", (bid,u["id"]))
            return self.send_json(200, {"ok": True})

        return self.send_json(404, {"error": "Not found"})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

if __name__ == "__main__":
    init_db()
    print(f"Velocity Tactical Board v2 API on http://{HOST}:{PORT}")
    print("Database:", DB_PATH)
    print("Encrypted LLM key storage:", bool(Fernet and MASTER_KEY))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
