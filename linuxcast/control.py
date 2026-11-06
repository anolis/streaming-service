"""Private, per-cast command socket; commands never reconnect to another cast."""
from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

from linuxcast import session


class ControlServer:
    def __init__(self, active, publish, has_next=False):
        self.active = active
        self.publish = publish
        self.has_next = has_next
        self.directory = Path(tempfile.mkdtemp(prefix="ctl-", dir=session.STATE.parent))
        self.path = str(self.directory / "socket")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket.bind(self.path)
            self.socket.listen(4)
            self.socket.settimeout(0.2)
        except BaseException:
            self.socket.close()
            shutil.rmtree(self.directory)
            raise
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self._last = None

    def snapshot(self):
        result = self.active.playback_status()
        result["next"] = self.has_next and result.get("active", False)
        return result

    def start(self):
        self._update()
        self.thread.start()
        return self

    def _update(self):
        status = self.snapshot()
        if status != self._last:
            self.publish(self.path, status)
            self._last = status

    def _serve(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.socket.accept()
            except socket.timeout:
                self._update()
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(2)
                try:
                    data = bytearray()
                    while b"\n" not in data and len(data) < 4096:
                        chunk = conn.recv(4096 - len(data))
                        if not chunk:
                            break
                        data.extend(chunk)
                    request = json.loads(data)
                    action = request.get("action")
                    if action == "next":
                        if not self.snapshot()["next"]:
                            raise RuntimeError("No next file in this cast")
                        self.active.skip()
                    else:
                        self.active.control(action, request.get("seconds"))
                    response = {"ok": True}
                except Exception as error:
                    response = {"ok": False, "error": str(error)}
                try:
                    conn.sendall(json.dumps(response).encode() + b"\n")
                except OSError:
                    pass  # caller closed; the command may already have completed
                self._update()

    def close(self):
        self.stopped.set()
        self.socket.close()
        if self.thread.is_alive():
            self.thread.join()  # finish any in-flight command before closing the receiver
        shutil.rmtree(self.directory, ignore_errors=True)


def send(action, seconds=None, expected=None):
    state = session.current()
    path = state.get("control") if state else None
    if not path:
        raise RuntimeError("This cast has no playback controls. Start a new Chromecast file cast with the updated LinuxCast.")
    if expected and expected != path:
        raise RuntimeError("The active cast changed. Reopen the menu and try again.")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(25)
            client.connect(path)
            client.sendall(json.dumps({"action": action, "seconds": seconds}).encode() + b"\n")
            data = bytearray()
            while b"\n" not in data and len(data) < 4096:
                chunk = client.recv(4096 - len(data))
                if not chunk:
                    break
                data.extend(chunk)
            reply = json.loads(data)
    except (OSError, ValueError) as error:
        raise RuntimeError("The cast stopped responding; check its status before retrying.") from error
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "Playback command failed"))
