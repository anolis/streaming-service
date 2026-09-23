#!/usr/bin/env bash
# Install linuxcast and its desktop integrations for the current user.
#   ./install.sh            CLI + integrations for the running desktop
#   ./install.sh all        CLI + Cinnamon + Xfce integrations
#   ./install.sh cinnamon   / xfce / cli
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin/linuxcast"
UUID="linuxcast@anolis"

install_cli() {
    echo "* CLI -> $BIN"
    pipx install --force --editable "$REPO" >/dev/null 2>&1
}

install_nemo_action() {
    local dir="$HOME/.local/share/nemo/actions"
    echo "* Nemo right-click action -> $dir"
    mkdir -p "$dir"
    sed "s|@LINUXCAST@|$BIN|" "$REPO/cinnamon/nemo/linuxcast.nemo_action" > "$dir/linuxcast.nemo_action"
}

install_cinnamon() {
    local dir="$HOME/.local/share/cinnamon/applets"
    echo "* Cinnamon applet -> $dir/$UUID"
    mkdir -p "$dir"
    ln -sfn "$REPO/cinnamon/$UUID" "$dir/$UUID"

    local enabled
    enabled="$(gsettings get org.cinnamon enabled-applets)"
    if [[ "$enabled" != *"$UUID"* ]]; then
        # Next free instance id; place it at the right end of the first panel.
        local next
        next=$(grep -oE ":[0-9]+'" <<<"$enabled" | tr -d ":'" | sort -n | tail -1)
        next=$(( ${next:-0} + 1 ))
        gsettings set org.cinnamon enabled-applets \
            "${enabled%]}, 'panel1:right:1:$UUID:$next']"
        echo "  added to panel (Super+K opens it)"
    fi
    install_nemo_action
}

install_xfce() {
    local tray="$HOME/.local/bin/linuxcast-tray"
    echo "* Xfce tray app -> $tray (autostarts in Xfce only)"
    ln -sfn "$REPO/xfce/linuxcast-tray" "$tray"
    mkdir -p "$HOME/.config/autostart"
    sed "s|@TRAY@|$tray|" "$REPO/xfce/linuxcast-tray.desktop" \
        > "$HOME/.config/autostart/linuxcast-tray.desktop"

    # Super+K toggles the tray menu, like Win+K. Stored in xfconf, so it's
    # only active in Xfce sessions and doesn't touch Cinnamon's bindings.
    if command -v xfconf-query >/dev/null; then
        local prop="/commands/custom/<Super>k" existing
        existing="$(xfconf-query -c xfce4-keyboard-shortcuts -p "$prop" 2>/dev/null || true)"
        if [[ -n "$existing" && "$existing" != "$tray --menu" ]]; then
            echo "  Super+K is already bound to '$existing'; leaving it alone"
        else
            xfconf-query -c xfce4-keyboard-shortcuts -p "$prop" -n -t string -s "$tray --menu"
            echo "  Super+K bound in xfce4-keyboard-shortcuts"
        fi
    fi
    install_nemo_action
}

target="${1:-auto}"
if [[ "$target" == auto ]]; then
    case "${XDG_CURRENT_DESKTOP:-}" in
        *Cinnamon*) target=cinnamon ;;
        *XFCE*) target=xfce ;;
        *) target=cli ;;
    esac
fi

install_cli
case "$target" in
    cinnamon) install_cinnamon ;;
    xfce) install_xfce ;;
    all) install_cinnamon; install_xfce ;;
    cli) ;;
    *) echo "unknown target: $target" >&2; exit 2 ;;
esac
echo "done"
