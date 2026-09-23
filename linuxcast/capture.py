"""ffmpeg pipelines: screen+audio capture and file transcoding, both emitting HLS.

HLS is the lowest common denominator every receiver we care about can pull
over plain HTTP. Short segments keep mirror latency to a few seconds.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from linuxcast.base import CaptureOptions

PLAYLIST = "stream.m3u8"
OUT_W, OUT_H = 1920, 1080  # receivers get a fixed 16:9 1080p frame


@dataclass
class Monitor:
    name: str
    width: int
    height: int
    x: int
    y: int
    primary: bool


def monitors() -> list[Monitor]:
    out = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, check=True).stdout
    mons = []
    for m in re.finditer(r"^(\S+) connected (primary )?(\d+)x(\d+)\+(\d+)\+(\d+)", out, re.M):
        mons.append(Monitor(m[1], int(m[3]), int(m[4]), int(m[5]), int(m[6]), bool(m[2])))
    return mons


def pick_monitor(name: str | None) -> Monitor:
    mons = monitors()
    if not mons:
        raise RuntimeError("xrandr reports no connected monitors")
    if name:
        for m in mons:
            if m.name == name:
                return m
        raise RuntimeError(f"no monitor {name!r}; have: {', '.join(m.name for m in mons)}")
    return next((m for m in mons if m.primary), mons[0])


def default_audio_monitor() -> str:
    sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, check=True)
    return sink.stdout.strip() + ".monitor"


@cache
def _encoder_works(args: tuple[str, ...]) -> bool:
    """Try a 1-frame encode to see if a hardware encoder actually initialises."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", "color=black:s=256x256:d=0.1", *args, "-frames:v", "1", "-f", "null", "-"]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def video_encoder_args(choice: str, fps: int, bitrate: str) -> list[str]:
    gop = str(fps)  # keyframe every second so each 1s segment is independently decodable
    candidates = {
        "nvenc": ["-c:v", "h264_nvenc", "-preset", "p2", "-tune", "ll", "-zerolatency", "1",
                  "-rc", "cbr", "-profile:v", "high", "-bf", "0"],
        "vaapi": ["-vaapi_device", "/dev/dri/renderD128", "-c:v", "h264_vaapi",
                  "-profile:v", "high", "-bf", "0"],
        "x264": ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                 "-profile:v", "high", "-pix_fmt", "yuv420p"],
    }
    order = [choice] if choice != "auto" else ["nvenc", "vaapi", "x264"]
    for name in order:
        args = candidates[name]
        probe = args + (["-vf", "format=nv12,hwupload"] if name == "vaapi" else [])
        if name == "x264" or _encoder_works(tuple(probe)):
            rate = ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]
            return args + rate + ["-g", gop, "-keyint_min", gop, "-sc_threshold", "0"]
    raise RuntimeError(f"encoder {choice!r} is not usable on this machine")


def _scale_filter(hw_vaapi: bool) -> str:
    # Letterbox anything (portrait monitors, 16:10, 4K) into a 1080p frame.
    f = (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
         f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    return f + (",format=nv12,hwupload" if hw_vaapi else ",format=yuv420p")


def _hls_output(outdir: Path, live: bool) -> list[str]:
    # Live: rolling window. File: growing EVENT playlist that gets ENDLIST at EOF.
    flags = "independent_segments+omit_endlist+delete_segments" if live else "independent_segments"
    return [
        "-f", "hls", "-hls_time", "1",
        "-hls_list_size", "6" if live else "0",
        "-hls_playlist_type", "event" if not live else "",
        "-hls_flags", flags,
        "-hls_segment_filename", str(outdir / "seg%05d.ts"),
        str(outdir / PLAYLIST),
    ]


def _clean(cmd: list[str]) -> list[str]:
    # drop "-opt ''" pairs so option lists can be built conditionally
    out = []
    i = 0
    while i < len(cmd):
        if cmd[i].startswith("-") and i + 1 < len(cmd) and cmd[i + 1] == "":
            i += 2
            continue
        out.append(cmd[i])
        i += 1
    return out


def screen_command(opts: CaptureOptions, outdir: Path) -> list[str]:
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        raise RuntimeError("screen capture currently uses x11grab; log into an X11 session")
    mon = pick_monitor(opts.monitor)
    display = os.environ.get("DISPLAY", ":0")
    venc = video_encoder_args(opts.encoder, opts.fps, opts.bitrate)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-thread_queue_size", "512", "-f", "x11grab", "-draw_mouse", "1",
           "-framerate", str(opts.fps), "-video_size", f"{mon.width}x{mon.height}",
           "-i", f"{display}+{mon.x},{mon.y}"]
    if opts.audio:
        cmd += ["-thread_queue_size", "512", "-f", "pulse", "-i", default_audio_monitor()]
    cmd += ["-vf", _scale_filter("h264_vaapi" in venc), *venc]
    # aresample async smooths PulseAudio's jittery capture timestamps
    audio = ["-af", "aresample=async=1000", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += audio if opts.audio else ["-an"]
    return _clean(cmd + _hls_output(outdir, live=True))


def transcode_command(src: str, outdir: Path, encoder: str = "auto") -> list[str]:
    venc = video_encoder_args(encoder, 30, "8M")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re", "-i", src,
           "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", _scale_filter("h264_vaapi" in venc), *venc,
           "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000"]
    return _clean(cmd + _hls_output(outdir, live=False))


def probe(src: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name:stream=codec_type,codec_name",
         "-of", "json", src], capture_output=True, text=True)
    return json.loads(out.stdout or "{}")


# What Chromecast's Default Media Receiver decodes natively.
_NATIVE = {
    "mp4": ({"h264", "vp9", "av1"}, {"aac", "mp3", "opus", "flac"}),
    "webm": ({"vp8", "vp9", "av1"}, {"opus", "vorbis"}),
}


def needs_transcode(src: str) -> bool:
    info = probe(src)
    fmt = info.get("format", {}).get("format_name", "")
    streams = info.get("streams", [])
    v = {s["codec_name"] for s in streams if s.get("codec_type") == "video"}
    a = {s["codec_name"] for s in streams if s.get("codec_type") == "audio"}
    for key, (vok, aok) in _NATIVE.items():
        if key in fmt and v <= vok and a <= aok:
            return False
    if fmt in {"mp3", "flac", "ogg", "wav"} and not v:
        return False
    return True


class HlsProcess:
    """Runs an ffmpeg HLS pipeline and waits until the playlist is playable."""

    def __init__(self, cmd: list[str], outdir: Path):
        self.outdir = outdir
        self.cmd = cmd
        if os.environ.get("LINUXCAST_DEBUG"):
            print("+", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)

    def wait_ready(self, segments: int = 2, timeout: float = 20.0):
        playlist = self.outdir / PLAYLIST
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                if self.proc.returncode == 0 and playlist.exists():
                    return  # short input finished before `segments` were written
                raise RuntimeError(f"ffmpeg exited with code {self.proc.returncode}")
            if playlist.exists() and playlist.read_text().count("#EXTINF") >= segments:
                return
            time.sleep(0.2)
        raise RuntimeError("timed out waiting for ffmpeg to produce HLS segments")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def have(binary: str) -> bool:
    return shutil.which(binary) is not None
