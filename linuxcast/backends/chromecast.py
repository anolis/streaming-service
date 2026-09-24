"""Google Cast backend.

Chromecast can't receive arbitrary screen-mirroring streams (Chrome's tab/desktop
mirroring uses a private Cast Streaming app), so mirroring works by encoding the
screen to HLS and pointing the Default Media Receiver at it. Expect ~10 s of
latency (see LIVE_START_OFFSET_S): fine for video, not for games.
"""

from __future__ import annotations

import mimetypes
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from linuxcast import capture
from linuxcast.base import Backend, BackendUnavailable, CaptureOptions, Device, Session, log
from linuxcast.httpserver import MediaServer, local_ip_for

try:
    import pychromecast
    from pychromecast.controllers.media import MediaStatusListener
except ImportError:  # pragma: no cover - reported via available()
    pychromecast = None
    MediaStatusListener = object


class ChromecastBackend(Backend):
    name = "chromecast"

    def available(self):
        if pychromecast is None:
            return False, "pip install pychromecast"
        if not capture.have("ffmpeg"):
            return False, "ffmpeg not found (needed for mirroring/transcoding)"
        return True, ""

    def discover(self, timeout=5.0):
        if pychromecast is None:
            raise BackendUnavailable("pychromecast is not installed")
        casts, browser = pychromecast.get_chromecasts(timeout=timeout)
        browser.stop_discovery()
        return [
            Device(self.name, str(c.cast_info.uuid), c.cast_info.friendly_name,
                   host=c.cast_info.host, model=c.cast_info.model_name,
                   extra={"port": c.cast_info.port, "cast_type": c.cast_info.cast_type})
            for c in casts
        ]

    def _connect(self, device: Device):
        # Connect by IP rather than via the mDNS browser: pychromecast resolves
        # mDNS services lazily and breaks once discovery has been stopped.
        cast = pychromecast.get_chromecast_from_host(
            (device.host, device.extra.get("port", 8009), uuid.UUID(device.id),
             device.model, device.name), tries=3, timeout=10)
        cast.wait(timeout=10)
        if not cast.socket_client.is_connected:
            cast.disconnect(timeout=2)
            raise RuntimeError(f"{device.name} ({device.host}) did not respond")
        return cast

    def _start(self, device, url, content_type, title, live, cleanup) -> "CastSession":
        cast = self._connect(device)
        mc = cast.media_controller
        session = CastSession(cast, cleanup)
        mc.register_status_listener(session)
        media_info = None
        if content_type == "application/x-mpegURL":
            fmt = "fmp4" if capture.SEGMENT_TYPE == "fmp4" else "ts"
            media_info = {"hlsSegmentFormat": fmt,
                          "hlsVideoSegmentFormat": "fmp4" if fmt == "fmp4" else "mpeg2_ts"}
        try:
            mc.play_media(url, content_type, title=title,
                          stream_type="LIVE" if live else "BUFFERED", media_info=media_info)
            mc.block_until_active(timeout=15)
            session.bind(mc.status)
        except BaseException:
            # Stopped mid-connect: don't leave the receiver on a dead stream.
            cast.quit_app()
            cast.disconnect(timeout=3)
            raise
        return session

    def mirror(self, device, opts: CaptureOptions):
        workdir = Path(opts.workdir or tempfile.mkdtemp(prefix="linuxcast-"))
        server = MediaServer(workdir, local_ip_for(device.host)).start()
        server.set_playlist_header(
            f"#EXT-X-START:TIME-OFFSET=-{capture.LIVE_START_OFFSET_S},PRECISE=YES")
        hls = capture.HlsProcess(capture.screen_command(opts, workdir), workdir)

        def cleanup():
            hls.stop()
            server.close()
            shutil.rmtree(workdir, ignore_errors=True)

        try:
            # The start offset only works once that much stream exists.
            hls.wait_ready(segments=capture.LIVE_START_OFFSET_S + 2,
                           timeout=capture.LIVE_START_OFFSET_S + 20)
            s = self._start(device, server.url(capture.PLAYLIST), "application/x-mpegURL",
                            "Linux screen", live=True, cleanup=cleanup)
        except BaseException:
            cleanup()
            raise
        s.child = hls.proc
        s.watch_live(lambda: capture.live_edge(workdir))
        return s

    def play(self, device, source):
        if source.startswith(("http://", "https://")):
            ctype = mimetypes.guess_type(source.split("?")[0])[0] or "video/mp4"
            return self._start(device, source, ctype, source, live=False, cleanup=lambda: None)

        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(source)
        workdir = Path(tempfile.mkdtemp(prefix="linuxcast-"))
        server = MediaServer(workdir, local_ip_for(device.host)).start()
        hls = None

        def cleanup():
            if hls:
                hls.stop()
            server.close()
            shutil.rmtree(workdir, ignore_errors=True)

        try:
            if capture.needs_transcode(str(path)):
                print(f"{path.name}: format not native to Chromecast, transcoding on the fly")
                hls = capture.HlsProcess(capture.transcode_command(str(path), workdir), workdir)
                hls.wait_ready()
                url, ctype = server.url(capture.PLAYLIST), "application/x-mpegURL"
            else:
                url = server.add_file(path)
                ctype = mimetypes.guess_type(path.name)[0] or "video/mp4"
            s = self._start(device, url, ctype, path.name, live=False, cleanup=cleanup)
        except BaseException:
            cleanup()
            raise
        s.child = hls.proc if hls else None
        return s

    def stop(self, device):
        cast = self._connect(device)
        cast.quit_app()
        time.sleep(0.5)
        cast.disconnect(timeout=3)


