# linuxcast

Windows-style "Cast to device" for Linux. Pluggable per-protocol backends:

| Backend    | Status | How |
|------------|--------|-----|
| Chromecast | working: mirror + files/URLs | ffmpeg → HLS served over LAN → Default Media Receiver |
| Miracast   | hand-off | needs a Wi-Fi Direct capable card; launches GNOME Network Displays |
| DLNA       | mirror + files/URLs | ffmpeg → continuous MPEG-TS for mirroring; files served as-is |
| AirPlay native | experimental; Samsung desktop + audio confirmed | pinned Doubletake engine → native encrypted mirroring |
| AirPlay URL | pairing + video backend; compatible receiver playback unverified | pyatv; URL playback and HLS mirroring on video-capable receivers |

## Setup

```sh
./install.sh          # CLI (via pipx) + integration for the desktop you're logged into
./install.sh all      # Cinnamon and Xfce integrations
```

Run the installer as your normal desktop user. On Debian/Ubuntu-based systems it
installs missing dependencies through `sudo` (or PolicyKit), including pipx,
Python venv support, ffmpeg/ffprobe, xrandr, pactl, zenity and notification tools.
Tray installs also include the system Python GTK and Ayatana AppIndicator bindings.
Python casting dependencies are installed in pipx's isolated environment.

On Cinnamon, installation enables the Cast panel applet, reloads it immediately,
and checks that Cinnamon loaded it. It stays enabled at future logins. On Xfce,
the installer starts the tray immediately and registers login autostart. Re-running
`./install.sh all` updates both integrations without adding a second Cinnamon icon.
Keep the checkout in place: the integrations and editable CLI link to it.

Screen capture currently requires X11. For development: `python3 -m venv .venv && .venv/bin/pip install -e .`

## Desktop integration

| Windows | Cinnamon | Xfce |
|---|---|---|
| Win+K Cast flyout | Panel applet (`cinnamon/linuxcast@anolis`), Super+K | Tray icon + flyout (`xfce/linuxcast-tray`), Super+K |
| Explorer → "Cast to device" | Nemo right-click action | Nemo right-click action |

Both UIs launch `linuxcast` detached and read `$XDG_RUNTIME_DIR/linuxcast/session.json`,
which the CLI keeps current (`searching` → `connecting` → `casting`). So a cast started
anywhere (terminal, applet, Nemo) shows up everywhere, survives a panel restart, and
`linuxcast stop` ends it. Starting a new cast replaces the current one, like Windows.

- Cinnamon: open the Cast menu → Mirroring for resolution, frame rate and buffer
  presets; right-click → Configure also exposes these settings and the hotkey.
- Xfce: choose Mirroring settings from the tray menu or flyout.
- The tray autostarts in desktop sessions other than Cinnamon (which uses its
  own panel applet) and keeps its
  screen/audio choices in `~/.config/linuxcast/tray.json`. It runs on the system
  Python because it needs PyGObject and AyatanaAppIndicator3.
- Logs: `$XDG_RUNTIME_DIR/linuxcast/{applet,tray}.log`.

## Usage

```sh
linuxcast devices                  # discovered targets, including unsupported reasons
linuxcast mirror                   # primary monitor + desktop audio
linuxcast mirror -m DP-2 -d bedroom --no-audio
linuxcast mirror -b dlna -d living # DLNA screen mirroring
linuxcast mirror -b miracast       # open GNOME Network Displays
linuxcast pair -b airplay -d TV    # one-time AirPlay pairing
linuxcast play movie.mkv           # auto-transcodes if Chromecast can't decode it
linuxcast play https://example.com/video.mp4
linuxcast status                   # what this machine is casting
linuxcast stop                     # stop it (or: stop -d bedroom to stop any cast there)
linuxcast backends | monitors      # diagnostics
```

`LINUXCAST_DEBUG=1` prints the ffmpeg command and HTTP requests.

## Mirroring responsiveness

Resolution, target frame rate and playback buffer can be saved through the desktop
controls. They apply to the next cast; restart an active cast to use new values.
The defaults remain **1080p, 30 fps, 8 seconds**.

```sh
linuxcast mirror --resolution 720p --fps 30 --buffer 4
```

- Resolution: 480p, 720p, 1080p, 1440p or 2160p. Lower resolutions reduce encoding
  and network load. The encoder and receiver must support the selected output.
- Target frame rate: 15, 24, 30 or 60 fps in the desktop UI; 1–60 via `--fps`.
  Higher rates look smoother but require more processing and bandwidth.
