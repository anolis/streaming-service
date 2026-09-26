import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from linuxcast import cli
from linuxcast.base import BackendUnavailable, CaptureOptions, Device
from linuxcast.backends.airplay import AirPlayBackend
from linuxcast.backends.airplay_native import (AirPlayNativeBackend, NativeSession,
                                              ENGINE_REVISION, bitrate_kbps)


class NativeAdapterTests(unittest.TestCase):
    def test_discovery_distinguishes_mirroring_from_url_video(self):
        devices = [Device('airplay', 'a', 'Samsung', extra={'mirroring': True, 'video': False}),
                   Device('airplay', 'b', 'Speaker', extra={'mirroring': False})]
        with patch.object(AirPlayBackend, 'discover', return_value=devices):
            native = AirPlayNativeBackend().discover()
        self.assertEqual(len(native), 1)
        self.assertEqual(native[0].backend, 'airplay-native')
        self.assertFalse(native[0].extra['video'])

    def test_native_is_not_a_file_target(self):
        dev = Device('airplay-native', 'id', 'TV')
        args = SimpleNamespace(target=json.dumps(dev.__dict__), cmd='play')
        with self.assertRaisesRegex(cli.Failure, 'mirroring only'):
            cli.choose_device(args)

    def test_command_forwards_capture_geometry_and_native_latency(self):
        backend = AirPlayNativeBackend()
        with patch.object(backend, '_command', return_value=['engine']), \
             patch('linuxcast.backends.airplay_native.capture.pick_monitor',
                   return_value=SimpleNamespace(x=1680, y=0, width=1920, height=1080)), \
             patch('linuxcast.backends.airplay_native.NativeSession.start') as start, \
             patch.dict(os.environ, {'XDG_SESSION_TYPE': 'x11'}):
            backend.mirror(Device('airplay-native', 'id', 'TV'),
                           CaptureOptions(resolution='720p', fps=60, audio=False,
                                          encoder='x264', airplay_latency_ms=100))
        cmd = start.call_args.args[0]
        for flag, value in [('-fps', '60'), ('-output-width', '1280'), ('-output-height', '720'),
                            ('-crop-x', '1680'), ('-crop-width', '1920'),
                            ('-target-latency-ms', '100'), ('-hwaccel', 'none')]:
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertIn('-no-audio', cmd)
        self.assertNotIn('-buffer', cmd)

    def test_missing_engine_is_actionable(self):
        with patch('linuxcast.backends.airplay_native.engine_path', return_value=None):
            reason = AirPlayNativeBackend().unsupported_reason(Device('airplay-native', 'id', 'TV'))
        self.assertIn('./install.sh airplay', reason)

    def test_old_bridge_requires_reinstallation(self):
        with patch('linuxcast.backends.airplay_native.engine_path', return_value='/engine'), \
             patch('linuxcast.backends.airplay_native.subprocess.run',
                   return_value=SimpleNamespace(stdout=f'linuxcast-airplay/1 {ENGINE_REVISION}')):
            with self.assertRaisesRegex(BackendUnavailable, 'version mismatch'):
                AirPlayNativeBackend()._command(Device('airplay-native', 'id', 'TV'))

    def test_bitrate_conversion(self):
        self.assertEqual(bitrate_kbps('8M'), 8000)
        self.assertEqual(bitrate_kbps('4500k'), 4500)
        self.assertEqual(bitrate_kbps('8000000'), 8000)
        with self.assertRaises(BackendUnavailable):
            bitrate_kbps('invalid')

    def test_pair_prompt_and_process_teardown(self):
        script = '''import json, sys, time
print('LINUXCAST_EVENT ' + json.dumps({'type':'credential','prompt':'Enter PIN:'}), flush=True)
assert sys.stdin.readline().strip() == '1234'
print('LINUXCAST_EVENT ' + json.dumps({'type':'ready'}), flush=True)
time.sleep(60)
'''
        prompt = Mock(return_value='1234')
        session = NativeSession.start([sys.executable, '-c', script], prompt=prompt, timeout=3)
        try:
            prompt.assert_called_once_with('Enter PIN:')
            self.assertIsNone(session.proc.poll())
        finally:
            session.close()
        self.assertIsNotNone(session.proc.poll())
        self.assertFalse(session._reader.is_alive())
        session.close()  # idempotent

    def test_early_engine_failure_is_reported(self):
        with self.assertRaisesRegex(BackendUnavailable, 'synthetic setup failure'):
            NativeSession.start([sys.executable, '-c',
                                 "print('synthetic setup failure', flush=True); raise SystemExit(1)"], timeout=3)

    def test_no_ready_event_times_out(self):
        with self.assertRaisesRegex(BackendUnavailable, 'timed out'):
            NativeSession.start([sys.executable, '-c', 'import time; time.sleep(60)'], timeout=.2)


