"""Juggle bridge: keeps the Juggle phone/Mac app connected to this Hermes.

Runs inside the gateway, so it is up whenever Hermes is, and restarts with it:
  * serves the Juggle relay API on 127.0.0.1:9340 (the whitelist in
    ~/.hermes/scripts/juggle-relay.py; the dashboard token never leaves here),
  * dials OUT to the Juggle hub over a WebSocket and answers phone requests
    through it (no open ports, no ssh tunnel), reconnecting on its own,
  * keeps the local dashboard (127.0.0.1:9119) running, started detached so
    it survives gateway restarts.
"""
import asyncio
import base64
import fcntl
import importlib.util
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("juggle-bridge")

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
RELAY_SCRIPT = Path(__file__).resolve().parent / "relay.py"
SECRET_FILE = HOME / "juggle-bridge.secret"
LOCK_FILE = HOME / "juggle-bridge.lock"
HUB_URL = os.environ.get("JUGGLE_HUB_URL", "wss://api.lycheeisle.com/juggle-bridge")
RELAY = ("127.0.0.1", 9340)
DASH = ("127.0.0.1", 9119)
HERMES_BIN = shutil.which("hermes") or str(HOME / "hermes-agent" / ".hermes" / "bin" / "hermes")

_started = False
_lock_fd = None
state = {"hub": "starting", "relay": "starting", "since": None}


def _port_open(addr, timeout=1.0):
    try:
        with socket.create_connection(addr, timeout=timeout):
            return True
    except OSError:
        return False


# ---- local relay -----------------------------------------------------------

