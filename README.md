# linuxcast

Windows-style "Cast to device" for Linux. Pluggable per-protocol backends:

| Backend    | Status | How |
|------------|--------|-----|
| Chromecast | working: mirror + files/URLs | ffmpeg → HLS served over LAN → Default Media Receiver |
| Miracast   | hand-off | needs a Wi-Fi Direct capable card; launches GNOME Network Displays |
| AirPlay    | discovery only | lists Apple TVs via mDNS; casting needs pairing (pyatv is the likely path) |

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
```

Needs `ffmpeg`, `xrandr`, `pactl` (X11 session; Wayland capture isn't supported yet).

## Usage

```sh
linuxcast devices                  # everything castable on the LAN
linuxcast mirror                   # primary monitor + desktop audio
linuxcast mirror -m DP-2 -d bedroom --no-audio
linuxcast play movie.mkv           # auto-transcodes if Chromecast can't decode it
linuxcast play https://example.com/video.mp4
linuxcast stop -d bedroom
linuxcast backends | monitors      # diagnostics
```

`LINUXCAST_DEBUG=1` prints the ffmpeg command and HTTP requests.

## Notes

- Chromecast mirroring has ~3–5 s latency (HLS with 1 s segments). Chrome's
  low-latency mirroring uses Google's proprietary Cast Streaming receiver, which
  third-party senders can't use.
- Encoder is picked automatically: NVENC → VAAPI → libx264.
- Output is always letterboxed to 1920×1080, so portrait/ultrawide monitors are fine.
- The receiver fetches media from this machine over HTTP on a random port, so a
  firewall must allow inbound LAN connections.
