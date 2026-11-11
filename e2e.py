"""Juggle end-to-end encryption and device pairing.

Pairing: `hermes juggle pair` makes a one-time code (10 minutes) and shows it as a
QR code holding a juggle://pair link: hub URL, pair id, code, and this Mac's
X25519 public key. The app makes its own X25519 key and sends
POST /pair {pair_id, device_pub, name, mac=HMAC(code, device_pub|name)}.
Both sides derive the device key = HKDF-SHA256(X25519(...), salt=code,
info="juggle-e2e-v1"). The hub only ever sees public keys and an HMAC, so it can't
derive the key, and the code alone (someone who saw the QR) isn't enough either.

Requests: POST /e2e/<device_id>, body = ChaCha20-Poly1305 sealed box
(nonce | ciphertext | tag, AAD = device id) of
  u32 header length | header JSON {ts, method, path, ctype} | raw body.
The reply is sealed the same way with header {status, ctype}. Inside, the
request goes to the local relay, so the whitelist and limits are unchanged.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
KEY_FILE = HOME / "juggle-bridge.key"
DEVICES_FILE = HOME / "juggle-devices.json"
PAIRING_FILE = HOME / "juggle-pairing.json"
HUB_BASE = os.environ.get("JUGGLE_HUB_PUBLIC", "https://api.lycheeisle.com/juggle/")
SECRET_FILE = HOME / "juggle-bridge.secret"


def bridge_id(secret):
    """Public routing id for this bridge at the hub; hub.py computes the same."""
    return base64.urlsafe_b64encode(hashlib.sha256(b"juggle-bridge-id|" + secret.encode()).digest()[:16]).rstrip(b"=").decode()


def ensure_secret():
    """Each install gets its own random bridge secret (it only ever leaves this
    machine to authenticate to the hub). Returns it."""
    if not SECRET_FILE.exists():
        import secrets as _s
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(_s.token_urlsafe(32))
        except FileExistsError:
            pass
    return SECRET_FILE.read_text().strip()


def hub_public():
    """The hub URL phones use for this bridge: <hub>/b/<bridge id>/."""
    base = HUB_BASE if HUB_BASE.endswith("/") else HUB_BASE + "/"
    return f"{base}b/{bridge_id(ensure_secret())}/"


def hub_health():
    """(hub reachable, this bridge connected) as the hub sees it."""
    import json as _j
    import urllib.request
    base = HUB_BASE.rstrip("/")
    root = base[: -len("/juggle")] if base.endswith("/juggle") else base
    try:
        url = f"{root}/juggle-hub/health?bridge={bridge_id(ensure_secret())}"
        with urllib.request.urlopen(url, timeout=8) as r:
            d = _j.loads(r.read())
        return bool(d.get("hub")), bool(d.get("bridge"))
    except Exception:
        return False, False
INFO = b"juggle-e2e-v1"
PAIR_TTL = 600
SKEW = 300
MAX_BODY = 16 * 1024 * 1024 + 4096

_lock = threading.Lock()
_seen = {}  # nonce -> time, for replay protection
_fails = []


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _write_private(path, data):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def bridge_key():
    if not KEY_FILE.exists():
        k = X25519PrivateKey.generate()
        _write_private(KEY_FILE, k.private_bytes(serialization.Encoding.Raw,
                                                 serialization.PrivateFormat.Raw,
                                                 serialization.NoEncryption()))
    return X25519PrivateKey.from_private_bytes(KEY_FILE.read_bytes())


def bridge_pub():
    return bridge_key().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def derive(shared, code):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=code, info=INFO).derive(shared)


def devices():
    return _load(DEVICES_FILE, {})


def save_devices(d):
    _write_private(DEVICES_FILE, json.dumps(d, indent=1).encode())


# ---- pairing ---------------------------------------------------------------

def new_pairing(hub=None):
    """Create a one-time pairing; returns the juggle://pair link."""
    pid, code = b64u(secrets.token_bytes(9)), secrets.token_bytes(16)
    with _lock:
        p = {k: v for k, v in _load(PAIRING_FILE, {}).items() if v["expires"] > time.time()}
        p[pid] = {"code": b64u(code), "expires": time.time() + PAIR_TTL}
        _write_private(PAIRING_FILE, json.dumps(p).encode())
    from urllib.parse import urlencode
    q = urlencode({"hub": hub or hub_public(), "id": pid, "code": b64u(code), "pk": b64u(bridge_pub()),
                   "name": os.uname().nodename.split(".")[0]})
    return f"juggle://pair?{q}"


