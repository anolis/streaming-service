"""DLNA / UPnP MediaRenderer backend (most smart TVs: Samsung, LG, Sony, ...).

The TV pulls media over HTTP, like Chromecast. Files are served as-is (renderers
decode far more than Chromecast does); screen mirroring is one continuous
MPEG-TS stream, which renderers handle better than HLS. Control is plain SOAP
to the AVTransport service; there is no push status, so the session polls.
"""

from __future__ import annotations

import mimetypes
from contextlib import ExitStack
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin
from xml.sax.saxutils import escape

from linuxcast import capture
from linuxcast.base import Backend, BackendUnavailable, CaptureOptions, Device, Session, log
from linuxcast.httpserver import Broadcaster, MediaServer, local_ip_for

SSDP_ADDR = ("239.255.255.250", 1900)
RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
NS = {"d": "urn:schemas-upnp-org:device-1-0"}

# Renderers match on MIME type; these are the names TVs actually list.
MIME_OVERRIDES = {".mkv": "video/x-mkv", ".ts": "video/mpeg", ".m2ts": "video/mpeg",
                  ".avi": "video/x-msvideo", ".flac": "audio/x-flac", ".m4a": "audio/mp4"}
LIVE_MIME = "video/mpeg"  # MPEG-TS, as Samsung and LG advertise it
# DLNA flags: streaming transfer, connection stalling, background transfer.
LIVE_FEATURES = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
FILE_FEATURES = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"


def _ssdp_search(timeout: float) -> dict[str, str]:
    """ip -> description URL for every MediaRenderer that answers."""
    msg = "\r\n".join(["M-SEARCH * HTTP/1.1", f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}",
                       'MAN: "ssdp:discover"', "MX: 2", f"ST: {RENDERER}", "", ""]).encode()
    found: dict[str, str] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.5)
        deadline = time.monotonic() + timeout
        for _ in range(2):  # UDP: ask twice
            s.sendto(msg, SSDP_ADDR)
        while time.monotonic() < deadline:
            try:
                data, (ip, _port) = s.recvfrom(8192)
            except socket.timeout:
                continue
            for line in data.decode(errors="replace").split("\r\n"):
                if line.lower().startswith("location:"):
                    found.setdefault(ip, line.split(":", 1)[1].strip())
    return found


def _describe(ip: str, location: str) -> Device | None:
    try:
        with urllib.request.urlopen(location, timeout=3) as r:
            root = ET.fromstring(r.read())
    except (OSError, ET.ParseError):
        return None
    dev = root.find("d:device", NS)
    if dev is None:
        return None
    base = root.findtext("d:URLBase", namespaces=NS) or location
    control = None
    for svc in dev.iterfind("d:serviceList/d:service", NS):
        if svc.findtext("d:serviceType", namespaces=NS) == AVTRANSPORT:
            control = urljoin(base, svc.findtext("d:controlURL", namespaces=NS))
    if not control:
        return None
    return Device("dlna", dev.findtext("d:UDN", namespaces=NS) or location,
                  dev.findtext("d:friendlyName", namespaces=NS) or ip, host=ip,
                  model=dev.findtext("d:modelName", namespaces=NS),
                  extra={"avtransport": control, "location": location})


def _soap(control_url: str, action: str, args: dict[str, str], timeout=5) -> ET.Element:
    body = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in args.items())
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{AVTRANSPORT}">{body}</u:{action}>'
        "</s:Body></s:Envelope>").encode()
    req = urllib.request.Request(control_url, data=envelope, method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{AVTRANSPORT}#{action}"'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return ET.fromstring(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        code = detail.split("<errorCode>")[-1].split("<")[0] if "<errorCode>" in detail else ""
        raise RuntimeError(f"TV rejected {action} (HTTP {e.code}"
                           + (f", UPnP error {code}" if code else "") + ")") from None


def _field(resp: ET.Element, name: str) -> str | None:
    el = next((e for e in resp.iter() if e.tag.rsplit("}", 1)[-1] == name), None)
    return el.text if el is not None else None


def _didl(url: str, title: str, mime: str, features: str, video: bool) -> str:
    cls = "object.item.videoItem" if video else "object.item.audioItem.musicTrack"
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{escape(title)}</dc:title><upnp:class>{cls}</upnp:class>"
        f'<res protocolInfo="http-get:*:{mime}:{features}">{escape(url)}</res>'
        "</item></DIDL-Lite>")


