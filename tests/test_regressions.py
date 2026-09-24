import concurrent.futures
import io
import json
import multiprocessing
import os
from pathlib import Path
import signal
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
from dataclasses import asdict

from linuxcast import cli, session
from linuxcast.base import CaptureOptions, Device
from linuxcast.backends.airplay import AirPlayBackend, AirPlaySession
from linuxcast.backends.chromecast import ChromecastBackend, CastSession
from linuxcast.backends.dlna import DlnaBackend
from linuxcast.httpserver import MediaServer


def claim_worker(state_path, ready, claimed):
    session.STATE = Path(state_path)
    def stop(*_):
        session.clear()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    ready.wait()
    session.replace_current('mirror', 'test')
    claimed.put(os.getpid())
    while True:
        time.sleep(0.05)


class DeviceSelectionTests(unittest.TestCase):
    def test_discovery_json_round_trip(self):
        dev = Device('dlna', 'test', 'TV', '192.0.2.1')
        args = SimpleNamespace(target=json.dumps(asdict(dev) | {'castable': True, 'unsupported': None}))
        self.assertEqual(cli.choose_device(args), dev)

    def test_pair_and_stop_can_select_nonvideo_airplay(self):
        dev = Device('airplay', 'test', 'TV', extra={'video': False})
        args = SimpleNamespace(target=None, backend='airplay', device='TV', timeout=0)
        with patch.object(cli, 'discover_all', return_value=[dev]):
            self.assertEqual(cli.choose_device(args, castable=False), dev)
            with self.assertRaises(cli.Failure):
                cli.choose_device(args)

    def test_target_cannot_bypass_capabilities(self):
        dev = Device('airplay', 'test', 'TV', extra={'video': False})
        args = SimpleNamespace(target=json.dumps(asdict(dev)))
        with self.assertRaises(cli.Failure):
            cli.choose_device(args)

    def test_miracast_handoff_skips_capture_and_discovery(self):
        with patch.object(cli.BACKENDS['miracast'], 'launch') as launch, \
             patch.object(cli.capture, 'pick_monitor', side_effect=AssertionError), \
             patch.object(cli, 'discover_all', side_effect=AssertionError), \
             patch('sys.stdout', new_callable=io.StringIO):
            cli.main(['mirror', '-b', 'miracast'])
        launch.assert_called_once()

    def test_cancelled_picker_terminates_child(self):
        proc = Mock()
        proc.communicate.side_effect = KeyboardInterrupt
        proc.poll.return_value = None
        with patch.object(cli.subprocess, 'Popen', return_value=proc):
            with self.assertRaises(KeyboardInterrupt):
                cli._gui_pick([])
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once()


class AirPlayTests(unittest.TestCase):
    def test_pending_poll_does_not_end_playback(self):
        future = Mock()
        future.result.side_effect = [concurrent.futures.TimeoutError(), None]
        future.done.return_value = False
        cast = AirPlaySession(None, future, lambda: None, False)
        cast.wait()
        self.assertIsNone(cast.last_error)
        self.assertEqual(future.result.call_count, 2)

    def test_completed_timeout_is_reported(self):
        future = concurrent.futures.Future()
        future.set_exception(concurrent.futures.TimeoutError())
        cast = AirPlaySession(None, future, lambda: None, False)
        cast.wait()
        self.assertEqual(cast.last_error, 'AirPlay playback timed out')


