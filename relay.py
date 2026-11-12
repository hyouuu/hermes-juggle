#!/usr/bin/env python3
"""Juggle relay.

Listens on 127.0.0.1:9340 and serves a small whitelist of dashboard calls
from the local Hermes dashboard (127.0.0.1:9119), plus Juggle's own routes
(pairing, sealed requests, replies, new chats, images). Devices reach it
through the hub connection the plugin keeps open, end to end encrypted.

The dashboard session token never leaves this machine.
"""
import hmac
import importlib.util
import json
import uuid
import os
import sys
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DASH = "http://127.0.0.1:9119"


def _e2e():
    """Pairing and end-to-end sealed requests (juggle-bridge plugin's e2e.py)."""
    import importlib.util
    import sys
    mod = sys.modules.get("juggle_e2e")
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            "juggle_e2e", Path(__file__).resolve().parent / "e2e.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["juggle_e2e"] = mod
        spec.loader.exec_module(mod)
    return mod
PORT = 9340
HERMES = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _phone_token():
    """Legacy bearer token for unpaired clients. A fresh install gets a random
    one it never hands out, so only paired (end-to-end) devices get in."""
    f = HERMES / "juggle-relay.token"
    if not f.exists():
        import secrets
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(secrets.token_urlsafe(32))
    return f.read_text().strip()


PHONE_TOKEN = _phone_token()
ID = r"[A-Za-z0-9_.:-]+"
GET_OK = [re.compile(r"^/api/sessions$"), re.compile(rf"^/api/sessions/{ID}/messages$")]
PATCH_OK = re.compile(rf"^/api/sessions/{ID}$")
PATCH_FIELDS = {"unread"}
REPLY_OK = re.compile(rf"^/api/sessions/({ID})/reply$")
UPLOAD_OK = re.compile(r"^/api/uploads$")
EVENTS_OK = re.compile(rf"^/api/sessions/({ID})/skill-events$")
DISPATCH = HERMES / "dispatch"
EVENTS = HERMES / "juggle" / "skill-events.jsonl"
UPLOADS = HERMES / "juggle" / "uploads"
# Images Juggle may fetch: Discord attachments, agent output, Juggle uploads.
MEDIA_ROOTS = [(HERMES / "cache").resolve(), UPLOADS.resolve()]
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic"}
UPLOAD_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/heic": ".heic",
              "image/gif": ".gif", "image/webp": ".webp"}
MAX_UPLOAD = 15 * 1024 * 1024
DISCORD_API = "https://discord.com/api/v10"


def env(key):
    if os.environ.get(key):
        return os.environ[key]
    try:
        lines = (HERMES / ".env").read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def discord(method, path, body=None):
    """Bot-token Discord REST call; the token stays on this Mac."""
    req = urllib.request.Request(DISCORD_API + path, method=method,
                                 data=None if body is None else json.dumps(body).encode())
    req.add_header("Authorization", "Bot " + env("DISCORD_BOT_TOKEN"))
    req.add_header("User-Agent", "JuggleRelay (https://github.com/hyouuu/hermes-juggle, 1)")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read()
        return json.loads(data) if data else {}


_channels = (0.0, None)


def channels():
    """Every text channel in the bot's servers, in Discord sidebar order."""
    global _channels
    if _channels[1] is not None and time.time() - _channels[0] < 60:
        return _channels[1]
    out = []
    for g in discord("GET", "/users/@me/guilds"):
        chans = discord("GET", f"/guilds/{g['id']}/channels")
        cats = {c["id"]: c for c in chans if c["type"] == 4}
        def order(c):
            cat = cats.get(c.get("parent_id"))
            return (cat["position"] if cat else -1, c["position"])
        for c in sorted((c for c in chans if c["type"] in (0, 5)), key=order):
            out.append({"guild_id": g["id"], "guild": g["name"], "id": c["id"], "name": c["name"],
                        "category": (cats.get(c.get("parent_id")) or {}).get("name")})
    _channels = (time.time(), out)
    return out


def dispatch(job):
    """Inject a user turn into Hermes. In the gateway this calls the plugin's
    inject_message directly; otherwise it falls back to the session-dispatch
    plugin's inbox. True/False when it ran, None if still queued."""
    inject = getattr(sys.modules.get("juggle_runtime"), "inject", None)
    if inject is not None:
        try:
            if "origin" in job:
                return bool(inject(job["content"], origin=job["origin"]))
            return bool(inject(job["content"], session_key=job["session_key"]))
        except Exception:
            return False
    for sub in ("new", "inbox", "done"):
        (DISPATCH / sub).mkdir(parents=True, exist_ok=True)
    stem = f"juggle-{int(time.time() * 1000)}"
    tmp = DISPATCH / ("new" if "origin" in job else "inbox") / (stem + ".tmp")
    tmp.write_text(json.dumps(job))
    tmp.rename(tmp.with_suffix(".json"))
    result = DISPATCH / "done" / (stem + ".result.json")
    for _ in range(40):
        if result.exists():
            return bool(json.loads(result.read_text()).get("ok"))
        time.sleep(0.25)
    return None


