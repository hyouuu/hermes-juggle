# Juggle for Hermes

The Hermes side of [Juggle](https://apps.apple.com/app/id6831812171), an iPhone and Mac app for keeping many Hermes conversations moving at once. You can see which chats need you, which are working, and reply or start new ones from anywhere.

This plugin runs inside your Hermes gateway. It:

- serves a small local API over your Hermes sessions (list, read, reply, start a chat, images),
- dials **out** to the Juggle hub over a WebSocket, so your phone can reach Hermes without opening ports, a VPN or ssh,
- pairs devices with a QR code and encrypts every request end to end,
- adds **Juggle** as a Hermes chat platform, so chats you start in the app are Hermes conversations of their own (no Discord or Telegram needed).

## Install

```bash
hermes plugins install hyouuu/hermes-juggle
hermes config set plugins.entries.juggle-bridge.allow_gateway_injection true
hermes gateway restart
```

The second line lets the plugin hand your replies to Hermes as your own messages. Without it, replies to existing chats are refused.

## Pair a device

```bash
hermes juggle pair
```

This prints a QR code in your terminal. In Juggle, open **Settings > Scan pairing code** and scan it, or use `hermes juggle pair --open` on the Mac running Hermes to open the link directly. Codes are single use and expire after 10 minutes.

```bash
hermes juggle devices          # list paired devices
hermes juggle forget <name>    # unpair one; it can no longer reach Hermes
```

## Privacy and security

- **End to end encrypted.** Pairing is an X25519 key agreement between the device and this plugin, bound to the one-time code (HKDF-SHA256, HMAC-checked). Each request and response is sealed with ChaCha20-Poly1305 and carries a timestamp and nonce, so replays are rejected.
- **The hub sees only sealed boxes.** It forwards bytes between your devices and your gateway and logs only method, status and timing. It stores nothing. Its source is in [`hub/hub.py`](hub/hub.py), so you can read it or run your own.
- **Per-install identity.** On first run the plugin creates a random secret in `~/.hermes/juggle-bridge.secret` (mode 600). The hub routes your devices by a hash of it, so no other install can answer for yours.
- **Nothing listens publicly.** The local API binds to `127.0.0.1` only.
- **Narrow file access.** The app can only fetch images from Hermes' cache and Juggle's own upload folder.

## Self-hosting the hub

`hub/hub.py` is a single-file [aiohttp](https://docs.aiohttp.org) server (`pip install aiohttp`, then `python3 hub.py`; `JUGGLE_HUB_PORT` sets the port). Put it behind TLS and route `/juggle/` (HTTP) and `/juggle-bridge` (WebSocket) to it, then point the plugin at it:

```bash
JUGGLE_HUB_URL=wss://your.host/juggle-bridge
JUGGLE_HUB_PUBLIC=https://your.host/juggle/
```

Set both in the gateway's environment (for example `~/.hermes/.env`) and restart the gateway. Newly paired devices will use your hub.

## Files

- `__init__.py`: plugin entry. Starts the relay and the hub connection in the gateway and registers `hermes juggle`.
- `relay.py`: the local API the app talks to (127.0.0.1:9340).
- `e2e.py`: pairing and sealed requests.
- `platform.py`: the Juggle chat platform.
- `cli.py`: `hermes juggle pair | devices | forget`.
- `hub/hub.py`: the hub.

Requires the `cryptography` and `websockets` Python packages, which Hermes already ships with.

## License

MIT
