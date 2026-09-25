"""Experimental native AirPlay mirroring through the pinned Doubletake engine.

The engine implements pairing, FairPlay, RTSP, timing and encrypted media. This
adapter owns discovery, capture options, credential prompts and process lifetime.
It never substitutes URL playback for a native mirroring request.
"""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace

from linuxcast import capture
from linuxcast.base import BackendUnavailable, CaptureOptions, RESOLUTIONS, Session, log
from linuxcast.backends.airplay import AirPlayBackend

EVENT_PREFIX = 'LINUXCAST_EVENT '
ENGINE_REVISION = 'ae067228d76df011375164814b729932ed55ca2f'


def engine_path():
    override = os.environ.get('LINUXCAST_AIRPLAY_NATIVE')
    if override:
        return override if os.access(override, os.X_OK) else None
    local = Path.home() / '.local/bin/linuxcast-airplay-native'
    return str(local) if os.access(local, os.X_OK) else shutil.which('linuxcast-airplay-native')


def bitrate_kbps(value):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([kKmM]?)', value)
    if not match:
        raise BackendUnavailable('native AirPlay bitrate must be a number with optional k or M suffix')
    scale = {'': .001, 'k': 1, 'm': 1000}[match[2].lower()]
    result = int(float(match[1]) * scale)
    if result <= 0:
        raise BackendUnavailable('native AirPlay bitrate must be positive')
    return result


def ask_credential(prompt):
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    if not shutil.which('zenity'):
        raise BackendUnavailable('AirPlay needs a PIN/password; run the cast in a terminal or install zenity')
    proc = subprocess.Popen(['zenity', '--entry', '--hide-text', '--title=AirPlay pairing',
                             '--text=' + prompt, '--timeout=300'], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    try:
        out, _ = proc.communicate(timeout=305)
        if proc.returncode:
            raise BackendUnavailable('AirPlay pairing cancelled or timed out')
        return out.strip()
    finally:
        capture.stop_process(proc)


class AirPlayNativeBackend(AirPlayBackend):
    name = 'airplay-native'
    can_play = False

    def discover(self, timeout=5.0):
        return [replace(d, backend=self.name, name=d.name + ' (native AirPlay)')
                for d in super().discover(timeout) if d.extra.get('mirroring')]

    def unsupported_reason(self, device):
        if not device.extra.get('mirroring', True):
            return 'receiver does not advertise screen mirroring'
        if not engine_path():
            return 'install native mirroring with ./install.sh airplay'
        return None

    def _command(self, device):
        reason = self.unsupported_reason(device)
        if reason:
            raise BackendUnavailable(reason)
        binary = engine_path()
        try:
            version = subprocess.run([binary, '-linuxcast-version'], capture_output=True,
                                     text=True, timeout=5, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackendUnavailable('native AirPlay engine is unavailable; rerun ./install.sh airplay') from exc
        if version != f'linuxcast-airplay/1 {ENGINE_REVISION}':
            raise BackendUnavailable('native AirPlay engine version mismatch; rerun ./install.sh airplay')
        credentials = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'linuxcast/native-airplay.json'
        credentials.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return [binary, '-linuxcast-events', '-target', device.host,
                '-port', str(device.extra.get('port', 7000)), '-creds', str(credentials)]

    def mirror(self, device, opts: CaptureOptions):
        command = self._command(device)
        width, height = RESOLUTIONS[opts.resolution]
        command += ['-fps', str(opts.fps), '-bitrate', str(bitrate_kbps(opts.bitrate)),
                    '-hwaccel', 'none' if opts.encoder == 'x264' else opts.encoder,
                    '-video-codec', 'h264', '-output-width', str(width), '-output-height', str(height)]
        if os.environ.get('XDG_SESSION_TYPE') != 'wayland':
            mon = capture.pick_monitor(opts.monitor)
            command += ['-crop-x', str(mon.x), '-crop-y', str(mon.y),
                        '-crop-width', str(mon.width), '-crop-height', str(mon.height)]
        elif opts.monitor:
            raise BackendUnavailable('native AirPlay on Wayland selects the display through the screen-sharing portal')
        if not opts.audio:
            command.append('-no-audio')
        if opts.airplay_latency_ms:
            command += ['-target-latency-ms', str(opts.airplay_latency_ms)]
        log('Starting experimental native AirPlay; HLS buffer setting does not apply')
        return NativeSession.start(command)

    def pair(self, device, ask_pin):
        command = self._command(device) + ['-pair', '-pair-only']
        session = NativeSession.start(command, prompt=lambda _: ask_pin(), ready_event='paired')
        try:
            session.wait()
            if session.last_error:
                raise BackendUnavailable(session.last_error)
        finally:
            session.close()

    def play(self, device, source):
        raise BackendUnavailable('native AirPlay mirrors the screen; use the airplay or dlna backend for files')

    def stop(self, device):
        from linuxcast import session
        state = session.current()
        if state and state['device']['backend'] == self.name and state['device']['id'] == device.id:
            session.stop_current()
            return
        raise BackendUnavailable('no local native AirPlay session for this receiver; use the TV to stop another sender')


class NativeSession(Session):
    def __init__(self, command, prompt=ask_credential):
        self.last_error = None
        self.end_note = None
        self._prompt = prompt
        self._events = queue.Queue()
        self._last_line = ''
        self._closed = False
        # A private group lets teardown also reap GStreamer after a helper error.
        env = os.environ.copy()
        env.pop('DOUBLETAKE_CODE', None)
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     start_new_session=True, env=env)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                line = line.rstrip()
                if line.startswith(EVENT_PREFIX):
                    try:
                        self._events.put(json.loads(line[len(EVENT_PREFIX):]))
                    except ValueError:
                        self._events.put({'type': 'error', 'prompt': 'invalid engine status event'})
                else:
                    self._last_line = line[-2048:]
                    log('native AirPlay: ' + self._last_line)
        finally:
            self._events.put({'type': 'exit'})

    @classmethod
    def start(cls, command, prompt=ask_credential, ready_event='ready', timeout=60):
        session = cls(command, prompt)
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    event = session._events.get(timeout=.2)
                except queue.Empty:
                    continue
                kind = event.get('type')
                if kind == ready_event:
                    return session
                if kind == 'credential':
                    credential = session._prompt(event['prompt'])
                    if not credential or '\n' in credential or '\r' in credential:
                        raise BackendUnavailable('no valid AirPlay credential entered')
                    session.proc.stdin.write(credential + '\n')
                    session.proc.stdin.flush()
                    deadline = time.monotonic() + timeout
                elif kind in ('exit', 'error'):
                    raise BackendUnavailable('native AirPlay setup failed: ' + session._last_line)
            raise BackendUnavailable('native AirPlay setup timed out')
        except BaseException:
            session.close()
            raise

    def wait(self):
        code = self.proc.wait()
        self._reader.join(timeout=2)
        if code:
            self.last_error = f'native AirPlay exited ({code}): {self._last_line}'
        else:
            self.end_note = 'native AirPlay session ended'

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            os.killpg(self.proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()
            self._reader.join(timeout=2)
            self.proc.stdin.close()
            self.proc.stdout.close()
