"""Google Cast backend.

Chromecast can't receive arbitrary screen-mirroring streams (Chrome's tab/desktop
mirroring uses a private Cast Streaming app), so mirroring works by encoding the
screen to HLS and pointing the Default Media Receiver at it. Expect ~10 s of
latency at the default 8-second playback offset: fine for video, not for games.
"""

from __future__ import annotations

import math
import mimetypes
from contextlib import ExitStack
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

    def _start(self, device, url, content_type, title, live, cleanup, duration=None, autoplay=True) -> "CastSession":
        cast = self._connect(device)
        mc = cast.media_controller
        session = CastSession(cast, cleanup)
        session._source_duration = duration
        mc.register_status_listener(session)
        media_info = None
        if content_type == "application/x-mpegURL":
            fmt = "fmp4" if capture.SEGMENT_TYPE == "fmp4" else "ts"
            media_info = {"hlsSegmentFormat": fmt,
                          "hlsVideoSegmentFormat": "fmp4" if fmt == "fmp4" else "mpeg2_ts"}
        if duration is not None:
            media_info = (media_info or {}) | {"duration": duration}
        try:
            mc.play_media(url, content_type, title=title,
                          stream_type="LIVE" if live else "BUFFERED", media_info=media_info,
                          current_time=None if live else 0, autoplay=autoplay)
            # An already-playing receiver makes block_until_active return for
            # the OLD media. Wait for our unique URL before binding ownership.
            deadline = time.monotonic() + 20
            while True:
                status = mc.status
                if (status.content_id == url and status.media_session_id is not None
                        and status.player_state in ("PLAYING", "PAUSED", "BUFFERING")):
                    session.bind(status)
                    break
                if session.last_error:
                    raise RuntimeError(session.last_error)
                if time.monotonic() >= deadline:
                    raise RuntimeError("Receiver did not load the requested media")
                time.sleep(0.1)
        except BaseException:
            # Stopped mid-connect: don't leave the receiver on a dead stream.
            cast.quit_app()
            cast.disconnect(timeout=3)
            raise
        return session

    def mirror(self, device, opts: CaptureOptions):
        resources = ExitStack()
        cleanup = resources.close
        try:
            workdir = Path(opts.workdir or tempfile.mkdtemp(prefix="linuxcast-"))
            if opts.workdir is None:
                resources.callback(shutil.rmtree, workdir, ignore_errors=True)
            server = MediaServer(workdir, local_ip_for(device.host)).start()
            resources.callback(server.close)
            server.set_playlist_header(
                f"#EXT-X-START:TIME-OFFSET=-{opts.buffer_seconds},PRECISE=YES")
            hls = capture.HlsProcess(capture.screen_command(opts, workdir), workdir)
            resources.callback(hls.stop)
            hls.wait_ready(segments=opts.buffer_seconds + 2,
                           timeout=opts.buffer_seconds + 20)
            s = self._start(device, server.url(capture.PLAYLIST), "application/x-mpegURL",
                            "Linux screen", live=True, cleanup=cleanup)
        except BaseException:
            cleanup()
            raise
        s.child = hls.proc
        s.watch_live(lambda: capture.live_edge(workdir))
        return s

    def play(self, device, source):
        active = self._play(device, source)
        if active._transcoded:
            return TranscodedSession(active, lambda offset, paused: self._play(device, source, offset, paused))
        return active

    def _play(self, device, source, offset=0, paused=False):
        if source.startswith(("http://", "https://")):
            ctype = mimetypes.guess_type(source.split("?")[0])[0] or "video/mp4"
            return self._start(device, source, ctype, source, live=False, cleanup=lambda: None)

        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(source)
        resources = ExitStack()
        cleanup = resources.close
        hls = None
        try:
            workdir = Path(tempfile.mkdtemp(prefix="linuxcast-"))
            resources.callback(shutil.rmtree, workdir, ignore_errors=True)
            server = MediaServer(workdir, local_ip_for(device.host)).start()
            resources.callback(server.close)
            info = capture.probe(str(path))
            duration = capture.media_duration(info)
            if capture.needs_transcode(str(path), info=info):
                print(f"{path.name}: format not native to Chromecast, transcoding on the fly")
                server.set_playlist_header("#EXT-X-START:TIME-OFFSET=0,PRECISE=YES")
                hls = capture.HlsProcess(capture.transcode_command(str(path), workdir, start_seconds=offset), workdir)
                resources.callback(hls.stop)
                hls.wait_ready()
                url, ctype = server.url(capture.PLAYLIST), "application/x-mpegURL"
            else:
                url = server.add_file(path)
                ctype = mimetypes.guess_type(path.name)[0] or "video/mp4"
            s = self._start(device, url, ctype, path.name, live=False, cleanup=cleanup,
                            duration=max(0, duration - offset) if duration else None, autoplay=not paused)
        except BaseException:
            cleanup()
            raise
        s.child = hls.proc if hls else None
        s._source_duration = duration
        s._source_offset = offset
        s._transcoded = hls is not None
        if hls:
            s._seek_limit = lambda: capture.live_edge(workdir)
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
        self._skip = threading.Event()
        self._source_duration = None
        self._source_offset = 0
        self._transcoded = False
        self._seek_limit = None
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

    def playback_status(self):
        status = self.cast.media_controller.status
        active = (not self._done.is_set() and not self._skip.is_set()
                  and self._media_session is not None
                  and status.media_session_id == self._media_session
                  and status.player_state in ("PLAYING", "PAUSED", "BUFFERING"))
        position = status.adjusted_current_time
        duration = self._source_duration or status.duration
        position = max(0, int(position + self._source_offset)) if isinstance(position, (int, float)) and math.isfinite(position) else int(self._source_offset)
        duration = duration if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0 else None
        seek_end = self._seek_limit() if self._seek_limit else duration
        if self._seek_limit and seek_end is not None:
            seek_end += self._source_offset
        if seek_end is not None:
            seek_end = min(seek_end, duration) if duration else seek_end
            seek_end = max(0, int(seek_end))
        return {"active": active, "state": status.player_state,
                "position": position, "duration": duration, "seek_end": seek_end,
                "play": active and status.player_state == "PAUSED",
                "pause": active and status.player_state != "PAUSED" and bool(status.supports_pause),
                "seek": active and bool(status.supports_seek) and status.stream_type != "LIVE"}

    def skip(self):
        if not self.playback_status()["active"]:
            raise RuntimeError("The media session is no longer active")
        self._skip.set()

    def control(self, action, seconds=None):
        available = self.playback_status()
        if action not in ("play", "pause", "seek", "seek-to"):
            raise RuntimeError("Unknown playback command")
        if not available["seek" if action == "seek-to" else action]:
            raise RuntimeError(f"{action.capitalize()} is unavailable for the current media")
        mc = self.cast.media_controller
        if action == "play":
            mc.play()
        elif action == "pause":
            mc.pause()
        else:
            if type(seconds) not in (int, float) or not math.isfinite(seconds):
                raise RuntimeError("Seek offset must be a finite number of seconds")
            status = mc.status
            position = status.adjusted_current_time
            if action == "seek" and (position is None or not math.isfinite(position)):
                raise RuntimeError("Receiver has not reported a playback position yet")
            target = max(0, seconds if action == "seek-to" else position + seconds)
            duration = available["duration"]
            if duration:
                target = min(target, max(0, duration - 0.1))
            prepared = self._seek_limit() if self._seek_limit else None
            if self._seek_limit and (prepared is None or target >= prepared):
                ready = max(0, int(prepared or 0))
                raise RuntimeError(f"That point is still encoding. Ready through {ready // 60}:{ready % 60:02}; try again as encoding progresses.")
            paused = status.player_state == "PAUSED"
            mc.seek(target)
            # pychromecast's seek resumes playback; retain an intentional pause.
            if paused:
                mc.pause()

    def wait(self):
        while not self._done.wait(0.5):
            if self._skip.is_set():
                break
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
            try:
                self.cast.disconnect(timeout=3)
            finally:
                self._cleanup()


