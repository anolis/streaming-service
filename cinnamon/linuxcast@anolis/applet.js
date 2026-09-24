// Cast applet: a Win+K style "Cast to device" menu backed by the linuxcast CLI.
//
// The applet never casts itself. It launches `linuxcast` detached (so a cast
// survives Cinnamon restarts) and reads $XDG_RUNTIME_DIR/linuxcast/session.json,
// which the CLI keeps up to date, to show what is currently casting. Casts
// started from a terminal or the Nemo action therefore show up here too.

const Applet = imports.ui.applet;
const PopupMenu = imports.ui.popupMenu;
const Settings = imports.ui.settings;
const Main = imports.ui.main;
const Util = imports.misc.util;
const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const St = imports.gi.St;

const UUID = "linuxcast@anolis";
const RUNTIME = GLib.build_filenamev([GLib.get_user_runtime_dir(), "linuxcast"]);
const STATE_FILE = GLib.build_filenamev([RUNTIME, "session.json"]);
const LOG_FILE = GLib.build_filenamev([RUNTIME, "applet.log"]);
const DEVICE_TTL_SECONDS = 60;
const ICON_IDLE = "video-display-symbolic";
const ICON_ACTIVE = "screen-shared-symbolic";
// Icon themes disagree on a TV icon, so fall back to a plain display.
const DEVICE_ICONS = ["tv-symbolic", "video-display-tv-symbolic", "video-display-symbolic"];