def pair(body):
    """-> (status, dict)"""
    now = time.time()
    with _lock:
        _fails[:] = [t for t in _fails if now - t < 60]
        if len(_fails) >= 10:
            return 429, {"error": "slow down"}
    try:
        req = json.loads(body)
        pid, dpub = req["pair_id"], unb64u(req["device_pub"])
        name = str(req.get("name") or "Device")[:60]
        mac = unb64u(req["mac"])
    except Exception:
        return 400, {"error": "bad request"}
    with _lock:
        pending = _load(PAIRING_FILE, {})
        entry = pending.get(pid)
        code = unb64u(entry["code"]) if entry and entry["expires"] > now else None
        want = hmac.new(code, dpub + name.encode(), hashlib.sha256).digest() if code else b""
        if not code or len(dpub) != 32 or not hmac.compare_digest(want, mac):
            _fails.append(now)
            return 403, {"error": "pairing code is wrong or expired"}
        pending.pop(pid, None)  # one-time
        _write_private(PAIRING_FILE, json.dumps(pending).encode())
        key = derive(bridge_key().exchange(X25519PublicKey.from_public_bytes(dpub)), code)
        did = b64u(secrets.token_bytes(12))
        d = devices()
        d[did] = {"name": name, "key": b64u(key), "paired": int(now), "seen": int(now)}
        save_devices(d)
    proof = hmac.new(key, b"paired|" + did.encode(), hashlib.sha256).digest()
    return 200, {"device_id": did, "proof": b64u(proof)}


# ---- sealed requests -------------------------------------------------------

def pack(header, body=b""):
    h = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack(">I", len(h)) + h + body


def unpack(data):
    n = struct.unpack(">I", data[:4])[0]
    return json.loads(data[4:4 + n]), data[4 + n:]


def seal(key, did, plain):
    nonce = secrets.token_bytes(12)
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, plain, did.encode())


def open_box(key, did, box):
    return ChaCha20Poly1305(key).decrypt(box[:12], box[12:], did.encode())


def _fresh(nonce, ts):
    now = time.time()
    if abs(now - ts) > SKEW:
        return False
    with _lock:
        for k in [k for k, t in _seen.items() if now - t > 2 * SKEW]:
            del _seen[k]
        if nonce in _seen:
            return False
        _seen[nonce] = now
    return True


def sealed(did, box, relay_url, relay_token):
    """-> (status, content_type, body). Errors before decryption are plain JSON."""
    dev = devices().get(did)
    if not dev:
        return 401, "application/json", b'{"error":"unknown device"}'
    key = unb64u(dev["key"])
    try:
        head, body = unpack(open_box(key, did, box))
    except Exception:
        return 400, "application/json", b'{"error":"bad box"}'
    path = str(head.get("path", ""))
    if not _fresh(box[:12], float(head.get("ts", 0))) or not path.startswith("/api/"):
        return 400, "application/json", b'{"error":"stale or bad request"}'
    req = urllib.request.Request(relay_url + path, data=body or None, method=head.get("method", "GET"))
    req.add_header("Authorization", f"Bearer {relay_token}")
    if head.get("ctype"):
        req.add_header("Content-Type", head["ctype"])
    try:
        with urllib.request.urlopen(req, timeout=55) as r:
            status, ctype, out = r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        status, ctype, out = e.code, e.headers.get("Content-Type"), e.read()
    except Exception:
        status, ctype, out = 502, "application/json", b'{"error":"relay unavailable"}'
    if time.time() - dev.get("seen", 0) > 60:
        with _lock:
            d = devices()
            if did in d:
                d[did]["seen"] = int(time.time())
                save_devices(d)
    return 200, "application/octet-stream", seal(key, did, pack({"status": status, "ctype": ctype}, out))


def handle(handler, path, relay_url, relay_token):
    """Serve /pair and /e2e/<id> on the relay's BaseHTTPRequestHandler."""
    n = int(handler.headers.get("Content-Length", 0) or 0)
    if not 0 < n <= MAX_BODY:
        return handler.reply(413, b'{"error":"too large"}')
    body = handler.rfile.read(n)
    if path == "/pair":
        code, obj = pair(body)
        if code != 200:
            time.sleep(1)
        return handler.reply(code, json.dumps(obj).encode())
    status, ctype, out = sealed(path[len("/e2e/"):], body, relay_url, relay_token)
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(out)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(out)
