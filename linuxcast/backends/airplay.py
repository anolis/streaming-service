"""AirPlay backend (Apple TV, and TVs with AirPlay 2 such as Samsung/LG), via pyatv.

The receiver pulls media from a URL, like Chromecast, so files are served over
HTTP and the screen is mirrored as HLS (native to AirPlay video). Real AirPlay
screen mirroring needs Apple's FairPlay on the sending side, which third-party
senders can't do, so latency is the same as Chromecast's (~10 s).

Most receivers require a one-time pairing (the TV shows a PIN): run
`linuxcast pair -d NAME`. Credentials are kept in ~/.config/linuxcast/airplay.json.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
import mimetypes
from contextlib import ExitStack
import os
import shutil
import tempfile
import threading
from pathlib import Path

from linuxcast import capture
from linuxcast.base import Backend, BackendUnavailable, CaptureOptions, Device, Session, log
from linuxcast.httpserver import MediaServer, local_ip_for

try:
    import pyatv
    from pyatv.const import Protocol
except ImportError:  # pragma: no cover - reported via available()
    pyatv = None

# AirPlay "features" bit 0: the receiver plays video from a URL (/play). Apple TVs
# set it; e.g. Samsung TVs don't (their AirPlay is mirroring + audio, and
# mirroring needs Apple's FairPlay, which we can't do).
FEATURE_VIDEO = 1 << 0

CREDENTIALS = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "linuxcast" / "airplay.json"


class _Loop:
    """One asyncio loop on a daemon thread; pyatv objects must stay on it."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, timeout=None):
        future = self.submit(coro)
        try:
            return future.result(timeout)
        except BaseException:
            future.cancel()
            raise

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


_loop = None


def _aio() -> _Loop:
    global _loop
    if _loop is None:
        _loop = _Loop()
    return _loop


