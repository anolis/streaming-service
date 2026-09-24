"""The one active cast on this machine, recorded in a runtime state file.

The CLI, the Cinnamon applet and the Nemo action all launch separate
processes; this file is how they agree on what is casting and how to stop it.
Like Windows, starting a new cast replaces the current one.
"""

from __future__ import annotations

import json
import fcntl
import tempfile
from contextlib import contextmanager
import os
import signal
import time
from pathlib import Path

from linuxcast.base import Device


def state_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/linuxcast-{os.getuid()}"
    d = Path(base) / "linuxcast"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


STATE = state_dir() / "session.json"


@contextmanager
def _lock(name):
    with (STATE.parent / name).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _identity(pid):
    try:
        # comm may contain spaces and parentheses; fields after its final ')'
        # begin at field 3. Start time (field 22) distinguishes reused PIDs.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def _alive(pid: int) -> bool:
    if type(pid) is not int or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def current() -> dict | None:
    try:
        state = json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(state, dict) or not _alive(state.get("pid")):
        return None
    identity = _identity(state["pid"])
    if identity is None or (state.get("identity") and state["identity"] != identity):
        return None
    return state


def record(device: Device | None, kind: str, what: str, status: str) -> None:
    """status is searching (device not chosen yet), connecting or casting."""
    if device:
        dev = {"backend": device.backend, "id": device.id, "name": device.name,
               "host": device.host}
    else:
        dev = {"backend": None, "id": None, "name": "cast devices", "host": None}
    state = {"pid": os.getpid(), "status": status, "kind": kind, "what": what,
             "started": time.time(), "device": dev, "identity": _identity(os.getpid())}
    with _lock("state.lock"):
        with tempfile.NamedTemporaryFile(mode="w", dir=STATE.parent,
                                         prefix="session-", delete=False) as f:
            tmp = Path(f.name)
            try:
                json.dump(state, f)
                f.flush()
                tmp.replace(STATE)
            finally:
                tmp.unlink(missing_ok=True)


def clear() -> None:
    with _lock("state.lock"):
        try:
            if json.loads(STATE.read_text()).get("pid") == os.getpid():
                STATE.unlink()
        except (FileNotFoundError, ValueError):
            pass


def replace_current(kind: str, what: str) -> dict | None:
    """Serialize stop-and-claim across CLI launches without blocking cleanup."""
    with _lock("control.lock"):
        previous = _stop_current(10.0)
        record(None, kind, what, status="searching")
        return previous


def stop_current(timeout: float = 10.0) -> dict | None:
    """Ask the active cast process to stop (it releases the receiver itself)."""
    with _lock("control.lock"):
        return _stop_current(timeout)


def _stop_current(timeout):
    state = current()
    if not state or state["pid"] == os.getpid():
        return None
    pid = state["pid"]
    identity = state.get("identity") or _identity(pid)

    def still_running():
        return identity is not None and _alive(pid) and _identity(pid) == identity

    try:
        if still_running():
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while still_running() and time.monotonic() < deadline:
            time.sleep(0.1)
        if still_running():
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # exited between checking and signalling
    with _lock("state.lock"):
        try:
            if json.loads(STATE.read_text()) == state:
                STATE.unlink()
        except (FileNotFoundError, ValueError):
            pass
    return state
