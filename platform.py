"""Juggle as a Hermes messaging platform.

Chats started from the Juggle app live on their own platform ("juggle"), so they
need neither Discord nor Telegram. Every request reaching this adapter came
through the paired, end-to-end sealed relay, so senders are already
authenticated: the platform registers as ``trusted_inbound``.

Inbound: the relay thread calls ``deliver(chat_id, text)``; that hands a
MessageEvent to the gateway on its own loop, exactly like a typed message.
Outbound: Hermes stores replies in the session transcript, which Juggle reads,
so ``send`` only acknowledges.
"""
import os
import asyncio
import json
import logging
import sys
import threading
import time
import types
import uuid
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("juggle-platform")

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
CHATS = HOME / "juggle" / "chats.json"
SECRET_FILE = HOME / "juggle-bridge.secret"
NAME = "juggle"
KEY_PREFIX = "agent:main:juggle:dm:"
MAX_MESSAGE_LENGTH = 32000


def _live():
    """State that survives plugin reloads (the connected adapter and its loop)."""
    mod = sys.modules.get("juggle_live")
    if mod is None:
        mod = types.ModuleType("juggle_live")
        mod.adapter, mod.loop = None, None
        sys.modules["juggle_live"] = mod
    return mod


def _chats() -> dict:
    try:
        return json.loads(CHATS.read_text())
    except Exception:
        return {}


def _save_chat(chat_id: str, title: str) -> None:
    chats = _chats()
    if chat_id not in chats:
        chats[chat_id] = {"title": title, "created": int(time.time())}
        CHATS.parent.mkdir(parents=True, exist_ok=True)
        tmp = CHATS.with_suffix(".tmp")
        tmp.write_text(json.dumps(chats))
        tmp.replace(CHATS)


def title_for(text: str) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("[")), "")
    return (line[:60].rstrip() + ("…" if len(line) > 60 else "")) or "New chat"


def new_chat_id() -> str:
    return "j" + uuid.uuid4().hex[:16]


def chat_id_from_key(session_key: str) -> Optional[str]:
    return session_key[len(KEY_PREFIX):] if (session_key or "").startswith(KEY_PREFIX) else None


def running() -> bool:
    live = _live()
    return bool(live.adapter and live.loop and live.loop.is_running())


def deliver(chat_id: str, text: str, title: Optional[str] = None, timeout: float = 15) -> bool:
    """Called from the relay thread: queue ``text`` as the user's turn in a Juggle chat."""
    live = _live()
    if not running():
        raise RuntimeError("the Juggle platform is not running in the gateway")
    _save_chat(chat_id, title or title_for(text))
    fut = asyncio.run_coroutine_threadsafe(live.adapter.receive(chat_id, text), live.loop)
    fut.result(timeout)
    return True


def _make_adapter_class():
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.platforms.event import MessageEvent, MessageType

    class JuggleAdapter(BasePlatformAdapter):
        MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

        def __init__(self, config):
            super().__init__(config=config, platform=Platform(NAME))

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            live = _live()
            live.adapter, live.loop = self, asyncio.get_running_loop()
            self._mark_connected()
            self._wire_plugin_handlers(None)
            log.info("[juggle] platform connected")
            return True

        async def disconnect(self) -> None:
            live = _live()
            if live.adapter is self:
                live.adapter = None
            self._mark_disconnected()

        async def receive(self, chat_id: str, text: str) -> None:
            title = (_chats().get(chat_id) or {}).get("title") or "Juggle chat"
            msg_id = uuid.uuid4().hex
            source = self.build_source(chat_id=chat_id, chat_name=title, chat_type="dm",
                                       user_id="juggle-owner", user_name="Juggle", message_id=msg_id)
            event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                                 message_id=msg_id, raw_message={"chat_id": chat_id})
            await self.handle_message(event)

        async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                       metadata: Optional[dict[str, Any]] = None):
            # The reply is already in the session transcript Juggle reads.
            return SendResult(success=True, message_id=uuid.uuid4().hex)

        async def send_typing(self, chat_id: str, metadata: Optional[dict] = None) -> None:
            return None

        async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
            return {"name": (_chats().get(chat_id) or {}).get("title") or chat_id, "type": "dm"}

    return JuggleAdapter


