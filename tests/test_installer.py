"""Run the actual installer against fake package managers and desktop services."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.geteuid() == 0, 'installer intentionally refuses root desktop installs')
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / 'repo'
        self.home = root / 'home with spaces'
        self.bin = root / 'bin'
        self.repo.mkdir()
        self.home.mkdir()
        self.bin.mkdir()
        for name in ('install.sh', 'cinnamon', 'xfce'):
            src, dst = REPO / name, self.repo / name
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        (self.repo / 'xfce/linuxcast-tray').write_text('#!/bin/sh\necho tray-start >> "$INSTALL_TEST_LOG"\n')
        self.log = root / 'commands.log'
        self.settings = root / 'settings.json'
        self.settings.write_text(json.dumps({'enabled-applets': '@as []',
                                             'panels-enabled': "['2:0:bottom']",
                                             'next-applet-id': '10'}))
        # Use a controlled PATH: missing ffmpeg/pipx are not masked by the host.
        for name in ('bash', 'dirname', 'mkdir', 'ln', 'grep', 'python3', 'id', 'nohup', 'sleep', 'tail'):
            (self.bin / name).symlink_to(shutil.which(name))
        stub = self.bin / 'stub'
        stub.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['INSTALL_TEST_LOG'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\\n')
if name == 'gsettings':
    path = Path(os.environ['INSTALL_TEST_SETTINGS'])
    settings = json.loads(path.read_text())
    if args[0] == 'list-schemas':
        print('org.cinnamon' if os.environ.get('INSTALL_TEST_CINNAMON', '1') == '1' else '')
    elif args[0] == 'get':
        print(settings[args[2]])
    elif args[0] == 'set':
        settings[args[2]] = args[3]
        path.write_text(json.dumps(settings))
elif name == 'sudo':
    os.execvp(args[0], args)
elif name == 'apt-get' and 'install' in args:
    for command in ('pipx', 'ffmpeg', 'ffprobe'):
        dest = Path(os.environ['INSTALL_TEST_BIN']) / command
        if not dest.exists():
            dest.symlink_to(Path(__file__).resolve())
elif name == 'pipx':
    sys.exit(int(os.environ.get('INSTALL_TEST_PIPX_EXIT', '0')))
elif name == 'gdbus':
    if 'org.Cinnamon.GetRunningXletUUIDs' in args:
        print("(['linuxcast@anolis'],)")
    sys.exit(int(os.environ.get('INSTALL_TEST_RELOAD_EXIT', '0')))
elif name == 'xfconf-query':
    sys.exit(1 if '-n' not in args else 0)
''')
        stub.chmod(0o755)
        for name in ('pipx', 'ffmpeg', 'ffprobe', 'xrandr', 'pactl', 'zenity', 'notify-send',
                     'gdbus', 'gsettings', 'apt-get', 'sudo', 'xfconf-query'):
            (self.bin / name).symlink_to(stub)
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin),
                        XDG_CURRENT_DESKTOP='X-Cinnamon', DISPLAY=':99',
                        XDG_CONFIG_HOME=str(root / 'config'), XDG_DATA_HOME=str(root / 'data'),
                        XDG_RUNTIME_DIR=str(root / 'runtime'), INSTALL_TEST_LOG=str(self.log),
                        INSTALL_TEST_SETTINGS=str(self.settings), INSTALL_TEST_BIN=str(self.bin))

    def run_installer(self, *args, **env):
        result = subprocess.run(['/bin/bash', str(self.repo / 'install.sh'), *args],
                                env=self.env | env, text=True, capture_output=True, timeout=20)
        return result

    def commands(self):
        return self.log.read_text() if self.log.exists() else ''

    def test_cinnamon_default_enables_correct_panel_and_reloads(self):
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stderr)
        settings = json.loads(self.settings.read_text())
        self.assertEqual(ast.literal_eval(settings['enabled-applets']), ['panel2:right:1:linuxcast@anolis:10'])
        self.assertEqual(settings['next-applet-id'], '11')
        self.assertIn('org.Cinnamon.ReloadXlet', self.commands())
        self.assertNotIn('tray-start', self.commands())
        self.assertTrue((Path(self.env['XDG_DATA_HOME']) / 'cinnamon/applets/linuxcast@anolis').is_symlink())

    def test_all_reinstall_is_persistent_without_duplicate_cinnamon_icon(self):
        for _ in range(2):
            r = self.run_installer('all')
            self.assertEqual(r.returncode, 0, r.stderr)
        entries = ast.literal_eval(json.loads(self.settings.read_text())['enabled-applets'])
        self.assertEqual(len(entries), 1)
        self.assertEqual(self.commands().count('org.Cinnamon.ReloadXlet'), 2)
        desktop = Path(self.env['XDG_CONFIG_HOME']) / 'autostart/linuxcast-tray.desktop'
        text = desktop.read_text()
        self.assertIn('NotShowIn=X-Cinnamon;Cinnamon;', text)
        self.assertIn('Exec="' + str(self.home), text)
        self.assertNotIn('tray-start', self.commands())

    def test_all_on_xfce_without_cinnamon_still_starts_tray(self):
        r = self.run_installer('all', XDG_CURRENT_DESKTOP='XFCE', INSTALL_TEST_CINNAMON='0')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('tray-start', self.commands())
        self.assertNotIn('org.Cinnamon.ReloadXlet', self.commands())

    def test_missing_dependencies_are_installed_before_cli(self):
        for name in ('pipx', 'ffmpeg', 'ffprobe'):
            (self.bin / name).unlink()
        r = self.run_installer('cinnamon')
        self.assertEqual(r.returncode, 0, r.stderr)
        commands = self.commands()
        self.assertIn('["apt-get", "update"]', commands)
        self.assertIn('["apt-get", "install", "-y", "pipx", "ffmpeg", "ffmpeg"', commands)
        self.assertLess(commands.index('["apt-get", "install"'), commands.index('["pipx", "install"'))

    def test_reload_failure_is_visible(self):
        r = self.run_installer('cinnamon', INSTALL_TEST_RELOAD_EXIT='1')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('could not reload', r.stderr)

    def test_pipx_failure_stops_installation(self):
        r = self.run_installer('all', INSTALL_TEST_PIPX_EXIT='1')
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('gsettings', self.commands())

    def test_invalid_target_does_not_install_anything(self):
        r = self.run_installer('invalid')
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.commands(), '')

    def test_headless_install_preserves_autostart_without_launch(self):
        r = self.run_installer('tray', XDG_CURRENT_DESKTOP='', DISPLAY='', WAYLAND_DISPLAY='')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('next login', r.stdout)
        self.assertNotIn('tray-start', self.commands())
