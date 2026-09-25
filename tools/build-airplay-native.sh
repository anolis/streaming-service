#!/usr/bin/env bash
# Build a reproducible, locally patched Doubletake engine. No receiver access.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REV=ae067228d76df011375164814b729932ed55ca2f
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/linuxcast/airplay-native"
DEST="${LINUXCAST_NATIVE_PREFIX:-$HOME/.local}"
mkdir -p "$CACHE" "$DEST/bin"
SOURCE="$CACHE/$REV"
if [[ ! -d "$SOURCE/.git" ]]; then
    git clone https://github.com/omarroth/doubletake.git "$SOURCE"
fi
if [[ "$(git -C "$SOURCE" rev-parse HEAD)" != "$REV" ]]; then
    git -C "$SOURCE" checkout --detach "$REV"
fi
# Always patch an isolated worktree so the retained upstream checkout stays clean.
BUILD="$(mktemp -d "$CACHE/build.XXXXXX")"
cleanup() { git -C "$SOURCE" worktree remove --force "$BUILD" >/dev/null 2>&1 || true; }
trap cleanup EXIT
git -C "$SOURCE" worktree add --detach "$BUILD" "$REV"
git -C "$BUILD" apply "$ROOT/tools/airplay-native/linuxcast.patch"

# A stale GOROOT (e.g. left in a shell profile by an old manual install) breaks
# every Go toolchain; each toolchain knows its own root.
unset GOROOT
# Distribution Go is often older than the engine's go.mod requires (Ubuntu 22.04
# ships 1.18, Debian 13 1.24). Old toolchains can't auto-download a newer one,
# so fetch the official release ourselves, checksum-verified, into the cache.
NEED="$(awk '/^go /{print $2; exit}' "$BUILD/go.mod")"
version_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]; }
GO=go
if ! command -v go >/dev/null || ! version_ge "$(go env GOVERSION 2>/dev/null | sed 's/^go//')" "$NEED"; then
    case "$(uname -m)" in
        x86_64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;;
        *) echo "No Go $NEED toolchain available for $(uname -m); install Go >= $NEED" >&2; exit 1 ;;
    esac
    read -r GOFILE GOSUM < <(curl -fsSL 'https://go.dev/dl/?mode=json' | python3 -c '
import json, sys
arch = sys.argv[1]
for rel in json.load(sys.stdin):  # newest first; stable releases only
    for f in rel["files"]:
        if f["os"] == "linux" and f["arch"] == arch and f["kind"] == "archive":
            print(f["filename"], f["sha256"]); sys.exit()
' "$ARCH")
    TOOLCHAIN="$CACHE/${GOFILE%.tar.gz}"
    if [[ ! -x "$TOOLCHAIN/go/bin/go" ]]; then
        echo "* Fetching $GOFILE (system Go is older than $NEED)"
        curl -fsSL -o "$CACHE/$GOFILE" "https://go.dev/dl/$GOFILE"
        echo "$GOSUM  $CACHE/$GOFILE" | sha256sum -c --quiet -
        mkdir -p "$TOOLCHAIN"
        tar -xzf "$CACHE/$GOFILE" -C "$TOOLCHAIN"
        rm -f "$CACHE/$GOFILE"
    fi
    GO="$TOOLCHAIN/go/bin/go"
fi
(
    cd "$BUILD"
    CGO_ENABLED=0 GOTOOLCHAIN=local "$GO" build -trimpath -o "$DEST/bin/linuxcast-airplay-native.new" ./cmd/doubletake
)
"$DEST/bin/linuxcast-airplay-native.new" -linuxcast-version
mv "$DEST/bin/linuxcast-airplay-native.new" "$DEST/bin/linuxcast-airplay-native"
# Retain corresponding source and license information alongside the installation.
mkdir -p "$DEST/share/linuxcast/airplay-native"
git -C "$BUILD" diff > "$DEST/share/linuxcast/airplay-native/linuxcast.patch"
cp "$BUILD/LICENSE" "$BUILD/COPYING.GPL" "$DEST/share/linuxcast/airplay-native/"
printf '%s\n' "https://github.com/omarroth/doubletake" "$REV" "$SOURCE" > "$DEST/share/linuxcast/airplay-native/SOURCE"
echo "Native AirPlay engine installed: $DEST/bin/linuxcast-airplay-native"
