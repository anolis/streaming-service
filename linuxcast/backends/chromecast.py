"""Google Cast backend.

Chromecast can't receive arbitrary screen-mirroring streams (Chrome's tab/desktop
mirroring uses a private Cast Streaming app), so mirroring works by encoding the
screen to HLS and pointing the Default Media Receiver at it. Expect ~10 s of
latency (see LIVE_START_OFFSET_S): fine for video, not for games.
"""

from __future__ import annotations

import mimetypes
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from linuxcast import capture
from linuxcast.base import Backend, BackendUnavailable, CaptureOptions, Device, Session
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


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class CastSession(Session, MediaStatusListener):
    def __init__(self, cast, cleanup):
        self.cast = cast
        self._cleanup = cleanup
        self._done = threading.Event()
        self._seen_playing = False
        self.child = None  # ffmpeg process, if any
        self.last_error = None
        self._live_edge = None  # callable -> seconds at the live edge, for logging lag
        self._last_state = None

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
            _log(f"receiver {status.player_state}{why}" + (f" (lag {lag:.1f}s)" if lag is not None else ""))
        if status.player_state in ("PLAYING", "BUFFERING"):
            self._seen_playing = True
        elif status.player_state == "IDLE" and self._seen_playing:
            if status.idle_reason == "ERROR":
                self.last_error = "receiver reported a playback error"
            self._done.set()

    def load_media_failed(self, queue_item_id, error_code):
        self.last_error = f"receiver failed to load media (error {error_code})"
        self._done.set()

    def wait(self):
        while not self._done.wait(0.5):
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
