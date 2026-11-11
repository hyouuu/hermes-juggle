"""`hermes juggle ...`: pair phones and Macs with this Hermes, list and remove them."""
import subprocess
import sys
import time

QR_SWIFT = r"""
import CoreImage
let f = CIFilter(name: "CIQRCodeGenerator")!
f.setValue(CommandLine.arguments[1].data(using: .utf8), forKey: "inputMessage")
f.setValue("M", forKey: "inputCorrectionLevel")
let img = f.outputImage!
let ctx = CIContext()
let w = Int(img.extent.width), h = Int(img.extent.height)
var px = [UInt8](repeating: 0, count: w * h * 4)
ctx.render(img, toBitmap: &px, rowBytes: w * 4, bounds: img.extent, format: .RGBA8, colorSpace: nil)
for y in 0..<h { print(String((0..<w).map { px[((h - 1 - y) * w + $0) * 4] < 128 ? "1" : "0" })) }
"""


def _matrix(text):
    try:
        import qrcode
        q = qrcode.QRCode(border=0)
        q.add_data(text)
        return [[bool(c) for c in row] for row in q.get_matrix()]
    except ImportError:
        pass
    if sys.platform == "darwin":
        out = subprocess.run(["swift", "-", text], input=QR_SWIFT, capture_output=True, text=True, timeout=120)
        rows = [r for r in out.stdout.split() if set(r) <= {"0", "1"}]
        if rows:
            return [[c == "1" for c in r] for r in rows]
    return None


def _render(m):
    """Two rows per line with half blocks, dark modules on a light quiet zone."""
    pad = 2
    w = len(m[0]) + 2 * pad
    grid = [[False] * w for _ in range(pad)] + [[False] * pad + r + [False] * pad for r in m] + [[False] * w for _ in range(pad + 1)]
    lines = []
    for y in range(0, len(grid) - 1, 2):
        top, bot = grid[y], grid[y + 1]
        lines.append("\033[47;30m" + "".join(
            "█" if a and b else "▀" if a else "▄" if b else " " for a, b in zip(top, bot)) + "\033[0m")
    return "\n".join(lines)


def setup(p):
    sub = p.add_subparsers(dest="juggle_cmd")
    pr = sub.add_parser("pair", help="Show a one-time QR code to pair a device")
    pr.add_argument("--open", action="store_true", help="Pair the Juggle app on this Mac instead of showing a code")
    pr.add_argument("--hub", help="Hub URL the device should use (default: the Juggle hub)")
    sub.add_parser("devices", help="List paired devices")
    fg = sub.add_parser("forget", help="Unpair a device")
    fg.add_argument("device", help="Device id or name from `hermes juggle devices`")


def _injection_allowed():
    try:
        from hermes_cli.config import load_config_readonly
        entry = ((load_config_readonly() or {}).get("plugins") or {}).get("entries", {}).get("juggle-bridge") or {}
        return entry.get("allow_gateway_injection") is True
    except Exception:
        return True  # can't tell; don't nag


def _preflight(e2e):
    out = []
    if not _injection_allowed():
        out.append("Messages from Juggle can't reach Hermes until you allow it:\n"
                   "      hermes config set plugins.entries.juggle-bridge.allow_gateway_injection true")
    hub, bridge = e2e.hub_health()
    if not bridge:
        out.append("The gateway isn't connected to the Juggle hub" + ("" if hub else " (hub unreachable)") +
                   ". Restart the gateway (`hermes gateway restart`) so the plugin loads, then pair.")
    return out


def dispatch(args):
    from . import e2e
    cmd = getattr(args, "juggle_cmd", None)
    if cmd == "pair":
        problems = _preflight(e2e)
        if problems:
            print("Juggle isn't ready yet:\n")
            for line in problems:
                print("  - " + line)
            print()
        link = e2e.new_pairing(args.hub)
        if args.open:
            subprocess.run(["open", "-g", link], check=False)
            print("Sent the pairing link to Juggle on this Mac. It expires in 10 minutes.")
            return 0
        m = _matrix(link)
        if m:
            print(_render(m))
            print("\nIn Juggle: Settings > Scan pairing code. The code works once and expires in 10 minutes.")
        else:
            print("No QR renderer here (pip install qrcode). Paste this into Juggle > Settings instead:\n")
            print(link)
        print("Keep this code to yourself: anyone who scans it before you can pair.")
        return 0
    if cmd == "devices":
        d = e2e.devices()
        if not d:
            print("No paired devices. Run `hermes juggle pair`.")
        for did, v in sorted(d.items(), key=lambda kv: -kv[1].get("seen", 0)):
            seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(v.get("seen", 0)))
            print(f"{did}  {v.get('name', '?'):<24} last seen {seen}")
        return 0
    if cmd == "forget":
        d = e2e.devices()
        hits = [k for k, v in d.items() if args.device in (k, v.get("name"))]
        if len(hits) != 1:
            print("No such device." if not hits else "Several devices have that name; use the id.")
            return 1
        d.pop(hits[0])
        e2e.save_devices(d)
        print("Unpaired. That device can no longer reach Hermes.")
        return 0
    print("usage: hermes juggle {pair,devices,forget}")
    return 1
