"""Miracast backend (placeholder).

Miracast is Wi-Fi Direct (P2P) + RTSP + an MPEG-TS/RTP stream. Doing it from
scratch needs a Wi-Fi card with P2P support and NetworkManager's wifi-p2p device;
GNOME Network Displays already implements the whole stack, so for now we detect
it and hand off to it rather than reimplementing. No hardware here to test with.
"""

from __future__ import annotations

import shutil
import subprocess

from linuxcast.base import Backend, BackendUnavailable

GND_FLATPAK = "org.gnome.NetworkDisplays"


def _gnd_command() -> list[str] | None:
    if shutil.which("gnome-network-displays"):
        return ["gnome-network-displays"]
    if shutil.which("flatpak"):
        r = subprocess.run(["flatpak", "info", GND_FLATPAK], capture_output=True)
        if r.returncode == 0:
            return ["flatpak", "run", GND_FLATPAK]
    return None


def _has_p2p_device() -> bool:
    r = subprocess.run(["nmcli", "-t", "-f", "TYPE", "device"], capture_output=True, text=True)
    return "wifi-p2p" in r.stdout


class MiracastBackend(Backend):
    name = "miracast"

    def available(self):
        if not _has_p2p_device():
            return False, "no Wi-Fi P2P device in NetworkManager (Miracast needs Wi-Fi Direct)"
        if not _gnd_command():
            return False, f"install GNOME Network Displays: flatpak install flathub {GND_FLATPAK}"
        return True, ""

    def discover(self, timeout=5.0):
        # P2P peer discovery lives inside GND for now; nothing to list headlessly.
        return []

    def mirror(self, device, capture):
        cmd = _gnd_command()
        if not cmd:
            raise BackendUnavailable(self.available()[1])
        subprocess.Popen(cmd)
        raise BackendUnavailable("opened GNOME Network Displays; pick the sink there")

    def play(self, device, source):
        raise BackendUnavailable("Miracast only mirrors; use `mirror` instead")

    def stop(self, device):
        raise BackendUnavailable("stop the session from GNOME Network Displays")