class ServingTests(unittest.TestCase):
    def test_registered_only_server_blocks_siblings_and_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / 'movie.mp4'
            media.write_bytes(b'0123456789')
            (root / 'private.txt').write_text('private')
            server = MediaServer(None, '127.0.0.1').start()
            try:
                url = server.add_file(media)
                with urllib.request.urlopen(url) as r:
                    self.assertEqual(r.read(), b'0123456789')
                req = urllib.request.Request(url, headers={'Range': 'bytes=2-4'})
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(r.status, 206)
                    self.assertEqual(r.read(), b'234')
                for rel in ['', 'private.txt', '../private.txt', 'f/unknown/private.txt']:
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(server.url(rel))
                    self.assertEqual(error.exception.code, 404)
            finally:
                server.close()

    def test_hls_still_served_without_conditional_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'stream.m3u8').write_text('#EXTM3U\n#EXTINF:1\nseg.ts\n')
            server = MediaServer(root, '127.0.0.1').start()
            server.set_playlist_header('#EXT-X-START:TIME-OFFSET=-8')
            try:
                req = urllib.request.Request(server.url('stream.m3u8'), headers={
                    'If-Modified-Since': 'Wed, 01 Jan 2031 00:00:00 GMT'})
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(r.status, 200)
                    self.assertEqual(r.headers['Cache-Control'], 'no-store')
                    self.assertIn(b'EXT-X-START', r.read())
            finally:
                server.close()


class CleanupTests(unittest.TestCase):
    def test_hls_setup_failure_closes_server_preserves_supplied_directory(self):
        for name, backend in [('chromecast', ChromecastBackend()), ('airplay', AirPlayBackend())]:
            with self.subTest(backend=name), tempfile.TemporaryDirectory() as td:
                sentinel = Path(td) / 'keep.txt'
                sentinel.write_text('keep')
                module = f'linuxcast.backends.{name}'
                server = Mock()
                with patch(f'{module}.local_ip_for', return_value='127.0.0.1'), \
                     patch(f'{module}.MediaServer') as factory, \
                     patch(f'{module}.capture.screen_command', side_effect=RuntimeError('capture unavailable')):
                    factory.return_value.start.return_value = server
                    with self.assertRaisesRegex(RuntimeError, 'capture unavailable'):
                        backend.mirror(Device(name, 'id', 'TV', extra={'paired': True}),
                                       CaptureOptions(workdir=Path(td)))
                server.close.assert_called_once()
                self.assertTrue(sentinel.exists())

    def test_dlna_spawn_failure_closes_server(self):
        with patch('linuxcast.backends.dlna.capture.screen_ts_command', return_value=['ffmpeg']), \
             patch('linuxcast.backends.dlna.local_ip_for', return_value='127.0.0.1'), \
             patch('linuxcast.backends.dlna.MediaServer') as factory, \
             patch('linuxcast.backends.dlna.subprocess.Popen', side_effect=OSError('spawn failed')):
            with self.assertRaises(OSError):
                DlnaBackend().mirror(Device('dlna', 'id', 'TV'), CaptureOptions())
            factory.return_value.start.return_value.close.assert_called_once()

    def test_disconnect_failure_still_cleans_up(self):
        cast = Mock()
        cast.disconnect.side_effect = RuntimeError('disconnect failed')
        cleanup = Mock()
        with self.assertRaises(RuntimeError):
            CastSession(cast, cleanup).close()
        cleanup.assert_called_once()


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'session.json'
        self.patcher = patch.object(session, 'STATE', self.state)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_reused_pid_is_not_current(self):
        session.record(None, 'mirror', 'test', 'casting')
        state = json.loads(self.state.read_text())
        state['identity'] = 'not-the-current-process'
        self.state.write_text(json.dumps(state))
        self.assertIsNone(session.current())

    def test_clear_preserves_other_owner(self):
        self.state.write_text(json.dumps({'pid': os.getpid() + 1}))
        session.clear()
        self.assertTrue(self.state.exists())

    def test_simultaneous_claims_leave_one_owner(self):
        ctx = multiprocessing.get_context('fork')
        ready, claimed = ctx.Event(), ctx.Queue()
        workers = [ctx.Process(target=claim_worker, args=(str(self.state), ready, claimed)) for _ in range(3)]
        try:
            for worker in workers:
                worker.start()
            ready.set()
            for _ in workers:
                claimed.get(timeout=15)
            current = session.current()
            self.assertIsNotNone(current)
            for worker in workers:
                if worker.pid != current['pid']:
                    worker.join(timeout=3)
                    self.assertFalse(worker.is_alive())
            session.stop_current(timeout=2)
            self.assertIsNone(session.current())
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=3)
            claimed.close()


if __name__ == '__main__':
    unittest.main()
