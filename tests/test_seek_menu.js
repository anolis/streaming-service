// Exercise Cinnamon menu event handling without a desktop or receiver.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Item {
    constructor(text) { this.text = text; this.label = {set_text: t => {this.text = t;}}; this.actor = {}; this.events = {}; }
    connect(name, callback) { this.events[name] = callback; }
    setSensitive(value) { this.sensitive = value; }
    setValue(value) { this.value = value; }
    emit(name, ...args) { this.events[name](this, ...args); }
}
const timers = new Map(); let serial = 0;
const sandbox = {imports: {ui: {applet: {IconApplet: class {}}, popupMenu: {
    PopupMenuItem: Item, PopupSliderMenuItem: Item, PopupSeparatorMenuItem: Item
}, settings: {}, main: {}}, misc: {util: {}}, gi: {Gio: {}, St: {}, GLib: {
    build_filenamev: paths => paths.join('/'), get_user_runtime_dir: () => '/tmp',
    get_monotonic_time: () => 10000000,
    timeout_add: (_priority, _delay, fn) => {timers.set(++serial, fn);return serial;},
    source_remove: id => timers.delete(id)
}}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('cinnamon/linuxcast@anolis/applet.js', 'utf8') + '\nthis.Prototype = CastApplet.prototype;', sandbox);
const app = Object.create(sandbox.Prototype);
let rebuilds = 0; const commands = [];
app._statusSection = {removeAll: () => rebuilds++, addMenuItem: () => {}, addAction: () => {}};
app._header = text => new Item(text);
app._playback = (...args) => commands.push(args);
app.state = {status: 'casting', what: 'movie', device: {name: 'TV'}, control: '/session',
    playback: {state: 'PLAYING', play: false, pause: true, seek: true, next: false, position: 20, duration: 100}};
app._syncStatus();
const slider = app._seekSlider;
assert.equal(slider.value, .2);
assert.equal(app._seekLabel.text, '0:20 / 1:40');
app.state.playback.position = 30;
app._syncStatus();
assert.equal(rebuilds, 1, 'progress must not rebuild the open menu');
assert.equal(slider.value, .3);
slider.emit('drag-begin');
slider.emit('value-changed', .75);
app.state.playback.position = 31;
app._syncStatus();
assert.equal(commands.length, 0, 'no seek while dragging');
assert.equal(app._seekLabel.text, '1:15 / 1:40');
slider.emit('drag-end');
assert.deepEqual(commands[0], ['seek-to', 75, '/session']);
slider.emit('value-changed', .8);
slider.emit('value-changed', .9);
assert.equal(timers.size, 1, 'wheel events coalesce');
for (const callback of timers.values()) callback();
assert.deepEqual(commands[1], ['seek-to', 90, '/session']);
app._seekPendingUntil = 0;
app.state.playback.duration = null;
app._syncStatus();
assert.equal(slider.sensitive, false);
console.log('PASS: Cinnamon timeline progress, drag release, wheel debounce, unknown duration');