class CastSession(Session, MediaStatusListener):
    IDLE_GRACE_S = 15

    def __init__(self, cast, cleanup):
        self.cast = cast
        self._cleanup = cleanup
        self._done = threading.Event()
        self.child = None  # ffmpeg process, if any
        self.last_error = None
        self._live_edge = None  # callable -> seconds at the live edge, for logging lag
        self._last_state = None
        self._media_session = None  # ours, once the load is active
        self.end_note = None  # why the receiver ended a cast we didn't stop
        self._idle_since = None  # reasonless IDLE: transitional unless it lasts

    def bind(self, status):
        """Only our own media session's status counts from here on. Until then the
        receiver may still report on whatever it played before (e.g. BUFFERING then
        IDLE as the old media is replaced), which must not end our cast."""
        self._media_session = status.media_session_id

    def watch_live(self, live_edge):
        self._live_edge = live_edge

    def _lag(self, status) -> float | None:
        edge = self._live_edge() if self._live_edge else None
        pos = status.adjusted_current_time if status else None
        return None if edge is None or pos is None else edge - pos

    # MediaStatusListener
    def new_media_status(self, status):
        if status.player_state != self._last_state:
            self._last_state = status.player_state
            lag = self._lag(status)
            why = f" [{status.idle_reason}]" if status.idle_reason else ""
            log(f"receiver {status.player_state}{why}" + (f" (lag {lag:.1f}s)" if lag is not None else ""))
        if self._media_session is None:
            return
        if status.media_session_id not in (None, self._media_session):
            self.end_note = "another device started casting to it"
            self._done.set()
            return
        if status.player_state != "IDLE":
            self._idle_since = None
            return
        if not status.idle_reason:
            # The receiver passes through IDLE with no reason while loading (and
            # while being taken over); only a reasoned IDLE means the media ended.
            if self._idle_since is None:
                self._idle_since = time.monotonic()
            return
        if status.idle_reason == "ERROR":
            self.last_error = "receiver reported a playback error"
        elif self._live_edge:
            # A live mirror never finishes by itself.
            self.end_note = "stopped from the TV or another device"
        self._done.set()

    def load_media_failed(self, queue_item_id, error_code):
        self.last_error = f"receiver failed to load media (error {error_code})"
        self._done.set()

    def wait(self):
        while not self._done.wait(0.5):
            if self._idle_since and time.monotonic() - self._idle_since > self.IDLE_GRACE_S:
                self.end_note = "the receiver went idle"
                break
            if self.child is not None and self.child.poll() is not None and self.child.returncode:
                self.last_error = f"ffmpeg exited with code {self.child.returncode}"
                break

    def close(self):
        try:
            if not self._done.is_set():
                self.cast.quit_app()
        finally:
            self.cast.disconnect(timeout=3)
            self._cleanup()