class DlnaBackend(Backend):
    name = "dlna"

    def available(self):
        if not capture.have("ffmpeg"):
            return False, "ffmpeg not found (needed for mirroring)"
        return True, ""

    def discover(self, timeout=5.0):
        found = _ssdp_search(min(timeout, 3.0))
        devices = []
        threads = [threading.Thread(target=lambda ip=ip, loc=loc: devices.append(_describe(ip, loc)))
                   for ip, loc in found.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(4)
        return [d for d in devices if d]

    def _load(self, device, url, title, mime, features, video=True):
        ctl = device.extra["avtransport"]
        try:  # some renderers refuse a new URI while something is playing
            _soap(ctl, "Stop", {"InstanceID": "0"})
        except RuntimeError:
            pass
        _soap(ctl, "SetAVTransportURI", {"InstanceID": "0", "CurrentURI": url,
                                         "CurrentURIMetaData": _didl(url, title, mime, features, video)})
        _soap(ctl, "Play", {"InstanceID": "0", "Speed": "1"})

    def mirror(self, device, opts: CaptureOptions):
        resources = ExitStack()
        cleanup = resources.close
        try:
            cmd = capture.screen_ts_command(opts)
            server = MediaServer(None, local_ip_for(device.host)).start()
            resources.callback(server.close)
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
            resources.callback(proc.stdout.close)
            resources.callback(capture.stop_process, proc)
            stream = Broadcaster(proc.stdout, align=188)
            url = server.add_live("screen.ts", stream, LIVE_MIME, {
                "transferMode.dlna.org": "Streaming",
                "contentFeatures.dlna.org": LIVE_FEATURES})

            # Audio capture takes a few seconds to start and holds video back until
            # it does; a TV told to play before data flows gives up.
            if not stream.wait_for(256 * 1024, timeout=20):
                raise RuntimeError("screen capture produced no data")
            self._load(device, url, "Linux screen", LIVE_MIME, LIVE_FEATURES)
        except BaseException:
            cleanup()
            raise
        return DlnaSession(device, url, live=True, cleanup=cleanup, child=proc)

    def play(self, device, source):
        if source.startswith(("http://", "https://")):
            mime = mimetypes.guess_type(source.split("?")[0])[0] or "video/mp4"
            self._load(device, source, source, mime, FILE_FEATURES, not mime.startswith("audio"))
            return DlnaSession(device, source, live=False, cleanup=lambda: None)
        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(source)
        mime = (MIME_OVERRIDES.get(path.suffix.lower())
                or mimetypes.guess_type(path.name)[0] or "video/mp4")
        server = MediaServer(None, local_ip_for(device.host)).start()
        url = server.add_file(path)
        try:
            self._load(device, url, path.name, mime, FILE_FEATURES, not mime.startswith("audio"))
        except BaseException:
            server.close()
            raise
        return DlnaSession(device, url, live=False, cleanup=server.close)

    def stop(self, device):
        _soap(device.extra["avtransport"], "Stop", {"InstanceID": "0"})


class DlnaSession(Session):
    POLL_S = 1.0
    START_TIMEOUT_S = 30

    def __init__(self, device, url, live, cleanup, child=None):
        self.device = device
        self.url = url
        self.live = live
        self._cleanup = cleanup
        self.child = child
        self.last_error = None
        self.end_note = None
        self._stopped = threading.Event()

    def _state(self) -> tuple[str | None, str | None]:
        ctl = self.device.extra["avtransport"]
        state = _field(_soap(ctl, "GetTransportInfo", {"InstanceID": "0"}), "CurrentTransportState")
        uri = _field(_soap(ctl, "GetMediaInfo", {"InstanceID": "0"}), "CurrentURI")
        return state, uri

    def wait(self):
        started = time.monotonic()
        played = False
        last = None
        failures = 0
        while not self._stopped.wait(self.POLL_S):
            if self.child is not None and self.child.poll() is not None:
                self.last_error = f"ffmpeg exited with code {self.child.returncode}"
                return
            try:
                state, uri = self._state()
                failures = 0
            except (OSError, RuntimeError) as e:
                failures += 1
                if failures >= 10:
                    self.last_error = f"lost contact with the TV ({e})"
                    return
                continue
            if state != last:
                log(f"receiver {state}")
                last = state
            if uri and uri != self.url:
                self.end_note = "another device started casting to it"
                return
            if state == "PLAYING":
                played = True
            elif played and state in ("STOPPED", "NO_MEDIA_PRESENT"):
                if self.live:
                    self.end_note = "stopped from the TV or another device"
                return
            elif not played and time.monotonic() - started > self.START_TIMEOUT_S:
                self.last_error = ("the TV never started playing (if it's showing an "
                                   "'allow this device?' prompt, accept it and try again)")
                return

    def close(self):
        self._stopped.set()
        try:
            state, uri = self._state()
            if uri == self.url and state not in ("STOPPED", "NO_MEDIA_PRESENT"):
                _soap(self.device.extra["avtransport"], "Stop", {"InstanceID": "0"})
        except (OSError, RuntimeError):
            pass
        finally:
            self._cleanup()
