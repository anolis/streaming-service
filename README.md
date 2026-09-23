# linuxcast

Windows-style "Cast to device" for Linux. Pluggable per-protocol backends:

| Backend    | Status | How |
|------------|--------|-----|
| Chromecast | working: mirror + files/URLs | ffmpeg → HLS served over LAN → Default Media Receiver |
| Miracast   | hand-off | needs a Wi-Fi Direct capable card; launches GNOME Network Displays |
| AirPlay    | discovery only | lists Apple TVs via mDNS; casting needs pairing (pyatv is the likely path) |

## Setup

```sh
./install.sh          # CLI (via pipx) + integration for the desktop you're logged into
./install.sh all      # Cinnamon and Xfce integrations
```

Needs `pipx`, `ffmpeg`, `xrandr`, `pactl`, `zenity` (X11 session; Wayland capture
isn't supported yet). For development: `python3 -m venv .venv && .venv/bin/pip install -e .`

## Desktop integration

| Windows | Cinnamon | Xfce |
|---|---|---|
| Win+K Cast flyout | Panel applet (`cinnamon/linuxcast@anolis`), Super+K | Tray icon + flyout (`xfce/linuxcast-tray`), Super+K |
| Explorer → "Cast to device" | Nemo right-click action | Nemo right-click action |

Both UIs launch `linuxcast` detached and read `$XDG_RUNTIME_DIR/linuxcast/session.json`,
which the CLI keeps current (`searching` → `connecting` → `casting`). So a cast started
anywhere (terminal, applet, Nemo) shows up everywhere, survives a panel restart, and
`linuxcast stop` ends it. Starting a new cast replaces the current one, like Windows.

- Applet settings (right-click → Configure): hotkey, audio, monitor, `linuxcast` path.
- The Xfce tray autostarts only in Xfce sessions (`OnlyShowIn=XFCE`) and keeps its
  screen/audio choices in `~/.config/linuxcast/tray.json`. It runs on the system
  Python because it needs PyGObject and AyatanaAppIndicator3.
- Logs: `$XDG_RUNTIME_DIR/linuxcast/{applet,tray}.log`.

## Usage

```sh
linuxcast devices                  # everything castable on the LAN
linuxcast mirror                   # primary monitor + desktop audio
linuxcast mirror -m DP-2 -d bedroom --no-audio
linuxcast play movie.mkv           # auto-transcodes if Chromecast can't decode it
linuxcast play https://example.com/video.mp4
linuxcast status                   # what this machine is casting
linuxcast stop                     # stop it (or: stop -d bedroom to stop any cast there)
linuxcast backends | monitors      # diagnostics
```

`LINUXCAST_DEBUG=1` prints the ffmpeg command and HTTP requests.

## Notes

- Chromecast mirroring runs ~10 s behind live, on purpose. The Default Media
  Receiver doesn't prefetch live HLS, so if it plays near the live edge every Wi-Fi
  hiccup shows the buffering spinner; the served playlist carries
  `EXT-X-START:TIME-OFFSET=-8` to park it far enough back
  (`LIVE_START_OFFSET_S` in `capture.py`). Chrome's low-latency mirroring uses
  Google's proprietary Cast Streaming receiver, which third-party senders can't use.
- Segments are fMP4 with a 30 s playlist window, and video is quality-targeted VBR
  capped at `--bitrate`, so a static desktop costs ~1 Mbit/s rather than a constant 6.
- Cast logs record receiver state changes and lag, e.g.
  `receiver BUFFERING (lag 3.0s)`, which is the first place to look if it stutters.
- Encoder is picked automatically: NVENC → VAAPI → libx264.
- Output is always letterboxed to 1920×1080, so portrait/ultrawide monitors are fine.
- The receiver fetches media from this machine over HTTP on a random port, so a
  firewall must allow inbound LAN connections.