def _hot_swap_relay():
    """On a plugin reload the old relay thread keeps serving with the old
    handler class. Point it at freshly loaded code so relay changes go live
    without restarting the gateway."""
    import gc
    import socketserver
    spec = importlib.util.spec_from_file_location("juggle_relay", RELAY_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules.pop("juggle_e2e", None)
    n = 0
    for o in gc.get_objects():
        if isinstance(o, socketserver.BaseServer) and getattr(o, "server_address", None) == RELAY:
            o.RequestHandlerClass = mod.Handler
            n += 1
    return n


def _relay_loop():
    """Serve the relay in-process. If a standalone relay still holds the port,
    wait; once it goes away, take over."""
    spec = importlib.util.spec_from_file_location("juggle_relay", RELAY_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    while True:
        try:
            server = ThreadingHTTPServer(RELAY, mod.Handler)
        except OSError:
            state["relay"] = "external" if _port_open(RELAY) else "bind-failed"
            time.sleep(10)
            continue
        state["relay"] = "in-process"
        log.info("juggle-bridge: relay serving on %s:%d", *RELAY)
        try:
            server.serve_forever()
        except Exception:
            log.exception("juggle-bridge: relay crashed")
        finally:
            server.server_close()
        time.sleep(2)


# ---- dashboard keeper ------------------------------------------------------

def _dashboard_loop():
    misses = 0
    while True:
        misses = 0 if _port_open(DASH) else misses + 1
        if misses >= 2:
            env = {k: v for k, v in os.environ.items() if k != "_HERMES_GATEWAY"}
            (HOME / "logs").mkdir(parents=True, exist_ok=True)
            out = open(HOME / "logs" / "juggle-dashboard.log", "ab")
            subprocess.Popen([HERMES_BIN, "dashboard", "--no-open"], env=env,
                             stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                             start_new_session=True, close_fds=True)
            log.warning("juggle-bridge: dashboard was down, started it")
            misses = -6  # give it ~90 s to come up before trying again
        time.sleep(15)


# ---- direct route (same network / tailnet) ---------------------------------

def _ensure_direct():
    """Start the direct listener once per gateway process, including after a plugin reload."""
    if any(th.name == "juggle-bridge-direct" and th.is_alive() for th in threading.enumerate()):
        return
    threading.Thread(target=_direct_loop, name="juggle-bridge-direct", daemon=True).start()


def _direct_loop():
    from . import direct
    if not direct.ENABLED:
        return
    warned = False
    while True:
        try:
            direct.serve(RELAY)
        except OSError as e:
            if not warned:
                log.warning("juggle-bridge: direct route unavailable (%s); phones will use the hub", e)
                warned = True
        except Exception:
            log.exception("juggle-bridge: direct route crashed; retrying in 60s")
        time.sleep(60)


# ---- hub connection --------------------------------------------------------

def _local(frame):
    """Answer one phone request by calling the local relay."""
    req = urllib.request.Request(
        f"http://{RELAY[0]}:{RELAY[1]}{frame['path']}",
        data=base64.b64decode(frame.get("body") or "") or None,
        method=frame.get("method", "GET"),
        headers=frame.get("headers") or {},
    )
    try:
        with urllib.request.urlopen(req, timeout=55) as r:
            status, ctype, body = r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        status, ctype, body = e.code, e.headers.get("Content-Type"), e.read()
    except Exception:
        status, ctype, body = 502, "application/json", b'{"error":"relay unavailable"}'
    return {"id": frame["id"], "status": status, "content_type": ctype,
            "body": base64.b64encode(body).decode()}


async def _hub_session(websockets, secret):
    async with websockets.connect(
        HUB_URL,
        additional_headers={"Authorization": f"Bearer {secret}"},
        max_size=40 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
        open_timeout=15,
    ) as ws:
        state["hub"], state["since"] = "connected", time.time()
        log.info("juggle-bridge: connected to hub")
        loop = asyncio.get_running_loop()

        async def answer(frame):
            reply = await loop.run_in_executor(None, _local, frame)
            await ws.send(json.dumps(reply))

        tasks = set()
        async for raw in ws:
            if not isinstance(raw, str):
                continue
            t = asyncio.create_task(answer(json.loads(raw)))
            tasks.add(t)
            t.add_done_callback(tasks.discard)


def _hub_loop():
    import websockets
    secret = SECRET_FILE.read_text().strip()

    async def run():
        delay = 1
        while True:
            try:
                await _hub_session(websockets, secret)
                delay = 1
            except Exception as e:
                state["hub"] = f"retrying ({type(e).__name__})"
                log.info("juggle-bridge: hub connection lost (%s), retry in %ss", type(e).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    asyncio.run(run())


# ---- plugin entry ----------------------------------------------------------

def _ensure_secret():
    """Each install gets its own random bridge secret (no crypto import here,
    so this works even where the cryptography wheel is unavailable)."""
    if SECRET_FILE.exists():
        return
    import secrets as _s
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as f:
        f.write(_s.token_urlsafe(32))


def _single_instance():
    global _lock_fd
    _lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def register(ctx) -> None:
    global _started
    from . import cli
    try:
        if os.environ.get("_HERMES_GATEWAY") == "1" and "dashboard" not in sys.argv:
            _hot_swap_relay()
            if SECRET_FILE.exists():
                _ensure_direct()
    except Exception:
        log.exception("juggle-bridge: relay hot swap failed")
    # The relay hands replies to existing sessions to Hermes through this.
    import types
    rt = sys.modules.setdefault("juggle_runtime", types.ModuleType("juggle_runtime"))
    if getattr(ctx, "inject_message", None) is not None:
        rt.inject = ctx.inject_message
    is_gateway = os.environ.get("_HERMES_GATEWAY") == "1" and "dashboard" not in sys.argv
    if is_gateway:
        try:
            _ensure_secret()
        except Exception:
            log.exception("juggle-bridge: could not create the bridge secret")
    from . import platform as juggle_platform
    try:
        juggle_platform.register(ctx)
    except Exception:
        log.exception("juggle-bridge: platform registration failed")
    if os.environ.get("_HERMES_GATEWAY") == "1" and "dashboard" not in sys.argv and SECRET_FILE.exists():
        juggle_platform.start_live()
    ctx.register_cli_command(name="juggle", help="Pair devices with Juggle and manage them",
                             setup_fn=cli.setup, handler_fn=cli.dispatch)
    if _started or os.environ.get("_HERMES_GATEWAY") != "1" or "dashboard" in sys.argv:
        return  # only the gateway hosts the bridge, never CLI/dashboard processes
    if not SECRET_FILE.exists() or not _single_instance():
        return
    _started = True
    for name, fn in (("relay", _relay_loop), ("hub", _hub_loop), ("dashboard", _dashboard_loop)):
        threading.Thread(target=fn, name=f"juggle-bridge-{name}", daemon=True).start()