def _load_credentials() -> dict[str, str]:
    try:
        return json.loads(CREDENTIALS.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_credentials(identifier: str, credentials: str):
    creds = _load_credentials()
    creds[identifier] = credentials
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS.write_text(json.dumps(creds, indent=2))
    CREDENTIALS.chmod(0o600)


class AirPlayBackend(Backend):
    name = "airplay"

    def available(self):
        if pyatv is None:
            return False, "pip install pyatv"
        return True, ""

    async def _scan(self, timeout, identifier=None, host=None):
        return await pyatv.scan(asyncio.get_running_loop(), timeout=int(max(timeout, 1)),
                                identifier=identifier, protocol=Protocol.AirPlay,
                                hosts=[host] if host else None)

    def discover(self, timeout=5.0):
        if pyatv is None:
            raise BackendUnavailable("pyatv is not installed")
        creds = _load_credentials()
        devices = []
        for conf in _aio().run(self._scan(timeout)):
            svc = conf.get_service(Protocol.AirPlay)
            ident = conf.identifier
            paired = ident in creds or svc.pairing.name == "NotNeeded"
            low = (svc.properties.get("features") or "0").split(",")[0]
            video = bool(int(low, 16) & FEATURE_VIDEO)
            devices.append(Device(self.name, ident, conf.name, host=str(conf.address),
                                  model=conf.device_info.raw_model or conf.device_info.model_str,
                                  extra={"paired": paired, "video": video}))
        return devices

    async def _config(self, device):
        # Unicast scans are unreliable on some TVs, so fall back to a full scan.
        confs = await self._scan(3, identifier=device.id)
        if not confs:
            raise RuntimeError(f"{device.name} did not respond")
        conf = confs[0]
        creds = _load_credentials().get(device.id)
        if creds:
            conf.set_credentials(Protocol.AirPlay, creds)
        return conf

    # -- pairing ---------------------------------------------------------------

    def pair(self, device, ask_pin):
        """ask_pin() is called once the TV shows its PIN and returns what the user typed."""
        async def run():
            conf = await self._config(device)
            handler = await pyatv.pair(conf, Protocol.AirPlay, asyncio.get_running_loop())
            try:
                await handler.begin()
                if handler.device_provides_pin:
                    pin = await asyncio.get_running_loop().run_in_executor(None, ask_pin)
                    handler.pin(int(pin))
                await handler.finish()
                if not handler.has_paired:
                    raise RuntimeError("pairing failed (wrong PIN?)")
                _save_credentials(device.id, handler.service.credentials)
            finally:
                await handler.close()
        _aio().run(run(), timeout=300)

    # -- casting ---------------------------------------------------------------

    def unsupported_reason(self, device):
        if not device.extra.get("video", True):
            return "its AirPlay doesn't accept video from non-Apple devices"
        return None

    def _require_paired(self, device):
        reason = self.unsupported_reason(device)
        if reason:
            raise BackendUnavailable(f"{device.name}: {reason}")
        if not device.extra.get("paired") and device.id not in _load_credentials():
            raise BackendUnavailable(
                f"{device.name} needs a one-time pairing first: linuxcast pair -d \"{device.name}\"")

    def _start(self, device, url, cleanup, live):
        self._require_paired(device)

        async def connect():
            return await pyatv.connect(await self._config(device), asyncio.get_running_loop(),
                                       protocol=Protocol.AirPlay)

        atv = _aio().run(connect(), timeout=30)
        # play_url returns when playback ends, so it is the session's lifetime.
        future = _aio().submit(atv.stream.play_url(url))
        return AirPlaySession(atv, future, cleanup, live)

    def mirror(self, device, opts: CaptureOptions):
        self._require_paired(device)
        resources = ExitStack()
        cleanup = resources.close
        try:
            workdir = Path(opts.workdir or tempfile.mkdtemp(prefix="linuxcast-"))
            if opts.workdir is None:
                resources.callback(shutil.rmtree, workdir, ignore_errors=True)
            server = MediaServer(workdir, local_ip_for(device.host)).start()
            resources.callback(server.close)
            server.set_playlist_header(
                f"#EXT-X-START:TIME-OFFSET=-{capture.LIVE_START_OFFSET_S},PRECISE=YES")
            hls = capture.HlsProcess(capture.screen_command(opts, workdir), workdir)
            resources.callback(hls.stop)
            hls.wait_ready(segments=capture.LIVE_START_OFFSET_S + 2,
                           timeout=capture.LIVE_START_OFFSET_S + 20)
            s = self._start(device, server.url(capture.PLAYLIST), cleanup, live=True)
        except BaseException:
            cleanup()
            raise
        s.child = hls.proc
        return s

    def play(self, device, source):
        if source.startswith(("http://", "https://")):
            return self._start(device, source, lambda: None, live=False)
        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(source)
        server = MediaServer(None, local_ip_for(device.host)).start()
        try:
            return self._start(device, server.add_file(path), server.close, live=False)
        except BaseException:
            server.close()
            raise

    def stop(self, device):
        async def run():
            atv = await pyatv.connect(await self._config(device), asyncio.get_running_loop(),
                                      protocol=Protocol.AirPlay)
            try:
                await atv.remote_control.stop()
            finally:
                tasks = atv.close()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
        _aio().run(run(), timeout=30)


def _close_atv(atv):
    """atv.close() schedules work on the pyatv loop, so it must run there."""
    async def run():
        tasks = atv.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    try:
        _aio().run(run(), timeout=5)
    except Exception:  # noqa: BLE001 - best effort
        pass


class AirPlaySession(Session):
    def __init__(self, atv, future, cleanup, live):
        self.atv = atv
        self.future = future
        self._cleanup = cleanup
        self.live = live
        self.child = None
        self.last_error = None
        self.end_note = None

    def wait(self):
        while True:
            try:
                self.future.result(timeout=0.5)
                log("AirPlay playback ended")
                if self.live:  # a live stream never finishes by itself
                    self.end_note = "stopped from the TV or another device"
                return
            except FutureTimeoutError:
                if self.future.done():
                    self.last_error = "AirPlay playback timed out"
                    return
            except Exception as e:  # noqa: BLE001 - pyatv raises many types
                self.last_error = f"AirPlay playback failed: {type(e).__name__}: {e}"
                return
            if self.child is not None and self.child.poll() is not None:
                self.last_error = f"ffmpeg exited with code {self.child.returncode}"
                return

    def close(self):
        try:
            if not self.future.done():
                self.future.cancel()
                try:
                    _aio().run(self.atv.remote_control.stop(), timeout=5)
                except Exception:  # noqa: BLE001 - best effort
                    pass
        finally:
            _close_atv(self.atv)
            self._cleanup()
