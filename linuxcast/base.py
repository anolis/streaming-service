"""Protocol-agnostic device and backend interfaces.

Every cast protocol (Chromecast, Miracast, AirPlay, ...) is a Backend that can
discover Devices and start Sessions on them. The CLI only talks to this layer.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path


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

    @abc.abstractmethod
    def available(self) -> tuple[bool, str]:
        """Return (usable, reason). reason explains what's missing when unusable."""

    @abc.abstractmethod
    def discover(self, timeout: float = 5.0) -> list[Device]:
        ...

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


@dataclass
class CaptureOptions:
    monitor: str | None = None      # xrandr output name, None = primary
    audio: bool = True              # capture desktop audio (default sink monitor)
    fps: int = 30
    bitrate: str = "6M"
    encoder: str = "auto"           # auto | nvenc | vaapi | x264
    workdir: Path | None = None
