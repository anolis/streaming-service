"""linuxcast: a Windows-style "Cast to device" for Linux."""

from __future__ import annotations

import argparse
import signal
import sys
from concurrent.futures import ThreadPoolExecutor

from linuxcast import capture
from linuxcast.backends import BACKENDS
from linuxcast.base import BackendUnavailable, CaptureOptions, Device


def discover_all(backends: list[str] | None, timeout: float) -> list[Device]:
    usable = [b for n, b in BACKENDS.items() if (not backends or n in backends) and b.available()[0]]
    with ThreadPoolExecutor(len(usable) or 1) as pool:
        results = pool.map(lambda b: b.discover(timeout), usable)
    return [d for devs in results for d in devs]


def choose_device(args) -> Device:
    devices = discover_all(args.backend and [args.backend], args.timeout)
    if args.device:
        q = args.device.lower()
        matches = [d for d in devices if q in d.name.lower() or q == d.id or q == d.host]
    else:
        matches = devices
    if not matches:
        sys.exit(f"no matching device found ({len(devices)} discovered)")
    if len(matches) == 1:
        return matches[0]
    if not sys.stdin.isatty():
        sys.exit("several devices match; pass --device NAME")
    for i, d in enumerate(matches, 1):
        print(f"  {i}) {d.label()}")
    while True:
        pick = input("Cast to which device? ")
        if pick.isdigit() and 1 <= int(pick) <= len(matches):
            return matches[int(pick) - 1]


def run_session(device: Device, start):
    backend = BACKENDS[device.backend]
    print(f"Connecting to {device.name}...")
    session = start(backend)
    print(f"Casting to {device.name}. Ctrl+C to stop.")
    # Turn SIGTERM into KeyboardInterrupt so the receiver is always released.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        session.wait()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        session.close()
    if getattr(session, "last_error", None):
        sys.exit(session.last_error)


def cmd_devices(args):
    devices = discover_all(args.backend and [args.backend], args.timeout)
    if not devices:
        print("no devices found")
    for d in devices:
        print(d.label())


def cmd_backends(args):
    for name, b in BACKENDS.items():
        ok, why = b.available()
        print(f"{name:11} {'yes' if ok else 'no ':4} {why}")


def cmd_monitors(args):
    for m in capture.monitors():
        print(f"{m.name:8} {m.width}x{m.height}+{m.x}+{m.y}{'  (primary)' if m.primary else ''}")


def cmd_mirror(args):
    opts = CaptureOptions(monitor=args.monitor, audio=not args.no_audio, fps=args.fps,
                          bitrate=args.bitrate, encoder=args.encoder)
    device = choose_device(args)
    run_session(device, lambda b: b.mirror(device, opts))


def cmd_play(args):
    device = choose_device(args)
    run_session(device, lambda b: b.play(device, args.source))


def cmd_stop(args):
    device = choose_device(args)
    BACKENDS[device.backend].stop(device)
    print(f"stopped {device.name}")


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
        return sp

    add("devices", cmd_devices, "list cast targets on the network", device=False)
    sub.add_parser("backends", help="show which protocols work here").set_defaults(fn=cmd_backends)
    sub.add_parser("monitors", help="list capturable monitors").set_defaults(fn=cmd_monitors)

    m = add("mirror", cmd_mirror, "mirror a monitor (with desktop audio)")
    m.add_argument("-m", "--monitor", help="xrandr output name (default: primary)")
    m.add_argument("--no-audio", action="store_true")
    m.add_argument("--fps", type=int, default=30)
    m.add_argument("--bitrate", default="6M")
    m.add_argument("--encoder", choices=["auto", "nvenc", "vaapi", "x264"], default="auto")

    pl = add("play", cmd_play, "cast a local media file or URL")
    pl.add_argument("source")

    add("stop", cmd_stop, "stop casting on a device")

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (BackendUnavailable, RuntimeError, FileNotFoundError) as e:
        sys.exit(f"error: {e}")
