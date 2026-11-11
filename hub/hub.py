"""Juggle hub: pairs phone HTTPS requests with each user's Hermes bridge.

A Hermes machine dials OUT to /juggle-bridge over a WebSocket (no open ports,
no ssh), authenticating with its own random secret. The hub routes phone
requests for /juggle/b/<bridge id>/... to the bridge whose secret hashes to
that id. Requests are end-to-end encrypted between the phone and the bridge,
so the hub only moves sealed boxes. It keeps nothing: no bodies are stored or
logged, only method, status and timing.

Optional: a LEGACY_SECRET file (bridge.secret next to this script) also gets
the un-prefixed /juggle/... path, for clients paired before bridge ids existed.
"""
import asyncio
import base64
import hashlib
import hmac
import itertools
import logging
import os
import time
from pathlib import Path

from aiohttp import WSMsgType, web

HERE = Path(__file__).resolve().parent
_legacy = HERE / "bridge.secret"
LEGACY_SECRET = _legacy.read_text().strip() if _legacy.exists() else None
MAX_BRIDGES = int(os.environ.get("JUGGLE_HUB_MAX_BRIDGES", "5000"))
PORT = int(os.environ.get("JUGGLE_HUB_PORT", "9341"))
MAX_BODY = 16 * 1024 * 1024
TIMEOUT = 60
PASS_HEADERS = ("authorization", "content-type")

log = logging.getLogger("juggle-hub")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def bridge_id(secret):
    """Same derivation as the plugin's e2e.bridge_id."""
    return base64.urlsafe_b64encode(hashlib.sha256(b"juggle-bridge-id|" + secret.encode()).digest()[:16]).rstrip(b"=").decode()


class Hub:
    def __init__(self):
        self.bridges = {}  # bridge id -> WebSocket
        self.pending = {}
        self.ids = itertools.count(1)
        self.legacy_id = bridge_id(LEGACY_SECRET) if LEGACY_SECRET else None

    async def bridge(self, request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) < 7 + 32 or len(auth) > 7 + 256:
            return web.Response(status=401)
        bid = bridge_id(auth[7:])
        if bid not in self.bridges and len(self.bridges) >= MAX_BRIDGES:
            return web.Response(status=503)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=40 * 1024 * 1024)
        await ws.prepare(request)
        old, self.bridges[bid] = self.bridges.get(bid), ws
        if old is not None:
            await old.close()
        log.info("bridge connected (%d online)", len(self.bridges))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                frame = msg.json()
                entry = self.pending.get(frame.get("id"))
                if entry is None or entry[0] != bid:
                    continue  # a bridge can only answer its own requests
                fut = self.pending.pop(frame.get("id"))[1]
                if not fut.done():
                    fut.set_result(frame)
        finally:
            if self.bridges.get(bid) is ws:
                del self.bridges[bid]
                for owner, fut in list(self.pending.values()):
                    if owner == bid and not fut.done():
                        fut.set_exception(ConnectionError())
            log.info("bridge disconnected (%d online)", len(self.bridges))
        return ws

    async def phone(self, request):
        path = request.path_qs
        if path.startswith("/juggle/b/"):
            bid, _, rest = path[len("/juggle/b/"):].partition("/")
            path = "/juggle/" + rest
        else:
            bid = self.legacy_id
        ws = self.bridges.get(bid) if bid else None
        if ws is None or ws.closed:
            return web.json_response({"error": "Hermes is offline"}, status=502)
        body = await request.read()
        if len(body) > MAX_BODY:
            return web.Response(status=413)
        rid = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = (bid, fut)
        t0 = time.monotonic()
        try:
            await ws.send_json({
                "id": rid,
                "method": request.method,
                "path": path,
                "headers": {k: v for k, v in request.headers.items() if k.lower() in PASS_HEADERS},
                "body": base64.b64encode(body).decode(),
            })
            frame = await asyncio.wait_for(fut, TIMEOUT)
        except asyncio.TimeoutError:
            return web.json_response({"error": "Hermes did not answer"}, status=504)
        except ConnectionError:
            return web.json_response({"error": "Hermes is offline"}, status=502)
        finally:
            self.pending.pop(rid, None)
        log.info("%s %s %dms", request.method, frame.get("status"), (time.monotonic() - t0) * 1000)
        return web.Response(status=frame.get("status", 502),
                            body=base64.b64decode(frame.get("body") or ""),
                            content_type=(frame.get("content_type") or "application/json").split(";")[0])

    async def health(self, request):
        bid = request.query.get("bridge") or self.legacy_id
        ws = self.bridges.get(bid) if bid else None
        return web.json_response({"hub": True, "bridge": ws is not None and not ws.closed})


def main():
    hub = Hub()
    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_get("/juggle-bridge", hub.bridge)
    app.router.add_get("/juggle-hub/health", hub.health)
    app.router.add_route("*", "/juggle/{tail:.*}", hub.phone)
    web.run_app(app, host="127.0.0.1", port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
