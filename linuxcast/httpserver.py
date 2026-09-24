"""Tiny threaded HTTP server that exposes local files/HLS output to cast devices.

Cast receivers fetch media themselves, so we serve it over the LAN. Chromecast's
Default Media Receiver needs CORS headers for HLS and Range support for seeking.
"""

from __future__ import annotations

import collections
import mimetypes
import os
import queue
import socket
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

mimetypes.add_type("application/x-mpegURL", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")
mimetypes.add_type("video/mp4", ".m4s")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/x-matroska", ".mkv")


def local_ip_for(remote_host: str) -> str:
    """The IP of the interface that routes to remote_host (what the device can reach)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((remote_host, 9))
        return s.getsockname()[0]


class Broadcaster:
    """Fans one live byte stream (e.g. ffmpeg's stdout) out to HTTP clients."""

    CLIENT_BACKLOG = 256  # chunks; a client this far behind is dropped
    RECENT_BYTES = 2 * 1024 * 1024  # replayed to new clients so they start instantly

    def __init__(self, source, chunk=64 * 1024, align=1):
        self._fd = source.fileno()
        self._chunk = chunk
        self._align = align  # e.g. 188 for MPEG-TS: late joiners must start on a packet
        self._clients: set[queue.Queue] = set()
        self._recent: collections.deque[bytes] = collections.deque()
        self._recent_size = 0
        self.total = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()

    def wait_for(self, nbytes: int, timeout: float) -> bool:
        """Block until the source has produced nbytes (e.g. capture has started)."""
        deadline = time.monotonic() + timeout
        while self.total < nbytes and time.monotonic() < deadline:
            time.sleep(0.1)
        return self.total >= nbytes

    def _pump(self):
        carry = b""
        while True:
            try:
                data = os.read(self._fd, self._chunk)  # returns whatever is ready
            except OSError:
                data = b""
            if data and self._align > 1:
                data = carry + data
                cut = len(data) - len(data) % self._align
                data, carry = data[:cut], data[cut:]
                if not data:
                    continue
            with self._lock:
                if data:
                    self.total += len(data)
                    self._recent.append(data)
                    self._recent_size += len(data)
                    while self._recent_size > self.RECENT_BYTES and len(self._recent) > 1:
                        self._recent_size -= len(self._recent.popleft())
                for q in list(self._clients):
                    try:
                        q.put_nowait(data)
                    except queue.Full:  # too slow: cut it off rather than corrupt it
                        self._clients.discard(q)
                        q.queue.clear()
                        q.put_nowait(b"")
            if not data:
                return

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(self.CLIENT_BACKLOG + 64)
        with self._lock:
            for data in list(self._recent)[-64:]:
                q.put_nowait(data)
            self._clients.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._clients.discard(q)


class _Handler(SimpleHTTPRequestHandler):
    # Set per-server via functools-free subclassing in MediaServer.
    files: dict[str, Path] = {}
    live: dict[str, tuple[Broadcaster, str, dict]] = {}  # path -> (source, mime, headers)
    playlist_header: str = ""  # extra tags inserted after #EXTM3U in served playlists

    def log_message(self, fmt, *args):  # keep the terminal quiet
        if os.environ.get("LINUXCAST_DEBUG"):
            super().log_message(fmt, *args)

    # What DLNA renderers expect for a seekable file (answer to getcontentFeatures).
    FILE_FEATURES = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"

    def end_headers(self):
        path = self.path.split("?", 1)[0]
        if not self._is_playlist() and path not in self.live:
            # Without Accept-Ranges, renderers assume they can't seek, and an MP4
            # whose index (moov) is at the end then plays as "format not supported".
            if not getattr(self, "_ranges_sent", False):
                self.send_header("Accept-Ranges", "bytes")
            if self.headers.get("getcontentFeatures.dlna.org"):
                self.send_header("contentFeatures.dlna.org", self.FILE_FEATURES)
            self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range")
        if self._is_playlist():
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def translate_path(self, path):
        # /f/<token>/<name> maps to an explicitly registered single file
        parts = path.split("?", 1)[0].split("/")
        if len(parts) >= 3 and parts[1] == "f" and parts[2] in self.files:
            return str(self.files[parts[2]])
        if self.directory == "":
            return os.devnull + "/unregistered"
        return super().translate_path(path)

    def _is_playlist(self):
        return self.path.split("?", 1)[0].endswith(".m3u8")

    def _send_live_headers(self):
        _, mime, extra = self.live[self.path.split("?", 1)[0]]
        self.send_response(200)
        self.send_header("Content-Type", mime)
        for k, v in extra.items():
            self.send_header(k, v)
        self.send_header("Connection", "close")  # no length: the stream ends when we do
        self.end_headers()

    def do_HEAD(self):
        if self.path.split("?", 1)[0] in self.live:
            return self._send_live_headers()
        return super().do_HEAD()

    def _serve_live(self):
        source = self.live[self.path.split("?", 1)[0]][0]
        self._send_live_headers()
        q = source.subscribe()
        try:
            while True:
                data = q.get()
                if not data:
                    return
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the receiver hung up; it may reconnect
        finally:
            source.unsubscribe(q)
            self.close_connection = True

    def do_GET(self):
        if self.path.split("?", 1)[0] in self.live:
            return self._serve_live()
        if self._is_playlist():
            # Live playlists change every second, but Last-Modified only has
            # 1-second resolution: honouring If-Modified-Since can answer "304
            # not modified" for a playlist that just gained a segment, and the
            # receiver starves at the live edge ("buffering" forever).
            del self.headers["If-Modified-Since"]
            del self.headers["If-None-Match"]
            if self.playlist_header:
                return self._send_playlist()
        rng = self.headers.get("Range")
        path = Path(self.translate_path(self.path))
        if not rng or not path.is_file():
            return super().do_GET()
        # Single byte-range support: "bytes=start-[end]"
        size = path.stat().st_size
        try:
            start_s, end_s = rng.strip().removeprefix("bytes=").split(",")[0].split("-")
            if start_s:
                start = int(start_s)
                end = int(end_s) if end_s else size - 1
            else:  # suffix range: last N bytes
                start, end = size - int(end_s), size - 1
            end = min(end, size - 1)
            if start > end or start < 0:
                raise ValueError
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Accept-Ranges", "bytes")
        self._ranges_sent = True
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = f.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # receivers routinely abort range requests while seeking

    def _send_playlist(self):
        try:
            text = Path(self.translate_path(self.path)).read_text()
        except FileNotFoundError:
            self.send_error(404)
            return
        body = text.replace("#EXTM3U\n", f"#EXTM3U\n{self.playlist_header}\n", 1).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-mpegURL")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Receivers routinely hang up mid-response (probing, seeking, switching
        # streams); only report errors that aren't that.
        import sys
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


class MediaServer:
    """Serves `root` (for HLS output) plus individually registered files."""

    def __init__(self, root: Path | None, bind_ip: str, port: int = 0):
        handler = type("Handler", (_Handler,), {"files": {}, "live": {}})
        self._handler = handler

        def make_handler(*args, **kwargs):
            # SimpleHTTPRequestHandler treats None as cwd; use an explicit
            # sentinel and translate registered-file-only requests ourselves.
            return handler(*args, directory=str(root) if root is not None else "", **kwargs)

        self._httpd = _QuietServer(
            (bind_ip, port), make_handler
        )
        self.ip = bind_ip
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> "MediaServer":
        self._thread.start()
        return self

    def url(self, rel: str) -> str:
        return f"http://{self.ip}:{self.port}/{rel.lstrip('/')}"

    def set_playlist_header(self, tags: str):
        self._handler.playlist_header = tags

    def add_live(self, name: str, source: Broadcaster, mime: str,
                 headers: dict | None = None) -> str:
        self._handler.live[f"/{name}"] = (source, mime, headers or {})
        return self.url(name)

    def add_file(self, path: Path) -> str:
        token = str(len(self._handler.files))
        self._handler.files[token] = path.resolve()
        return self.url(f"f/{token}/{quote(path.name)}")

    def close(self):
        # shutdown() waits for serve_forever to notice; after a receiver was
        # taken over by another sender this was seen to hang indefinitely (not
        # reproducible locally), so bound it. The loop thread is a daemon anyway.
        stopper = threading.Thread(target=self._httpd.shutdown, daemon=True)
        stopper.start()
        stopper.join(2)
        self._httpd.server_close()
