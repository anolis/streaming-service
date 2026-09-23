"""Tiny threaded HTTP server that exposes local files/HLS output to cast devices.

Cast receivers fetch media themselves, so we serve it over the LAN. Chromecast's
Default Media Receiver needs CORS headers for HLS and Range support for seeking.
"""

from __future__ import annotations

import mimetypes
import os
import socket
import threading
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


class _Handler(SimpleHTTPRequestHandler):
    # Set per-server via functools-free subclassing in MediaServer.
    files: dict[str, Path] = {}
    playlist_header: str = ""  # extra tags inserted after #EXTM3U in served playlists

    def log_message(self, fmt, *args):  # keep the terminal quiet
        if os.environ.get("LINUXCAST_DEBUG"):
            super().log_message(fmt, *args)

    def end_headers(self):
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
        return super().translate_path(path)

    def _is_playlist(self):
        return self.path.split("?", 1)[0].endswith(".m3u8")

    def do_GET(self):
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


class MediaServer:
    """Serves `root` (for HLS output) plus individually registered files."""

    def __init__(self, root: Path, bind_ip: str, port: int = 0):
        handler = type("Handler", (_Handler,), {"files": {}})
        self._handler = handler
        self._httpd = ThreadingHTTPServer(
            (bind_ip, port), lambda *a, **kw: handler(*a, directory=str(root), **kw)
        )
        self._httpd.daemon_threads = True
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