def _owner_name(uid):
    """Display name of the Discord user the thread is started for."""
    try:
        return discord("GET", f"/users/{uid}").get("username") or "owner" if uid else "owner"
    except Exception:
        return "owner"


def media_file(raw):
    """Resolve a requested path; only image files under MEDIA_ROOTS pass."""
    try:
        p = Path(raw).expanduser().resolve(strict=True)
    except Exception:
        return None
    if p.suffix.lower() not in IMAGE_TYPES or not p.is_file():
        return None
    if not any(p.is_relative_to(root) for root in MEDIA_ROOTS):
        return None
    return p


def compose(text, images):
    """Same shape the Discord gateway uses, so Hermes and Juggle read it alike."""
    head = "".join(
        "[The user sent an image~ attached from Juggle]\n"
        f"[If you need a closer look, use vision_analyze with image_url: {p} ~]\n"
        for p in images)
    return (head + "\n" + text).strip() if head else text

_dash_token = None


def dash_token(refresh=False):
    global _dash_token
    if _dash_token is None or refresh:
        html = urllib.request.urlopen(DASH + "/", timeout=5).read().decode()
        m = re.search(r'__HERMES_SESSION_TOKEN__\s*=\s*"([^"]+)"', html)
        _dash_token = m.group(1) if m else None
    return _dash_token


def forward(method, path_qs, body=None):
    for attempt in (0, 1):
        req = urllib.request.Request(DASH + path_qs, data=body, method=method)
        req.add_header("X-Hermes-Session-Token", dash_token(refresh=attempt == 1) or "")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and attempt == 0:
                continue
            return e.code, e.read()
    return 502, b"{}"


def delete_session(sid):
    """The dashboard's fenced delete: read the session's mutation snapshot, then
    DELETE with that revision so a session that changed meanwhile isn't removed."""
    code, body = forward("GET", f"/api/sessions/{sid}/mutation-snapshot")
    if code != 200:
        return code
    snap = json.loads(body)
    if not snap.get("exists"):
        return 200  # already gone
    q = {"request_id": str(uuid.uuid4()), "expected_revision": int(snap["runtime_revision"])}
    if snap.get("runtime_generation") is not None:
        q["expected_generation"] = int(snap["runtime_generation"])
    code, _ = forward("DELETE", f"/api/sessions/{sid}?" + urllib.parse.urlencode(q))
    return code


JUGGLE_KEY_PREFIX = "agent:main:juggle:dm:"


def juggle_chat_id(key):
    return key[len(JUGGLE_KEY_PREFIX):] if (key or "").startswith(JUGGLE_KEY_PREFIX) else None


