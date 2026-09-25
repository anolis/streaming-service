"""Protocol-agnostic device and backend interfaces.

Every cast protocol (Chromecast, Miracast, AirPlay, ...) is a Backend that can
discover Devices and start Sessions on them. The CLI only talks to this layer.
"""

from __future__ import annotations

import abc
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


def log(msg: str) -> None:
    """Session events go to stderr, which the desktop integrations keep as a log."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Device:
    backend: str          # backend name, e.g. "chromecast"
    id: str               # stable unique id within the backend
    name: str             # human-friendly name shown in pickers
    host: str | None = None
    model: str | None = None
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    def label(self) -> str:
        bits = [self.name, f"[{self.backend}]"]
        if self.model:
            bits.append(self.model)
        if self.host:
            bits.append(self.host)
        return "  ".join(bits)


class BackendUnavailable(RuntimeError):
    """Raised when a backend can't run on this machine (missing deps/hardware)."""


class Backend(abc.ABC):
    name: str = ""
    can_play: bool = True
    can_cast: bool = True  # False for discovery-only backends

    @abc.abstractmethod
    def available(self) -> tuple[bool, str]:
        """Return (usable, reason). reason explains what's missing when unusable."""

    @abc.abstractmethod
    def discover(self, timeout: float = 5.0) -> list[Device]:
        ...

    def unsupported_reason(self, device: Device) -> str | None:
        """Why this particular device can't be cast to, or None if it can."""
        return None if self.can_cast else "not supported yet"

    @abc.abstractmethod
    def mirror(self, device: Device, capture: "CaptureOptions") -> "Session":
        """Start mirroring the screen to device."""

    @abc.abstractmethod
    def play(self, device: Device, source: str) -> "Session":
        """Play a local file path or http(s) URL on device."""

    @abc.abstractmethod
    def stop(self, device: Device) -> None:
        """Stop whatever is casting on device."""


class Session(abc.ABC):
    """A running cast. wait() blocks until it ends; close() tears it down."""

    @abc.abstractmethod
    def wait(self) -> None:
        ...

    @abc.abstractmethod
    def close(self) -> None:
        ...


RESOLUTIONS = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080),
               "1440p": (2560, 1440), "2160p": (3840, 2160)}


@dataclass
class CaptureOptions:
    monitor: str | None = None      # xrandr output name, None = primary
    audio: bool = True              # capture desktop audio (default sink monitor)
    fps: int = 30
    bitrate: str = "8M"             # cap; actual rate follows screen complexity
    encoder: str = "auto"           # auto | nvenc | vaapi | x264
    workdir: Path | None = None
    resolution: str = "1080p"
    buffer_seconds: int = 8        # HLS playback offset, not encoder VBV size

    airplay_latency_ms: int = 0    # native protocol playout override; zero = automatic

    def __post_init__(self):
        if not 0 <= self.airplay_latency_ms <= 2000:
            raise ValueError("native AirPlay latency must be between 0 and 2000 ms")
        if self.resolution not in RESOLUTIONS:
            raise ValueError(f"unsupported output resolution: {self.resolution}")
        if not 1 <= self.fps <= 60:
            raise ValueError("target frame rate must be between 1 and 60")
        if not 2 <= self.buffer_seconds <= 20:
            raise ValueError("playback buffer must be between 2 and 20 seconds")