class CastApplet extends Applet.IconApplet {
    constructor(metadata, orientation, panelHeight, instanceId) {
        super(orientation, panelHeight, instanceId);
        this.instanceId = instanceId;

        this.settings = new Settings.AppletSettings(this, UUID, instanceId);
        this.settings.bind("hotkey", "hotkey", () => this._bindHotkey());
        this.settings.bind("include-audio", "includeAudio");
        this.settings.bind("monitor", "monitor");
        this.settings.bind("linuxcast-path", "binPath");

        this.menuManager = new PopupMenu.PopupMenuManager(this);
        this.menu = new Applet.AppletPopupMenu(this, orientation);
        this.menuManager.addMenu(this.menu);
        this.menu.connect("open-state-changed", (menu, open) => {
            if (open) {
                this._refreshDevices(false);
                this._loadMonitors();
            }
        });

        this.devices = [];
        this.devicesAt = 0;
        this.scanning = false;
        this.monitors = [];
        this.state = null;
        this._stateJson = "";

        this._bindHotkey();
        this._readState();
        this._pollId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => {
            this._readState();
            return GLib.SOURCE_CONTINUE;
        });
        this._loadMonitors();
        this._refreshDevices(true);
        this._update();
    }

    on_applet_clicked() {
        this.menu.toggle();
    }

    on_applet_removed_from_panel() {
        if (this._pollId)
            GLib.source_remove(this._pollId);
        Main.keybindingManager.removeHotKey(this._hotkeyName());
        this.settings.finalize();
    }

    _hotkeyName() {
        return `${UUID}-open-${this.instanceId}`;
    }

    _bindHotkey() {
        Main.keybindingManager.removeHotKey(this._hotkeyName());
        if (this.hotkey)
            Main.keybindingManager.addHotKey(this._hotkeyName(), this.hotkey, () => this.menu.toggle());
    }

    // ---- talking to the CLI -------------------------------------------------

    _bin() {
        let path = (this.binPath || "").replace(/^~(?=\/|$)/, GLib.get_home_dir());
        if (path && GLib.file_test(path, GLib.FileTest.IS_EXECUTABLE))
            return path;
        return GLib.find_program_in_path("linuxcast");
    }

    // Run linuxcast and hand its stdout to callback (for quick queries).
    _query(args, callback) {
        let bin = this._bin();
        if (!bin) {
            this._missingBin();
            return;
        }
        Util.spawnCommandLineAsyncIO(null, (stdout, stderr, code) => {
            if (code !== 0)
                global.logError(`${UUID}: linuxcast ${args.join(" ")} failed: ${stderr}`);
            callback(code === 0 ? stdout : null);
        }, { argv: [bin, ...args] });
    }

    // Start a long-running cast in its own session so it outlives Cinnamon.
    _launch(args) {
        let bin = this._bin();
        if (!bin) {
            this._missingBin();
            return;
        }
        GLib.mkdir_with_parents(RUNTIME, 0o700);
        let script = `exec "$0" "$@" >>${GLib.shell_quote(LOG_FILE)} 2>&1`;
        Util.spawn(["setsid", "-f", "sh", "-c", script, bin, ...args, "--notify"]);
        // Pick up the "connecting" state as soon as the CLI writes it.
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 400, () => {
            this._readState();
            return GLib.SOURCE_REMOVE;
        });
    }

    _missingBin() {
        Main.notifyError("Cast", "linuxcast was not found. Set its path in the applet settings.");
    }

    _refreshDevices(force) {
        let fresh = GLib.get_monotonic_time() / 1e6 - this.devicesAt < DEVICE_TTL_SECONDS;
        if (this.scanning || (fresh && !force))
            return;
        this.scanning = true;
        this._update();
        this._query(["devices", "--json", "-t", "3"], (out) => {
            this.scanning = false;
            if (out !== null) {
                try {
                    this.devices = JSON.parse(out).sort((x, y) => (y.castable ? 1 : 0) - (x.castable ? 1 : 0));
                    this.devicesAt = GLib.get_monotonic_time() / 1e6;
                } catch (e) {
                    global.logError(`${UUID}: bad device JSON: ${e}`);
                }
            }
            this._update();
        });
    }

    _loadMonitors() {
        this._query(["monitors", "--json"], (out) => {
            if (out === null)
                return;
            try {
                this.monitors = JSON.parse(out);
            } catch (e) {
                return;
            }
            this._update();
        });
    }

    _readState() {
        let json = "";
        try {
            let [ok, bytes] = GLib.file_get_contents(STATE_FILE);
            if (ok)
                json = imports.byteArray.toString(bytes);
        } catch (e) {
            // no file: not casting
        }
        let state = null;
        if (json) {
            try {
                state = JSON.parse(json);
                if (!GLib.file_test(`/proc/${state.pid}`, GLib.FileTest.EXISTS))
                    state = null; // process died without cleaning up
            } catch (e) {
                state = null;
            }
        }
        let key = state ? json : "";
        if (key === this._stateJson)
            return;
        this._stateJson = key;
        this.state = state;
        this._update();
    }

    // ---- actions --------------------------------------------------------------

    _mirrorTo(device) {
        let args = ["mirror", "--target", JSON.stringify(device)];
        if (this.monitor)
            args.push("-m", this.monitor);
        if (!this.includeAudio)
            args.push("--no-audio");
        this._launch(args);
    }

    _castFile() {
        let dialog = ["zenity", "--file-selection", "--title=Cast a media file",
            "--file-filter=Media | *.mp4 *.mkv *.webm *.mov *.avi *.m4v *.ts *.mp3 *.flac *.ogg *.wav *.m4a",
            "--file-filter=All files | *"];
        Util.spawnCommandLineAsyncIO(null, (stdout, stderr, code) => {
            let path = (stdout || "").trim();
            if (code !== 0 || !path)
                return; // cancelled
            let args = ["play", path];
            let castable = this.devices.filter(d => d.castable);
            if (castable.length === 1)
                args.push("--target", JSON.stringify(castable[0]));
            else
                args.push("--gui"); // let the CLI discover and show a picker
            this._launch(args);
        }, { argv: dialog });
    }

    _stop() {
        this._query(["stop"], () => this._readState());
    }

    // ---- UI ---------------------------------------------------------------------

    _header(text) {
        let item = new PopupMenu.PopupMenuItem(text, { reactive: false });
        item.label.add_style_class_name("popup-subtitle-menu-item");
        return item;
    }

    _monitorLabel() {
        if (this.monitor)
            return this.monitor;
        let primary = this.monitors.find(m => m.primary);
        return primary ? `Primary (${primary.name})` : "Primary";
    }

    _update() {
        let s = this.state;
        if (s) {
            let verb = {searching: "Looking for", connecting: "Connecting to"}[s.status] || "Casting to";
            this.set_applet_icon_symbolic_name(ICON_ACTIVE);
            this.set_applet_tooltip(`${verb} ${s.device.name}: ${s.what}`);
        } else {
            this.set_applet_icon_symbolic_name(ICON_IDLE);
            this.set_applet_tooltip("Cast");
        }
        // Update in place: rebuilding the whole menu while it's open swaps out the
        // item under the pointer and collapses the Screen submenu.
        if (!this._built)
            this._buildMenu();
        this._audio.setToggleState(this.includeAudio);
        this._syncStatus();
        this._syncDevices();
        this._syncScreens();
    }

    _buildMenu() {
        this._built = true;
        this._statusSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._statusSection);

        this.menu.addMenuItem(this._header("Mirror screen to"));
        this._deviceSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._deviceSection);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._screens = new PopupMenu.PopupSubMenuMenuItem("");
        this.menu.addMenuItem(this._screens);

        this._audio = new PopupMenu.PopupSwitchMenuItem("Include desktop audio", this.includeAudio);
        this._audio.connect("toggled", (item, on) => { this.includeAudio = on; });
        this.menu.addMenuItem(this._audio);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction("Cast a media file…", () => this._castFile());
        this.menu.addAction("Miracast (GNOME Network Displays)…",
                            () => this._launch(["mirror", "-b", "miracast"]));
        this.menu.addAction("Refresh devices", () => this._refreshDevices(true));
    }

    _syncStatus() {
        let s = this.state;
        let key = s ? `${s.status}|${s.device.name}|${s.what}` : "";
        if (key === this._statusKey)
            return;
        this._statusKey = key;
        this._statusSection.removeAll();
        if (!s)
            return;
        let verb = {searching: "Looking for", connecting: "Connecting to"}[s.status] || "Casting to";
        this._statusSection.addMenuItem(this._header(`${verb} ${s.device.name}`));
        this._statusSection.addMenuItem(new PopupMenu.PopupMenuItem(s.what, { reactive: false }));
        this._statusSection.addAction("Stop casting", () => this._stop());
        this._statusSection.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
    }

    _syncDevices() {
        let activeId = this.state ? this.state.device.id : null;
        // "Searching…" only shows with an empty list, so a scan that finds the
        // same devices doesn't touch the items under the pointer.
        let key = JSON.stringify([this.devices.map(d => [d.id, d.name, d.castable, d.unsupported, d.host, d.extra]),
                                  activeId, this.scanning && this.devices.length === 0]);
        if (key === this._devicesKey)
            return;
        this._devicesKey = key;
        let section = this._deviceSection;
        section.removeAll();
        for (let device of this.devices) {
            let label = device.name;
            if (!device.castable)
                label += `  (${device.backend}: ${device.unsupported || "not supported"})`;
            else if (device.id === activeId)
                label += "  (active)";
            let item = new PopupMenu.PopupIconMenuItem(label, DEVICE_ICONS[0], St.IconType.SYMBOLIC,
                                                       { reactive: !!device.castable });
            item._icon.gicon = Gio.ThemedIcon.new_from_names(DEVICE_ICONS);
            if (device.castable)
                item.connect("activate", () => this._mirrorTo(device));
            section.addMenuItem(item);
        }
        if (this.scanning && this.devices.length === 0)
            section.addMenuItem(new PopupMenu.PopupMenuItem("Searching…", { reactive: false }));
        else if (!this.scanning && this.devices.length === 0)
            section.addMenuItem(new PopupMenu.PopupMenuItem("No devices found", { reactive: false }));
    }

    _syncScreens() {
        this._screens.label.set_text(`Screen: ${this._monitorLabel()}`);
        let key = JSON.stringify(this.monitors.map(m => [m.name, m.width, m.height]));
        if (key !== this._monitorsKey) {
            this._monitorsKey = key;
            this._screens.menu.removeAll();
            this._screenItems = [];
            let choices = [["", "Primary"], ...this.monitors.map(m => [m.name, `${m.name}  ${m.width}×${m.height}`])];
            for (let [name, label] of choices) {
                let item = new PopupMenu.PopupIndicatorMenuItem(label);
                item.connect("activate", () => {
                    this.monitor = name;
                    this._syncScreens();
                });
                this._screens.menu.addMenuItem(item);
                this._screenItems.push([name, item]);
            }
        }
        for (let [name, item] of this._screenItems)
            item.setOrnament(PopupMenu.OrnamentType.DOT, this.monitor === name);
    }
}

function main(metadata, orientation, panelHeight, instanceId) {
    return new CastApplet(metadata, orientation, panelHeight, instanceId);
}