class Handler(BaseHTTPRequestHandler):
    server_version = "JuggleRelay"

    def log_message(self, fmt, *args):
        pass

    def reply(self, code, data=b"{}"):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        got = self.headers.get("Authorization", "")
        ok = hmac.compare_digest(got.encode(), f"Bearer {PHONE_TOKEN}".encode())
        if not ok:
            time.sleep(1)
            self.reply(401, b'{"error":"unauthorized"}')
        return ok

    def path_parts(self):
        path, _, qs = self.path.partition("?")
        if path.startswith("/juggle"):
            path = path[len("/juggle"):]
        return path, ("?" + qs if qs else "")

    def do_GET(self):
        path, qs = self.path_parts()
        if path == "/health":
            return self.reply(200, b'{"ok":true}')
        if not self.authed():
            return
        if path == "/api/media":
            from urllib.parse import parse_qs
            f = media_file((parse_qs(qs.lstrip("?")).get("path") or [""])[0])
            if not f:
                return self.reply(404, b'{"error":"not allowed"}')
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", IMAGE_TYPES[f.suffix.lower()])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            return self.wfile.write(data)
        if path == "/api/juggle/direct":
            # Addresses a paired phone can try before the hub (same network or tailnet).
            try:
                spec = importlib.util.spec_from_file_location(
                    "juggle_direct", Path(__file__).resolve().parent / "direct.py")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                found = mod.addresses()
            except Exception:
                found = []
            return self.reply(200, json.dumps({"direct": found}).encode())
        if path == "/api/channels":
            try:
                return self.reply(200, json.dumps({"channels": channels()}).encode())
            except Exception:
                return self.reply(502, b'{"error":"discord unreachable"}')
        if path == "/api/skill-events":
            out = []
            if EVENTS.exists():
                for line in EVENTS.read_text().splitlines()[-500:]:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
            return self.reply(200, json.dumps({"events": out}).encode())
        if (m := EVENTS_OK.match(path)):
            out = []
            if EVENTS.exists():
                for line in EVENTS.read_text().splitlines():
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if e.get("session_id") == m.group(1):
                        out.append(e)
            return self.reply(200, json.dumps({"events": out}).encode())
        if not any(p.match(path) for p in GET_OK):
            return self.reply(404, b'{"error":"not allowed"}')
        try:
            self.reply(*forward("GET", path + qs))
        except Exception:
            self.reply(502, b'{"error":"dashboard unreachable"}')

    def do_PATCH(self):
        path, _ = self.path_parts()
        if not self.authed():
            return
        if not PATCH_OK.match(path):
            return self.reply(404, b'{"error":"not allowed"}')
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 4096)
            fields = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.reply(400, b'{"error":"bad json"}')
        if not isinstance(fields, dict) or not fields or set(fields) - PATCH_FIELDS:
            return self.reply(400, b'{"error":"only unread can be changed"}')
        try:
            self.reply(*forward("PATCH", path, json.dumps(fields).encode()))
        except Exception:
            self.reply(502, b'{"error":"dashboard unreachable"}')

    def reply_juggle(self, chat_id, text, extra=None):
        live = sys.modules.get("juggle_live")
        if not live or not live.running():
            return self.reply(503, b'{"error":"the Juggle platform is starting, try again in a moment"}')
        try:
            live.deliver(chat_id, text)
        except Exception as e:
            return self.reply(503, json.dumps({"error": str(e)[:200]}).encode())
        self.reply(200, json.dumps({"ok": True, "chat_id": chat_id, **(extra or {})}).encode())

    def new_juggle_chat(self):
        """Start a brand-new Juggle chat with its first message."""
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 16384)
            body = json.loads(self.rfile.read(n) or b"{}")
            text = (body.get("text") or "").strip()
            images = [str(f) for f in (media_file(p) for p in body.get("images") or [])
                      if f and f.is_relative_to(UPLOADS.resolve())][:6]
        except Exception:
            return self.reply(400, b'{"error":"bad json"}')
        if not text and not images:
            return self.reply(400, b'{"error":"empty"}')
        live = sys.modules.get("juggle_live")
        if not live:
            return self.reply(503, b'{"error":"the Juggle platform is not loaded"}')
        cid = live.new_chat_id()
        self.reply_juggle(cid, compose(text, images), {"session_key": JUGGLE_KEY_PREFIX + cid})

    def delete_sessions(self):
        """Delete Hermes sessions (history, files) the user picked in Juggle.
        Uses the dashboard's per-session DELETE, which also scrubs on-disk
        snapshots. Discord/Telegram messages themselves are left alone."""
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 65536)
            ids = json.loads(self.rfile.read(n) or b"{}").get("ids")
        except Exception:
            return self.reply(400, b'{"error":"bad json"}')
        if not isinstance(ids, list) or not 0 < len(ids) <= 500 or \
                not all(isinstance(i, str) and re.fullmatch(ID, i) for i in ids):
            return self.reply(400, b'{"error":"ids must be 1-500 session ids"}')
        deleted, failed = [], []
        for sid in dict.fromkeys(ids):
            try:
                code = delete_session(sid)
            except Exception:
                code = 502
            (deleted if 200 <= code < 300 else failed).append(sid)
        self.reply(200 if deleted or not failed else 502,
                   json.dumps({"deleted": deleted, "failed": failed}).encode())

    def do_POST(self):
        """Reply from Juggle: queue the text as a user turn in the session's
        gateway conversation via the session-dispatch plugin's inbox."""
        path, _ = self.path_parts()
        if path == "/pair" or path.startswith("/e2e/"):
            return _e2e().handle(self, path, f"http://127.0.0.1:{PORT}", PHONE_TOKEN)
        if not self.authed():
            return
        if UPLOAD_OK.match(path):
            return self.upload()
        if path == "/api/threads":
            return self.new_thread()
        if path == "/api/juggle/chats":
            return self.new_juggle_chat()
        if path == "/api/sessions/delete":
            return self.delete_sessions()
        m = REPLY_OK.match(path)
        if not m:
            return self.reply(404, b'{"error":"not allowed"}')
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 16384)
            body = json.loads(self.rfile.read(n) or b"{}")
            text = (body.get("text") or "").strip()
            images = [str(f) for f in (media_file(p) for p in body.get("images") or [])
                      if f and f.is_relative_to(UPLOADS.resolve())][:6]
        except Exception:
            return self.reply(400, b'{"error":"bad json"}')
        if not text and not images:
            return self.reply(400, b'{"error":"empty"}')
        text = compose(text, images)
        try:
            code, body = forward("GET", f"/api/sessions/{m.group(1)}")
            s = json.loads(body) if code == 200 else {}
            s = s.get("session", s)
        except Exception:
            return self.reply(502, b'{"error":"dashboard unreachable"}')
        key = s.get("session_key")
        jid = juggle_chat_id(key)
        if jid:
            return self.reply_juggle(jid, text)
        if not key or s.get("source") == "cron":
            return self.reply(409, b'{"error":"this session has no live chat to reply into"}')
        ok = dispatch({"session_key": key, "content": text})
        if ok is None:
            return self.reply(202, b'{"ok":true,"queued":true}')
        self.reply(200 if ok else 503, json.dumps({"ok": ok}).encode())

    def new_thread(self):
        """Start a new conversation from Juggle.

        Body: {"guild_id", "channel_id" | "new_channel", "title", "text", "images"?}.
        Optionally creates the channel, then a thread in it named *title*, adds
        Xun, posts the prompt so Discord shows it, and starts a Hermes session
        there. Only servers the bot is already in are accepted."""
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 16384)
            body = json.loads(self.rfile.read(n) or b"{}")
            guild = str(body.get("guild_id") or "")
            text = (body.get("text") or "").strip()
            title = (body.get("title") or "").strip()[:95] or text[:60] or "New thread"
            new_channel = re.sub(r"[^a-z0-9_-]+", "-", (body.get("new_channel") or "").strip().lower()).strip("-")[:90]
            images = [str(f) for f in (media_file(p) for p in body.get("images") or [])
                      if f and f.is_relative_to(UPLOADS.resolve())][:6]
        except Exception:
            return self.reply(400, b'{"error":"bad json"}')
        if not text and not images:
            return self.reply(400, b'{"error":"empty"}')
        try:
            known = channels()
            if not any(c["guild_id"] == guild for c in known):
                return self.reply(404, b'{"error":"unknown server"}')
            if new_channel:
                ch = discord("POST", f"/guilds/{guild}/channels", {"name": new_channel, "type": 0})
                channel_id, channel_name = ch["id"], ch["name"]
                global _channels
                _channels = (0.0, None)
            else:
                c = next((c for c in known if c["guild_id"] == guild and c["id"] == str(body.get("channel_id"))), None)
                if not c:
                    return self.reply(404, b'{"error":"unknown channel"}')
                channel_id, channel_name = c["id"], c["name"]
            gname = next(c["guild"] for c in known if c["guild_id"] == guild)
            th = discord("POST", f"/channels/{channel_id}/threads",
                         {"name": title, "type": 11, "auto_archive_duration": 10080})
            uid = env("DISCORD_ALLOWED_USERS").split(",")[0].strip()
            if uid:
                discord("PUT", f"/channels/{th['id']}/thread-members/{uid}")
            msg = discord("POST", f"/channels/{th['id']}/messages",
                          {"content": ("**From Juggle:** " + text)[:1990] if text else "**From Juggle:** (image)",
                           "allowed_mentions": {"parse": []}})
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode(errors="replace")
            return self.reply(502, json.dumps({"error": f"discord {e.code}", "detail": detail}).encode())
        except Exception as e:
            return self.reply(502, json.dumps({"error": f"discord unreachable: {type(e).__name__}"}).encode())
        origin = {"platform": "discord", "chat_id": th["id"], "chat_type": "thread",
                  "chat_name": f"{gname} / #{channel_name} / {title}", "thread_id": th["id"],
                  "parent_chat_id": channel_id, "guild_id": guild, "scope_id": guild,
                  "user_id": uid, "user_name": _owner_name(uid), "message_id": msg.get("id")}
        live = sys.modules.get("juggle_live")
        ok = False
        if live and getattr(live, "deliver_on", None):
            try:  # as the owner's own Discord message, so Hermes admits it
                ok = live.deliver_on("discord", origin, compose(text, images))
            except Exception:
                ok = False
        if ok is False:
            ok = dispatch({"origin": origin, "content": compose(text, images)})
        self.reply(200 if ok is not False else 503, json.dumps({
            "ok": ok is not False, "queued": ok is None, "thread_id": th["id"], "channel_id": channel_id,
            "session_key": f"agent:main:discord:thread:{th['id']}:{th['id']}"}).encode())

    def upload(self):
        """Raw image body -> ~/.hermes/juggle/uploads/<random>.<ext>; returns its path."""
        import secrets
        ext = UPLOAD_EXT.get(self.headers.get("Content-Type", "").split(";")[0].strip())
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not ext:
            return self.reply(415, b'{"error":"images only"}')
        if not 0 < n <= MAX_UPLOAD:
            return self.reply(413, b'{"error":"too large"}')
        data = self.rfile.read(n)
        UPLOADS.mkdir(parents=True, exist_ok=True)
        dest = UPLOADS / (time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4) + ext)
        dest.write_bytes(data)
        self.reply(200, json.dumps({"path": str(dest)}).encode())


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