def register(ctx) -> None:
    cls = _make_adapter_class()
    ctx.register_platform(
        name=NAME, label="Juggle", adapter_factory=lambda cfg: cls(cfg),
        check_fn=lambda: True, validate_config=lambda cfg: True, required_env=[],
        is_connected=lambda cfg: SECRET_FILE.exists(),
        env_enablement_fn=lambda: {"paired": True} if SECRET_FILE.exists() else None,
        trusted_inbound=True,  # every sender is a paired device over the sealed relay
        dashboard_description="Chats started from the Juggle app (paired, end-to-end encrypted).",
        max_message_length=MAX_MESSAGE_LENGTH, emoji="🤹", pii_safe=True,
        platform_hint=("You are talking with the user in the Juggle app, which shows full "
                       "markdown. Your replies appear there as you write them."),
    )


def deliver_on(platform: str, origin: dict, text: str, timeout: float = 15) -> bool:
    """Hand ``text`` to another running adapter (e.g. Discord) as the owner's own message,
    so a chat Juggle opened there starts like one typed in that app."""
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run import _gateway_runner_ref
    runner, loop = _gateway_runner_ref(), _live().loop or _main_loop()
    adapter = runner and runner.adapters.get(Platform(platform))
    if adapter is None or loop is None:
        raise RuntimeError(f"{platform} is not connected in the gateway")
    fields = ("chat_id", "chat_name", "chat_type", "user_id", "user_name", "thread_id",
              "parent_chat_id", "guild_id", "scope_id", "message_id")
    kw = {k: origin[k] for k in fields if origin.get(k) is not None}

    async def go():
        try:
            source = adapter.build_source(**kw)
        except TypeError:  # adapter doesn't take every optional field
            source = adapter.build_source(**{k: kw[k] for k in ("chat_id", "chat_name", "chat_type",
                                                                "user_id", "user_name", "thread_id") if k in kw})
        event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                             message_id=origin.get("message_id"), raw_message={"via": "juggle"})
        await adapter.handle_message(event)

    asyncio.run_coroutine_threadsafe(go(), loop).result(timeout)
    return True


_live().deliver_on = deliver_on
_live().deliver = deliver
_live().new_chat_id = new_chat_id
_live().running = running


# ---- bring the platform up in a gateway that is already running ------------

def _main_loop():
    import gc
    main = threading.main_thread().ident
    for o in gc.get_objects():
        if isinstance(o, asyncio.AbstractEventLoop) and getattr(o, "_thread_id", None) == main \
                and o.is_running():
            return o
    return None


async def _install(runner) -> str:
    from gateway.config import Platform, PlatformConfig
    p = Platform(NAME)
    if p in runner.adapters:
        return "already running"
    platforms = getattr(runner.config, "platforms", None) or {}
    cfg = platforms.get(p) if isinstance(platforms, dict) else None
    cfg = cfg or PlatformConfig(enabled=True, extra={"paired": True})
    adapter = runner._create_adapter(p, cfg)
    if adapter is None:
        return "no adapter"
    runner._wire_adapter_handlers(adapter)
    if not await adapter.connect():
        return "connect failed"
    runner._publish_primary_adapter(p, adapter)
    runner.delivery_router.adapters = runner.adapters
    return "installed"


def start_live() -> None:
    """If this gateway booted before the platform existed, install it now (no restart)."""
    def run():
        for _ in range(60):
            try:
                from gateway.run import _gateway_runner_ref
                runner, loop = _gateway_runner_ref(), _main_loop()
            except Exception:
                runner = loop = None
            if runner is not None and loop is not None and getattr(runner, "adapters", None) is not None:
                if running():
                    return
                time.sleep(5)  # let normal startup connect it first
                if running():
                    return
                try:
                    res = asyncio.run_coroutine_threadsafe(_install(runner), loop).result(30)
                    log.info("[juggle] live install: %s", res)
                except Exception:
                    log.exception("[juggle] live install failed")
                return
            time.sleep(2)
    threading.Thread(target=run, name="juggle-platform-install", daemon=True).start()
