# Native AirPlay engine

linuxcast runs a separate, pinned build of [Doubletake](https://github.com/omarroth/doubletake)
for native AirPlay mirroring. It handles the actual pairing, encrypted RTSP/control,
FairPlay negotiation where required, timing and encrypted video/audio transport.
The Python adapter manages discovery, desktop credential prompts and lifetime.
This is experimental receiver compatibility, not an HLS fallback.

Upstream revision: `ae067228d76df011375164814b729932ed55ca2f`.
Upstream license: LGPL-3.0-or-later, including incorporated GPLv3 terms.
The modifications in `linuxcast.patch` are provided under LGPL-3.0-or-later.
The build script preserves upstream source, the integration patch and license
texts so the engine can be inspected, modified and rebuilt independently.

The patch adds:

- version identification and newline-delimited `LINUXCAST_EVENT` JSON for
  credential requests, pairing completion and mirror readiness;
- pairing without starting screen capture;
- output dimensions and an X11 monitor rectangle;
- encoder initialization probes with automatic fallback when a hardware encoder cannot start;
- visible GStreamer errors when capture fails;
- acquisition of the PIN challenge before prompting, matching the ordering used
  by pyatv, so the entered PIN is used with the retained challenge.

No cryptographic keys or PINs are included in lifecycle events. The adapter does
not enable upstream debug logging, which can include key material. Native
credentials use a separate owner-only file from pyatv's URL-video credentials.

## Build

On Debian 13, `./install.sh airplay` installs the system build/runtime dependencies
and builds the engine into `~/.local/bin/linuxcast-airplay-native`. This is an
explicit experimental install; regular desktop installation does not build it.
With dependencies already present, run `tools/build-airplay-native.sh` directly.
The Go module requires 1.25; Go's automatic toolchain support can obtain that
version when building with Debian 13's Go package. Network access is required for
source, Go modules and possibly the Go toolchain. Build without cgo; ALAC audio is
available, but optional AAC-ELD-only receivers require a different build and are
not supported by this installer.

## Local validation

The Python suite includes process/prompt/teardown and command-translation tests.
To additionally run actual synthetic video over native transport:

```sh
LINUXCAST_AIRPLAY_NATIVE=/path/to/patched/doubletake \
LINUXCAST_AIRPLAY_TEST_RECEIVER=/path/to/doubletake-test-receiver \
.venv/bin/python -m unittest discover -s tests -p test_airplay_native.py -v
```

Build both test binaries from the patched source with:

```sh
CGO_ENABLED=0 go build -o bin/ ./cmd/doubletake ./cmd/doubletake-test-receiver
```

This binds only localhost receiver fixtures and sends synthetic video. Modern,
Roku and LG profiles use PIN authentication and reconnect with saved credentials;
AppleTV3 and UxPlay use their raw/transient fixture modes. Configuring the legacy
fixtures with HAP PIN auth was rejected during testing (missing root FairPlay
fields). Those combinations are not claimed supported. A fixture's byte counters
prove transport flow, not TV decoding, visible picture or audio quality. The modern
fixture checks synthetic ALAC audio transport as well as video.

On 2026-09-25, the user confirmed both the desktop picture and a quiet test tone
on a Samsung AU8000 during a 20-second X11 cast from Debian 13, at 720p/30 fps
using NVENC and desktop audio through PipeWire’s PulseAudio compatibility service.
Pairing used a fresh PIN; the adapter saved credentials for future connections.
The sender reached its bounded shutdown timeout after the test; the adapter
reaped its process group. Long-session stability, precise synchronization,
latency and Wayland capture remain unverified.