- Playback buffer: 2–20 seconds via `--buffer` (alias `--buffer-seconds`). This
  changes Chromecast/AirPlay's requested HLS start offset and waits for enough
  media to support it. Smaller values reduce the requested delay but can increase
  stuttering. Actual latency also depends on capture, network and receiver behavior.

Resolution and frame rate apply to Chromecast, DLNA and compatible AirPlay screen
mirroring. DLNA buffering is controlled by the TV; the playback-buffer option has
no effect there. The native AirPlay backend uses resolution/frame rate too, but has a separate
millisecond playout setting described below. These settings do not change file
playback or the external GNOME Network Displays handoff.

## Notes

- Chromecast mirroring runs ~10 s behind live, on purpose. The Default Media
  Receiver doesn't prefetch live HLS, so if it plays near the live edge every Wi-Fi
  hiccup shows the buffering spinner; the served playlist carries
  `EXT-X-START:TIME-OFFSET=-8` to park it far enough back
  by default; `--buffer` changes that offset. Chrome's low-latency mirroring uses
  Google's proprietary Cast Streaming receiver, which third-party senders can't use.
- Segments are fMP4 with a 30 s playlist window, and video is quality-targeted VBR
  capped at `--bitrate`, so a static desktop costs ~1 Mbit/s rather than a constant 6.
- Cast logs record receiver state changes and lag, e.g.
  `receiver BUFFERING (lag 3.0s)`, which is the first place to look if it stutters.
- Encoder is picked automatically: NVENC → VAAPI → libx264.
- Mirroring output is letterboxed to the chosen resolution (1080p by default),
  including portrait/ultrawide inputs. File transcoding remains at 1080p.
- The receiver fetches media from this machine over HTTP on a random port, so a
  firewall must allow inbound LAN connections.

Miracast is an external handoff: use `linuxcast mirror -b miracast` or the
"Miracast (GNOME Network Displays)…" action in either desktop UI. Choose the
receiver and stop the session in GNOME Network Displays; these sessions are not
tracked by `linuxcast status` or controlled by `linuxcast stop`. Native Miracast
peer discovery and streaming are not implemented.

AirPlay URL playback and native screen mirroring are different protocols. The
Samsung rejects URL-video playback but **has displayed the desktop with audio**
using the experimental native backend. DLNA remains its established file path.
File casts through DLNA and AirPlay URL playback expose only the registered file.

## Experimental native AirPlay

```sh
./install.sh airplay
linuxcast mirror -b airplay-native -d living --resolution 720p --fps 30
linuxcast stop
```

This explicit install builds a pinned [Doubletake](https://github.com/omarroth/doubletake)
engine and installs the Go/GStreamer dependencies on Debian/Ubuntu. It does not
replace the existing `airplay` URL backend. The desktop menus list a separate
"(native AirPlay)" device; refresh discovery after installation.

Pairing occurs on the first cast: enter the TV's current PIN/password in the
terminal or desktop dialog. Native credentials are saved separately in
`~/.config/linuxcast/native-airplay.json`; prior pyatv pairing is not reused.
`linuxcast pair -b airplay-native -d living` can pair without starting capture.

Native mirroring uses automatic low-latency timing. The HLS `--buffer` setting
has no effect here; use `--airplay-latency-ms 100` to request a millisecond playout
override (0 means automatic). The receiver may add latency. Selected resolution
must fit its negotiated canvas. X11 uses the selected monitor; on Wayland the
native sender uses the screen-sharing portal (hardware validation pending).

This is experimental. A 20-second 720p/30 fps X11 desktop cast with desktop audio
was confirmed on a Samsung AU8000 from Debian 13. A follow-up two-minute cast
and saved-pairing reconnect completed with clean shutdown. Other receivers, Wayland,
long sessions, audio/video synchronization and measured latency still need validation. The default
build supports ALAC audio; AAC-ELD-only receivers require optional engine support
and are not covered by this installer. Use `--no-audio` to isolate video.
See [engine provenance, build and protocol tests](tools/airplay-native/README.md).

## Development checks

```sh
.venv/bin/python -m unittest discover -s tests -v
bash -n install.sh
git diff --check
```

The regression suite uses mocked receivers, temporary files, child processes,
and HTTP servers bound to localhost. It does not cast to real devices. A test
environment must permit local sockets. Hardware playback and desktop UI rendering
still require separate checks.

## License

linuxcast is available under the [MIT license](LICENSE). Third-party dependencies retain their own licenses.

Project website: https://anolis.github.io/streaming-service/
