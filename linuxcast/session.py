"""The one active cast on this machine, recorded in a runtime state file.

The CLI, the Cinnamon applet and the Nemo action all launch separate
processes; this file is how they agree on what is casting and how to stop it.
Like Windows, starting a new cast replaces the current one.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

from linuxcast.base import Device


def state_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/linuxcast-{os.getuid()}"
    d = Path(base) / "linuxcast"
    d.mkdir(parents=True, exist_ok=True)
    return d


STATE = state_dir() / "session.json"


def _alive(pid: int) -> bool:
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
    if not _alive(state.get("pid", -1)):
        STATE.unlink(missing_ok=True)  # stale: the process died without cleaning up
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
             "started": time.time(), "device": dev}
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)  # atomic, so readers never see a half-written file


def clear() -> None:
    try:
        if json.loads(STATE.read_text()).get("pid") == os.getpid():
            STATE.unlink()
    except (FileNotFoundError, ValueError):
        pass


def stop_current(timeout: float = 10.0) -> dict | None:
    """Ask the active cast process to stop (it releases the receiver itself)."""
    state = current()
    if not state or state["pid"] == os.getpid():
        return None
    os.kill(state["pid"], signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while _alive(state["pid"]) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _alive(state["pid"]):
        os.kill(state["pid"], signal.SIGKILL)
        STATE.unlink(missing_ok=True)
    return state
