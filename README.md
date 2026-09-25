# linuxcast

Windows-style "Cast to device" for Linux. Pluggable per-protocol backends:

| Backend    | Status | How |
|------------|--------|-----|
| Chromecast | working: mirror + files/URLs | ffmpeg → HLS served over LAN → Default Media Receiver |
| Miracast   | hand-off | needs a Wi-Fi Direct capable card; launches GNOME Network Displays |
| DLNA       | mirror + files/URLs | ffmpeg → continuous MPEG-TS for mirroring; files served as-is |
| AirPlay    | pairing + video backend; compatible receiver playback unverified | pyatv; URL playback and HLS mirroring on video-capable receivers |

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
no effect there. These settings do not change file playback or the external
GNOME Network Displays handoff.

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

AirPlay receivers without URL-video support are listed with a reason and cannot
be selected for video casting. Pairing is still allowed. The tested Samsung is
in this category; successful pairing does not enable AirPlay video on it.
DLNA is its working video route. File casts through DLNA and AirPlay expose only
the registered media file, not its containing directory.

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
