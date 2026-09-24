import ast
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from linuxcast import capture, cli
from linuxcast.base import CaptureOptions, Device, RESOLUTIONS
from linuxcast.backends.airplay import AirPlayBackend
from linuxcast.backends.chromecast import ChromecastBackend


class CaptureSettingsTests(unittest.TestCase):
    def test_existing_defaults(self):
        opts = CaptureOptions()
        self.assertEqual((opts.resolution, opts.fps, opts.buffer_seconds), ('1080p', 30, 8))

    def test_resolution_and_frame_rate_reach_both_capture_formats(self):
        with patch.object(capture, 'pick_monitor', return_value=capture.Monitor('test', 1200, 1920, 0, 0, True)), \
             patch.dict(os.environ, {'XDG_SESSION_TYPE': 'x11'}):
            for resolution, (width, height) in RESOLUTIONS.items():
                for factory in (lambda opts: capture.screen_command(opts, Path('/tmp')),
                                capture.screen_ts_command):
                    opts = CaptureOptions(resolution=resolution, fps=60, encoder='x264', audio=False)
                    cmd = factory(opts)
                    self.assertEqual(cmd[cmd.index('-framerate') + 1], '60')
                    self.assertEqual(cmd[cmd.index('-g') + 1], '60')
                    vf = cmd[cmd.index('-vf') + 1]
                    self.assertIn(f'pad={width}:{height}', vf)
                    self.assertIn('force_divisible_by=2', vf)

    def test_buffer_controls_hls_start_position_and_readiness(self):
        for backend in (ChromecastBackend(), AirPlayBackend()):
            for seconds in (2, 8, 20):
                with self.subTest(backend=backend.name, seconds=seconds), tempfile.TemporaryDirectory() as td:
                    module = f'linuxcast.backends.{backend.name}'
                    server, hls, running = Mock(), Mock(), Mock()
                    with patch(f'{module}.MediaServer') as factory, \
                         patch(f'{module}.local_ip_for', return_value='127.0.0.1'), \
                         patch.object(capture, 'screen_command', return_value=['ffmpeg']), \
                         patch.object(capture, 'HlsProcess', return_value=hls), \
                         patch.object(backend, '_start', return_value=running) as start:
                        factory.return_value.start.return_value = server
                        backend.mirror(Device(backend.name, 'id', 'TV', extra={'paired': True}),
                                       CaptureOptions(buffer_seconds=seconds, workdir=Path(td)))
                        server.set_playlist_header.assert_called_once_with(
                            f'#EXT-X-START:TIME-OFFSET=-{seconds},PRECISE=YES')
                        hls.wait_ready.assert_called_once_with(segments=seconds + 2, timeout=seconds + 20)
                        # Exercise the registered cleanup callback too.
                        cleanup = start.call_args.kwargs.get('cleanup')
                        if cleanup is None:
                            cleanup = start.call_args.args[2]
                        cleanup()
                        hls.stop.assert_called_once()
                        server.close.assert_called_once()

    def test_cli_forwards_settings(self):
        backend = Mock()
        device = Device('dlna', 'id', 'TV')
        with patch.object(capture, 'pick_monitor', return_value=SimpleNamespace(name='test')), \
             patch.object(cli, 'run_session', side_effect=lambda args, kind, what, start: start(backend, device)):
            cli.main(['mirror', '--resolution', '720p', '--fps', '24', '--buffer', '4'])
        opts = backend.mirror.call_args.args[1]
        self.assertEqual((opts.resolution, opts.fps, opts.buffer_seconds), ('720p', 24, 4))

    def test_invalid_settings_fail_before_start(self):
        for args in (['--resolution', 'bogus'], ['--fps', '0'], ['--fps', '61'],
                     ['--buffer', '1'], ['--buffer', '21']):
            with self.subTest(args=args), patch('sys.stderr', new_callable=io.StringIO), \
                 patch.object(cli, 'run_session') as start:
                with self.assertRaises(SystemExit) as error:
                    cli.main(['mirror', *args])
                self.assertEqual(error.exception.code, 2)
                start.assert_not_called()

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg unavailable')
    def test_portrait_filter_encodes_even_dimensions(self):
        result = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error',
                                 '-filter_threads', '1', '-f', 'lavfi', '-i', 'color=s=90x160',
                                 '-vf', capture._scale_filter(False, '480p'), '-frames:v', '1',
                                 '-c:v', 'libx264', '-threads', '1', '-f', 'null', '-'],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tray_preferences_migrate_persist_and_forward(self):
        # Load the actual preferences and launch method without needing a GTK display.
        tree = ast.parse(Path('xfce/linuxcast-tray').read_text())
        prefs = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Prefs')
        tray = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CastTray')
        mirror = next(n for n in tray.body if isinstance(n, ast.FunctionDef) and n.name == 'mirror_to')
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'tray.json'
            scope = {'json': json, 'PREFS_FILE': path, 'RESOLUTIONS': list(RESOLUTIONS),
                     'FRAME_RATES': [15, 24, 30, 60]}
            exec(compile(ast.Module(body=[prefs, mirror], type_ignores=[]), '<tray>', 'exec'), scope)
            path.write_text(json.dumps({'monitor': 'DP-2', 'audio': False}))
            pref = scope['Prefs']()
            self.assertEqual((pref.resolution, pref.fps, pref.buffer_seconds), ('1080p', 30, 8))
            pref.resolution, pref.fps, pref.buffer_seconds = '720p', 60, 4
            pref.save()
            loaded = scope['Prefs']()
            launcher = SimpleNamespace(prefs=loaded, launch=Mock())
            scope['mirror_to'](launcher, {'id': 'TV'})
            args = launcher.launch.call_args.args[0]
            self.assertEqual(args[args.index('--resolution') + 1], '720p')
            self.assertEqual(args[args.index('--fps') + 1], '60')
            self.assertEqual(args[args.index('--buffer') + 1], '4')

    @unittest.skipUnless(shutil.which('node'), 'node unavailable')
    def test_cinnamon_passes_settings_to_cli(self):
        js = '''const fs = require('fs'), vm = require('vm'), assert = require('assert');
const context = {imports: {ui: {applet: {IconApplet: class {}}, popupMenu: {}, settings: {}},
  misc: {}, gi: {GLib: {build_filenamev: p => p.join('/'), get_user_runtime_dir: () => '/tmp'}}}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('cinnamon/linuxcast@anolis/applet.js', 'utf8') +
                '\\nthis.CastApplet = CastApplet;', context);
const app = Object.create(context.CastApplet.prototype);
Object.assign(app, {outputResolution: '720p', targetFps: 60, bufferSeconds: 4, includeAudio: true});
app._launch = args => {
  assert.equal(args[args.indexOf('--resolution') + 1], '720p');
  assert.equal(args[args.indexOf('--fps') + 1], '60');
  assert.equal(args[args.indexOf('--buffer') + 1], '4');
};
app._mirrorTo({id: 'TV'});
'''
        result = subprocess.run(['node', '-e', js], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
