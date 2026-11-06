"""Playback transport, media ownership, playlist and menu regressions."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from linuxcast import cli, control, session
from linuxcast.base import Device
from linuxcast.backends.chromecast import CastSession


def running_cast():
    status = SimpleNamespace(media_session_id=7, player_state="PLAYING", idle_reason=None,
                             supports_pause=True, supports_seek=True, stream_type="BUFFERED",
                             adjusted_current_time=40, duration=100)
    cast = Mock()
    cast.media_controller.status = status
    active = CastSession(cast, Mock())
    active.bind(status)
    return active, cast.media_controller, status


class PlaybackTests(unittest.TestCase):
    def test_pause_and_resume_use_existing_media(self):
        active, mc, status = running_cast()
        active.control("pause")
        mc.pause.assert_called_once_with()
        status.player_state = "PAUSED"
        active.control("play")
        mc.play.assert_called_once_with()
        mc.play_media.assert_not_called()

    def test_relative_seek_bounds_and_preserves_pause(self):
        active, mc, status = running_cast()
        active.control("seek", -100)
        mc.seek.assert_called_with(0)
        status.player_state = "PAUSED"
        active.control("seek", 200)
        mc.seek.assert_called_with(99.9)
        mc.pause.assert_called_once_with()

    def test_unsupported_live_and_invalid_seeks(self):
        active, mc, status = running_cast()
        for offset in [None, "10", True, float("nan"), float("inf")]:
            with self.assertRaises(RuntimeError):
                active.control("seek", offset)
        status.stream_type = "LIVE"
        with self.assertRaises(RuntimeError):
            active.control("seek", 10)
        status.supports_pause = False
        with self.assertRaises(RuntimeError):
            active.control("pause")
        mc.seek.assert_not_called()

    def test_takeover_never_controls_new_session(self):
        active, mc, status = running_cast()
        status.media_session_id = 8
        for action in ("play", "pause", "seek"):
            with self.assertRaises(RuntimeError):
                active.control(action, 10)
        with self.assertRaises(RuntimeError):
            active.skip()
        mc.pause.assert_not_called()

    def test_skip_exits_wait_and_closes_previous_receiver(self):
        active, _, _ = running_cast()
        active.skip()
        active.wait()
        active.close()
        active.cast.quit_app.assert_called_once()
        active._cleanup.assert_called_once()


class CommandSocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = patch.object(session, "STATE", Path(self.tmp.name) / "session.json")
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_round_trip_error_stale_menu_and_cleanup(self):
        active, mc, _ = running_cast()
        def publish(path, playback):
            session.record(None, "play", "file", "casting", control=path, playback=playback)
        server = control.ControlServer(active, publish, has_next=False).start()
        try:
            self.assertEqual(Path(server.path).parent.stat().st_mode & 0o777, 0o700)
            control.send("pause", expected=server.path)
            mc.pause.assert_called_once()
            with self.assertRaisesRegex(RuntimeError, "No next"):
                control.send("next")
            with self.assertRaisesRegex(RuntimeError, "changed"):
                control.send("play", expected="/old/cast")
            with self.assertRaisesRegex(RuntimeError, "finite"):
                control.send("seek", float("nan"))
        finally:
            server.close()
        self.assertFalse(Path(server.path).exists())

    def test_next_requires_queued_file(self):
        active, _, _ = running_cast()
        def publish(path, playback):
            session.record(None, "play", "file", "casting", control=path, playback=playback)
        server = control.ControlServer(active, publish, has_next=True).start()
        try:
            control.send("next")
            self.assertTrue(active._skip.is_set())
        finally:
            server.close()

    def test_cli_menu_arguments(self):
        with patch.object(control, "send") as send, contextlib.redirect_stdout(io.StringIO()):
            cli.main(["control", "seek", "--notify", "--session", "/cast", "-10"])
        send.assert_called_once_with("seek", -10, "/cast")

    def test_playlist_advances_only_on_finish_or_next(self):
        device = Device("chromecast", "id", "TV")
        for reason, count in [("FINISHED", 2), ("CANCELLED", 1)]:
            first, _, status = running_cast()
            first.wait = Mock()
            status.idle_reason = reason
            second, _, _ = running_cast()
            second.wait = Mock()
            start = Mock(side_effect=[first, second])
            with patch.object(session, "replace_current"), patch.object(cli, "choose_device", return_value=device), \
                 patch.object(control, "ControlServer"), contextlib.redirect_stdout(io.StringIO()):
                cli.run_session(SimpleNamespace(notify=False), "play", "one", start, items=["one", "two"])
            self.assertEqual(start.call_count, count)
            self.assertFalse(session.STATE.exists())

    def test_multiple_sources_keep_order(self):
        backend = Mock()
        def run(args, kind, what, start, items):
            self.assertEqual(items, ["one.mp4", "two.mp4"])
            start(backend, "tv")
            start(backend, "tv")
        with patch.object(cli, "run_session", side_effect=run):
            cli.main(["play", "/tmp/one.mp4", "/tmp/two.mp4", "--notify"])
        self.assertEqual([c.args[1] for c in backend.play.call_args_list], ["/tmp/one.mp4", "/tmp/two.mp4"])


if __name__ == "__main__":
    unittest.main()
