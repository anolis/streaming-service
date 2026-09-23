"""linuxcast: a Windows-style "Cast to device" for Linux."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from linuxcast import capture, session
from linuxcast.backends import BACKENDS
from linuxcast.base import BackendUnavailable, CaptureOptions, Device

ICON = "video-display-symbolic"


class Failure(Exception):
    """A user-facing error; shown as a notification when --notify is on."""


def notify(args, title: str, body: str = "", urgent: bool = False):
    if getattr(args, "notify", False) and shutil.which("notify-send"):
        cmd = ["notify-send", "-a", "linuxcast", "-i", ICON, title, body]
        if urgent:
            cmd[1:1] = ["-u", "critical"]
        subprocess.run(cmd, check=False)


def discover_all(backends: list[str] | None, timeout: float) -> list[Device]:
    usable = [b for n, b in BACKENDS.items() if (not backends or n in backends) and b.available()[0]]
    with ThreadPoolExecutor(len(usable) or 1) as pool:
        results = pool.map(lambda b: b.discover(timeout), usable)
    return [d for devs in results for d in devs]


def _gui_pick(devices: list[Device]) -> Device | None:
    rows = []
    for i, d in enumerate(devices):
        rows += [str(i), d.name, d.backend, d.host or ""]
    r = subprocess.run(
        ["zenity", "--list", "--title=Cast to device", "--text=Choose a device",
         "--column=#", "--column=Device", "--column=Type", "--column=Address",
         "--hide-column=1", "--print-column=1", "--width=420", "--height=300", *rows],
        capture_output=True, text=True)
    choice = r.stdout.strip().split("|")[0]
    return devices[int(choice)] if choice.isdigit() else None


def choose_device(args) -> Device:
    if getattr(args, "target", None):  # a device the caller (the applet) already discovered
        fields = json.loads(args.target)
        fields.pop("castable", None)
        return Device(**fields)
    devices = discover_all(args.backend and [args.backend], args.timeout)
    devices = [d for d in devices if BACKENDS[d.backend].can_cast]
    if args.device:
        q = args.device.lower()
        matches = [d for d in devices if q in d.name.lower() or q == d.id or q == d.host]
    else:
        matches = devices
    if not matches:
        raise Failure(f"No matching cast device found ({len(devices)} discovered)")
    if len(matches) == 1:
        return matches[0]
    if getattr(args, "gui", False) and shutil.which("zenity"):
        picked = _gui_pick(matches)
        if not picked:
            raise KeyboardInterrupt  # dialog cancelled
        return picked
    if not sys.stdin.isatty():
        raise Failure("Several devices match; pass --device NAME")
    for i, d in enumerate(matches, 1):
        print(f"  {i}) {d.label()}")
    while True:
        pick = input("Cast to which device? ")
        if pick.isdigit() and 1 <= int(pick) <= len(matches):
            return matches[int(pick) - 1]


def run_session(args, kind: str, what: str, start):
    replaced = session.stop_current()
    if replaced:
        print(f"Stopped previous cast to {replaced['device']['name']}")
    # Record ourselves before discovery so `linuxcast stop` works at any point.
    session.record(None, kind, what, status="searching")
    try:
        device = choose_device(args)
        backend = BACKENDS[device.backend]
        session.record(device, kind, what, status="connecting")
        print(f"Connecting to {device.name}...")
        s = start(backend, device)
        try:
            session.record(device, kind, what, status="casting")
            print(f"Casting to {device.name}. Ctrl+C to stop.")
            notify(args, f"Casting to {device.name}", what)
            s.wait()
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            s.close()
        if s.last_error:
            raise Failure(f"Cast to {device.name} ended: {s.last_error}")
        note = getattr(s, "end_note", None)
        if note:
            print(f"{device.name}: {note}", file=sys.stderr)
        notify(args, f"Stopped casting to {device.name}", note or "")
    finally:
        session.clear()


def cmd_devices(args):
    devices = discover_all(args.backend and [args.backend], args.timeout)
    if args.json:
        rows = [asdict(d) | {"castable": BACKENDS[d.backend].can_cast} for d in devices]
        print(json.dumps(rows))
        return
    if not devices:
        print("no devices found")
    for d in devices:
        print(d.label())


def cmd_backends(args):
    for name, b in BACKENDS.items():
        ok, why = b.available()
        print(f"{name:11} {'yes' if ok else 'no ':4} {why}")


def cmd_monitors(args):
    mons = capture.monitors()
    if args.json:
        print(json.dumps([m.__dict__ for m in mons]))
        return
    for m in mons:
        print(f"{m.name:8} {m.width}x{m.height}+{m.x}+{m.y}{'  (primary)' if m.primary else ''}")


def cmd_status(args):
    state = session.current()
    if args.json:
        print(json.dumps(state))
    elif state:
        print(f"{state['status']}: {state['what']} -> {state['device']['name']} (pid {state['pid']})")
    else:
        print("not casting")


def cmd_mirror(args):
    opts = CaptureOptions(monitor=args.monitor, audio=not args.no_audio, fps=args.fps,
                          bitrate=args.bitrate, encoder=args.encoder)
    mon = capture.pick_monitor(args.monitor).name
    run_session(args, "mirror", f"Screen {mon}", lambda b, device: b.mirror(device, opts))


def cmd_play(args):
    name = args.source if "://" in args.source else Path(args.source).name
    run_session(args, "play", name, lambda b, device: b.play(device, args.source))


def cmd_stop(args):
    # With no --device, stop this machine's own cast without touching the network.
    if not args.device:
        state = session.stop_current()
        print(f"stopped {state['device']['name']}" if state else "not casting")
        return
    device = choose_device(args)
    BACKENDS[device.backend].stop(device)
    print(f"stopped {device.name}")


def _sigterm(*_):
    raise KeyboardInterrupt


def main(argv=None):
    p = argparse.ArgumentParser(prog="linuxcast", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help, device=True):
        sp = sub.add_parser(name, help=help)
        sp.set_defaults(fn=fn)
        sp.add_argument("-b", "--backend", choices=list(BACKENDS))
        sp.add_argument("-t", "--timeout", type=float, default=4.0, help="discovery seconds")
        if device:
            sp.add_argument("-d", "--device", help="name substring, id or IP")
            sp.add_argument("--gui", action="store_true", help="pick device in a dialog")
            sp.add_argument("--notify", action="store_true", help="desktop notifications")
            sp.add_argument("--target", help=argparse.SUPPRESS)  # JSON from `devices --json`
        return sp

    add("devices", cmd_devices, "list cast targets on the network", device=False) \
        .add_argument("--json", action="store_true")
    sp = sub.add_parser("backends", help="show which protocols work here")
    sp.set_defaults(fn=cmd_backends)
    sp = sub.add_parser("monitors", help="list capturable monitors")
    sp.set_defaults(fn=cmd_monitors)
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("status", help="show this machine's active cast")
    sp.set_defaults(fn=cmd_status)
    sp.add_argument("--json", action="store_true")

    m = add("mirror", cmd_mirror, "mirror a monitor (with desktop audio)")
    m.add_argument("-m", "--monitor", help="xrandr output name (default: primary)")
    m.add_argument("--no-audio", action="store_true")
    m.add_argument("--fps", type=int, default=30)
    m.add_argument("--bitrate", default="8M", help="max video bitrate (default 8M)")
    m.add_argument("--encoder", choices=["auto", "nvenc", "vaapi", "x264"], default="auto")

    pl = add("play", cmd_play, "cast a local media file or URL")
    pl.add_argument("source")

    add("stop", cmd_stop, "stop casting (this machine's cast, or --device's)")

    args = p.parse_args(argv)
    # Treat SIGTERM like Ctrl+C so the receiver is always released, even
    # when the applet or `linuxcast stop` ends us mid-connect.
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        pass
    except (Failure, BackendUnavailable, RuntimeError, FileNotFoundError) as e:
        notify(args, "Casting failed", str(e), urgent=True)
        sys.exit(f"error: {e}")