@unittest.skipUnless(os.environ.get('LINUXCAST_AIRPLAY_TEST_RECEIVER') and
                     os.environ.get('LINUXCAST_AIRPLAY_NATIVE'), 'native test binaries not configured')
class NativeWireTests(unittest.TestCase):
    def test_real_sender_pairs_and_streams_to_local_receivers(self):
        for profile in ('modern', 'roku', 'lg', 'appletv3', 'uxplay'):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as td:
                logfile = Path(td) / 'receiver.log'
                with logfile.open('w') as log:
                    receiver = subprocess.Popen([os.environ['LINUXCAST_AIRPLAY_TEST_RECEIVER'],
                                                 '-profile', profile, '-auth', ('none' if profile in ('appletv3', 'uxplay') else 'pin'), '-code', ('' if profile in ('appletv3', 'uxplay') else '1234'),
                                                 '-listen', '127.0.0.1:0', '-stats-interval', '200ms'],
                                                stdout=log, stderr=log)
                session = None
                try:
                    deadline = time.monotonic() + 5
                    port = None
                    while time.monotonic() < deadline:
                        text = logfile.read_text()
                        match = re.search(r'127\.0\.0\.1:(\d+)', text)
                        if match:
                            port = match[1]
                            break
                        time.sleep(.05)
                    self.assertIsNotNone(port, logfile.read_text())
                    command = [os.environ['LINUXCAST_AIRPLAY_NATIVE'], '-linuxcast-events',
                               '-target', '127.0.0.1', '-port', port, '-creds', td + '/credentials.json',
                               '-test', '-hwaccel', 'none', '-video-codec', 'h264',
                               '-output-width', '1280', '-output-height', '720']
                    if profile != 'modern':
                        command.append('-no-audio')
                    session = NativeSession.start(command, prompt=lambda _: '1234', timeout=30)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        text = logfile.read_text()
                        if (re.search(r'video=[1-9]\d*/[1-9]\d*B', text) and
                                (profile != 'modern' or re.search(r'audio=[1-9]\d*/[1-9]\d*B', text))):
                            break
                        time.sleep(.1)
                    self.assertRegex(logfile.read_text(), r'video=[1-9]\d*/[1-9]\d*B')
                    if profile == 'modern':
                        self.assertRegex(logfile.read_text(), r'audio=[1-9]\d*/[1-9]\d*B')
                    if profile == 'modern':
                        soak = float(os.environ.get('LINUXCAST_AIRPLAY_SOAK_SECONDS', '0'))
                        deadline = time.monotonic() + soak
                        last_check = time.monotonic()
                        previous = (0, 0)
                        while time.monotonic() < deadline:
                            self.assertIsNone(session.proc.poll(), 'sender exited during soak')
                            if time.monotonic() - last_check >= 5:
                                stats = re.findall(r'video=([0-9]+)/[0-9]+B audio=([0-9]+)/[0-9]+B', logfile.read_text())
                                self.assertTrue(stats)
                                current = tuple(map(int, stats[-1]))
                                self.assertGreater(current[0], previous[0], 'video stalled')
                                self.assertGreater(current[1], previous[1], 'audio stalled')
                                previous = current
                                last_check = time.monotonic()
                            time.sleep(.2)
                    session.close()
                    self.assertEqual(session.proc.returncode, 0, 'sender needed forced shutdown')
                    session = None
                    # Repeat using the persisted native credentials, without prompting.
                    session = NativeSession.start(command, prompt=Mock(side_effect=AssertionError('unexpected PIN')), timeout=30)
                finally:
                    if session:
                        session.close()
                    receiver.send_signal(signal.SIGINT)
                    try:
                        receiver.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        receiver.kill()
                        receiver.wait()
