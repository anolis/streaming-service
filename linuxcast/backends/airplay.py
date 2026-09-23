"""AirPlay backend (discovery only).

Apple TVs advertise _airplay._tcp over mDNS, so we can list them. Sending
video/mirroring requires HomeKit-style pairing (SRP + Ed25519) and FairPlay for
screen mirroring on modern tvOS; that's not implemented yet. Media URL playback
(the "/play" endpoint after pairing) is the realistic next step, e.g. via pyatv.
"""

from __future__ import annotations

from linuxcast.base import Backend, BackendUnavailable, Device

try:
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
except ImportError:  # pragma: no cover
    Zeroconf = None
    ServiceListener = object

SERVICE = "_airplay._tcp.local."


class _Collector(ServiceListener):
    def __init__(self):
        self.found: dict[str, Device] = {}

    def add_service(self, zc, type_, name):
        info = zc.get_service_info(type_, name, timeout=2000)
        if not info:
            return
        props = {k.decode(): (v or b"").decode(errors="replace") for k, v in info.properties.items()}
        addrs = info.parsed_addresses()
        self.found[name] = Device(
            "airplay", props.get("deviceid", name), name.removesuffix("." + SERVICE),
            host=addrs[0] if addrs else None, model=props.get("model"), extra=props)

    def update_service(self, zc, type_, name):
        self.add_service(zc, type_, name)

    def remove_service(self, zc, type_, name):
        self.found.pop(name, None)


class AirPlayBackend(Backend):
    name = "airplay"

    def available(self):
        if Zeroconf is None:
            return False, "pip install zeroconf"
        return True, "discovery only; casting not implemented"

    def discover(self, timeout=5.0):
        import time

        zc = Zeroconf()
        try:
            col = _Collector()
            ServiceBrowser(zc, SERVICE, col)
            time.sleep(timeout)
            return list(col.found.values())
        finally:
            zc.close()

    def _unsupported(self, *_):
        raise BackendUnavailable("AirPlay casting isn't implemented yet (discovery only)")

    mirror = play = stop = _unsupported
