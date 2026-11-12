"""Direct route: lets a paired phone reach this Mac without the hub.

When the phone is on the same network (or the same Tailscale tailnet), it can
send its sealed requests straight here instead of through the hub. This
listener accepts exactly what the hub forwards, nothing more:

  GET  /juggle-direct/ping   -> {"juggle": true}   (reachability check only)
  POST /e2e/<device_id>      -> the same sealed box the hub would carry

Everything else is 404. There is no pairing, no bearer route and no plain API
here, so anyone on the network who isn't a paired device can't read or do
anything. Requests are end-to-end encrypted and replay-checked by e2e.py, so
plain HTTP on the wire is fine.

Turn it off with JUGGLE_DIRECT=0; change the port with JUGGLE_DIRECT_PORT.
"""

import ipaddress
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENABLED = os.environ.get("JUGGLE_DIRECT", "1") != "0"
PORT = int(os.environ.get("JUGGLE_DIRECT_PORT", "9342"))
MAX_BODY = 20 * 1024 * 1024
TAILNET = ipaddress.ip_network("100.64.0.0/10")


def _interface_ips():
    """IPv4 addresses on this machine's interfaces, no extra packages."""
    out = ""
    for cmd in (["ifconfig"], ["ip", "-4", "-o", "addr"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
        except Exception:
            continue
        if out:
            break
    return re.findall(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", out)


def addresses():
    """Base URLs a phone can try, tailnet first (works away from home too)."""
    if not ENABLED:
        return []
    tail, lan = [], []
    for raw in _interface_ips():
        ip = ipaddress.ip_address(raw)
        if ip in TAILNET:
            tail.append(raw)
        elif ip.is_private and not ip.is_loopback and not ip.is_link_local:
            lan.append(raw)
    return [f"http://{ip}:{PORT}/" for ip in dict.fromkeys(tail + lan)][:4]


def make_handler(relay):
    """relay = (host, port) of the local relay, which opens the sealed box."""
    class Direct(BaseHTTPRequestHandler):
        server_version = "Juggle"

        def log_message(self, format, *args):
            pass

        def reply(self, code, data=b"{}", ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/juggle-direct/ping":
                return self.reply(200, json.dumps({"juggle": True}).encode())
            self.reply(404, b'{"error":"not found"}')

        def do_POST(self):
            if not re.fullmatch(r"/e2e/[A-Za-z0-9_-]{1,64}", self.path):
                return self.reply(404, b'{"error":"not found"}')
            n = int(self.headers.get("Content-Length", 0) or 0)
            if not 0 < n <= MAX_BODY:
                return self.reply(413, b'{"error":"too large"}')
            req = urllib.request.Request(f"http://{relay[0]}:{relay[1]}{self.path}", data=self.rfile.read(n),
                                         method="POST", headers={"Content-Type": "application/octet-stream"})
            try:
                with urllib.request.urlopen(req, timeout=58) as r:
                    status, ctype, out = r.status, r.headers.get("Content-Type"), r.read()
            except urllib.error.HTTPError as e:
                status, ctype, out = e.code, e.headers.get("Content-Type"), e.read()
            except Exception:
                status, ctype, out = 502, "application/json", b'{"error":"relay unavailable"}'
            self.reply(status, out, ctype)

    return Direct


def serve(relay):
    """Blocking. Call from a daemon thread."""
    server = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(relay))
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()
