#!/usr/bin/env bash
# Install dependencies, CLI, and persistent desktop integration for this user.
# ./install.sh [all|cinnamon|xfce|tray|cli] (default: current desktop)
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin/linuxcast"
UUID="linuxcast@anolis"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
RUNTIME="${XDG_RUNTIME_DIR:-/tmp/linuxcast-$(id -u)}/linuxcast"
DESKTOP="${XDG_CURRENT_DESKTOP:-}"
DESKTOP="${DESKTOP,,}"
target="${1:-auto}"
case "$target" in
    auto)
        case "$DESKTOP" in
            *cinnamon*) target=cinnamon ;;
            *) target=tray ;;
        esac ;;
    all|cinnamon|xfce|tray|cli) ;;
    *) echo "unknown target: $target" >&2; exit 2 ;;
esac

# Desktop settings and pipx must belong to the logged-in user, not root.
if [[ ${EUID} -eq 0 ]]; then
    echo "Run ./install.sh as your normal desktop user; it uses sudo for missing system packages." >&2
    exit 1
fi

install_dependencies() {
    local packages=() binary package
    for pair in 'pipx:pipx' 'ffmpeg:ffmpeg' 'ffprobe:ffmpeg' 'xrandr:x11-xserver-utils' \
                'pactl:pulseaudio-utils' 'zenity:zenity' 'notify-send:libnotify-bin' \
                'python3:python3' 'gdbus:libglib2.0-bin' 'gsettings:libglib2.0-bin'; do
        binary="${pair%%:*}"; package="${pair#*:}"
        if ! command -v "$binary" >/dev/null; then packages+=("$package"); fi
    done
    if ! /usr/bin/python3 -c 'import venv, ensurepip' 2>/dev/null; then
        packages+=(python3-venv)
    fi
    if [[ "$target" != cli && "$target" != cinnamon ]]; then
        if ! /usr/bin/python3 -c 'import gi; gi.require_version("Gtk", "3.0"); gi.require_version("AyatanaAppIndicator3", "0.1"); from gi.repository import Gtk, AyatanaAppIndicator3' 2>/dev/null; then
            packages+=(python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1)
        fi
    fi
    if (( ${#packages[@]} )); then
        if ! command -v apt-get >/dev/null; then
            echo "Automatic dependency installation currently supports Debian/Ubuntu-based distributions." >&2
            echo "Install these dependencies with your package manager and rerun: ${packages[*]}" >&2
            exit 1
        fi
        echo "* Installing missing system dependencies: ${packages[*]}"
        if command -v sudo >/dev/null; then
            sudo apt-get update
            sudo apt-get install -y "${packages[@]}"
        elif command -v pkexec >/dev/null; then
            pkexec apt-get update
            pkexec apt-get install -y "${packages[@]}"
        else
            echo "Install sudo or enable PolicyKit (pkexec) so the installer can install missing packages." >&2
            exit 1
        fi
    fi
}

install_cli() {
    echo "* CLI -> $BIN"
    mkdir -p "$HOME/.local/bin"
    PIPX_BIN_DIR="$HOME/.local/bin" pipx install --force --editable "$REPO"
}

install_nemo_action() {
    local dir="$DATA_HOME/nemo/actions"
    echo "* Nemo right-click action -> $dir"
    mkdir -p "$dir"
    python3 - "$REPO/cinnamon/nemo/linuxcast.nemo_action" "$dir/linuxcast.nemo_action" "$BIN" <<'PY'
from pathlib import Path
import sys
source, dest, binary = sys.argv[1:]
Path(dest).write_text(Path(source).read_text().replace('@LINUXCAST@', binary))
PY
}

install_cinnamon() {
    local dir="$DATA_HOME/cinnamon/applets"
    echo "* Cinnamon applet -> $dir/$UUID"
    mkdir -p "$dir"
    ln -sfn "$REPO/cinnamon/$UUID" "$dir/$UUID"
    # 'all' also works on Xfce-only installations, where this schema is absent.
    if ! gsettings list-schemas | grep -x org.cinnamon >/dev/null; then
        echo "  Cinnamon is not installed; applet files are ready if it is installed later."
        if [[ "$target" == cinnamon ]]; then return 1; fi
        return
    fi
    python3 - "$UUID" <<'PY'
import ast
import subprocess
import sys
uuid = sys.argv[1]
def get(key):
    raw = subprocess.check_output(['gsettings', 'get', 'org.cinnamon', key], text=True).strip()
    return ast.literal_eval(raw.removeprefix('@as '))
enabled = get('enabled-applets')
if not any(uuid in entry.split(':') for entry in enabled):
    ids = [int(entry.rsplit(':', 1)[-1]) for entry in enabled if entry.rsplit(':', 1)[-1].isdigit()]
    panels = get('panels-enabled')
    panel = panels[0].split(':', 1)[0] if panels else '1'
    instance = max(get('next-applet-id'), max(ids, default=0) + 1)
    enabled.append(f'panel{panel}:right:1:{uuid}:{instance}')
    subprocess.run(['gsettings', 'set', 'org.cinnamon', 'next-applet-id', str(instance + 1)], check=True)
    subprocess.run(['gsettings', 'set', 'org.cinnamon', 'enabled-applets', repr(enabled)], check=True)
print('  enabled persistently in Cinnamon (Super+K opens it)')
PY
    if [[ "$DESKTOP" == *cinnamon* && -n "${DISPLAY:-}" ]]; then
        # Reload already-enabled applets too, so reinstalling applies updates now.
        if ! gdbus call --session --dest org.Cinnamon --object-path /org/Cinnamon \
            --method org.Cinnamon.ReloadXlet "$UUID" APPLET; then
            echo "Cinnamon could not reload the applet; it remains enabled for next login." >&2
            return 1
        fi
        local running attempt
        for attempt in {1..10}; do
            running="$(gdbus call --session --dest org.Cinnamon --object-path /org/Cinnamon \
                --method org.Cinnamon.GetRunningXletUUIDs APPLET)"
            if [[ "$running" == *"'$UUID'"* ]]; then
                echo "  Cast panel icon is loaded and will return at future logins"
                return
            fi
            sleep 0.5
        done
        echo "Cinnamon did not load the Cast applet. Check Cinnamon's Looking Glass errors (Alt+F2, then lg)." >&2
        return 1
    fi
}

install_tray() {
    local tray="$HOME/.local/bin/linuxcast-tray"
    echo "* Tray -> $tray (starts now and at future desktop logins)"
    ln -sfn "$REPO/xfce/linuxcast-tray" "$tray"
    mkdir -p "$CONFIG_HOME/autostart"
    python3 - "$REPO/xfce/linuxcast-tray.desktop" "$CONFIG_HOME/autostart/linuxcast-tray.desktop" "$tray" <<'PY'
from pathlib import Path
import sys
source, dest, tray = sys.argv[1:]
# Desktop Entry Exec quoting; percent signs are field-code escapes.
quoted = '"' + tray.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'
Path(dest).write_text(Path(source).read_text().replace('@TRAY@', quoted))
PY
    if [[ "$DESKTOP" == *xfce* ]] && command -v xfconf-query >/dev/null; then
        local prop="/commands/custom/<Super>k" existing
        existing="$(xfconf-query -c xfce4-keyboard-shortcuts -p "$prop" 2>/dev/null || true)"
        if [[ -n "$existing" && "$existing" != "$tray --menu" ]]; then
            echo "  Super+K is already bound to '$existing'; leaving it alone"
        elif [[ -z "$existing" ]]; then
            xfconf-query -c xfce4-keyboard-shortcuts -p "$prop" -n -t string -s "$tray --menu"
        fi
    fi
}

start_tray() {
    # Cinnamon has its panel applet, so 'all' must not create a second icon there.
    if [[ "$DESKTOP" == *cinnamon* ]]; then return; fi
    if [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
        echo "  No graphical session; the tray will start automatically at next login."
        return
    fi
    mkdir -p "$RUNTIME"
    # Gtk.Application owns a unique D-Bus name: reruns activate the existing tray.
    nohup "$HOME/.local/bin/linuxcast-tray" </dev/null >>"$RUNTIME/tray.log" 2>&1 &
    local pid=$!
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        if ! wait "$pid"; then
            echo "Tray failed to start. See $RUNTIME/tray.log:" >&2
            tail -n 20 "$RUNTIME/tray.log" >&2
            return 1
        fi
    fi
    echo "  tray started; autostart is enabled"
}

install_dependencies
install_cli
case "$target" in
    cinnamon) install_cinnamon; install_nemo_action ;;
    xfce|tray)
        install_tray; install_nemo_action
        if [[ "$DESKTOP" == *cinnamon* ]]; then install_cinnamon; fi
        start_tray ;;
    all) install_cinnamon; install_tray; install_nemo_action; start_tray ;;
    cli) ;;
esac
echo "done"