class TranscodedSession(Session):
    """Seek a transcoded file at its source, bypassing unreliable EVENT-HLS SEEK.

    Each replacement has independent URLs/timestamps and its own cleanup. The
    wait loop follows replacements instead of treating their LOAD as a takeover.
    """
    def __init__(self, active, restart):
        self._active = active
        self._restart = restart
        self._lock = threading.RLock()
        self._closed = False

    @property
    def last_error(self):
        return self._active.last_error

    @property
    def end_note(self):
        return self._active.end_note

    @property
    def cast(self):
        return self._active.cast

    @property
    def _skip(self):
        return self._active._skip

    def playback_status(self):
        with self._lock:
            result = self._active.playback_status()
            # Source-based seeking can prepare any position, even beyond the
            # segments already encoded or the receiver's advertised seek range.
            result["seek"] = result["active"] and bool(result["duration"])
            result["seek_end"] = result["duration"]
            return result

    def control(self, action, seconds=None):
        with self._lock:
            if self._closed:
                raise RuntimeError("The cast has ended")
            if action not in ("seek", "seek-to"):
                return self._active.control(action, seconds)
            state = self.playback_status()
            if not state["seek"]:
                raise RuntimeError("Seeking is unavailable for this media")
            if type(seconds) not in (int, float) or not math.isfinite(seconds):
                raise RuntimeError("Seek position must be a finite number of seconds")
            target = seconds if action == "seek-to" else state["position"] + seconds
            target = min(max(0, target), max(0, state["duration"] - 0.1))
            log(f"source seek requested: {target:.1f}s (from {state['position']}s)")
            previous = self._active
            replacement = self._restart(target, state["state"] == "PAUSED")
            self._active = replacement
            # LOAD ends the old receiver media session. Wake its wait loop even
            # if its socket hasn't received that status yet, without quitting
            # the app now playing the replacement stream.
            previous._done.set()
            try:
                previous.close()
            except Exception as error:
                log(f"previous stream cleanup: {error}")
            log(f"source seek loaded: {target:.1f}s")

    def skip(self):
        with self._lock:
            self._active.skip()

    def wait(self):
        while True:
            with self._lock:
                if self._closed:
                    return
                active = self._active
            active.wait()
            with self._lock:
                if active is self._active:
                    return

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._active.close()
