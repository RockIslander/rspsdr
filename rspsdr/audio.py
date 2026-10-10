"""Audio output.

Two interchangeable backends:

``pipe``  writes raw S16_LE mono PCM to ``pacat`` / ``pw-play`` / ``aplay``.
          Works with PipeWire, PulseAudio and bare ALSA, and keeps decoding
          out of the GUI process.
``qt``    uses ``QAudioSink`` so the device can be picked from Qt.

Both are fed from a small bounded ring that the DSP thread writes and a
dedicated writer thread drains, so a slow sound card can never stall the
radio loop -- it just drops the oldest audio instead.
"""

from __future__ import annotations

import collections
import shutil
import subprocess
import threading
import time

import numpy as np

AUDIO_RATE = 48000


def _clip_i16(a: np.ndarray) -> bytes:
    """Float audio -> int16 little-endian bytes, clipped, never modified.

    The clip must not be done with ``out=x``: ``np.asarray`` on an array that
    is already float32 returns *the same object*, so an in-place clip would
    silently overwrite the caller's audio.  It happens to be harmless today
    because the one caller passes a freshly multiplied buffer, which is exactly
    the kind of thing that stops being true after an unrelated edit.
    """
    x = np.clip(np.asarray(a, dtype=np.float32), -1.0, 1.0)
    return (x * 32767.0).astype("<i2").tobytes()


def find_pipe_command(rate: int) -> list[str] | None:
    """Pick the best available CLI player for 48 kHz mono S16_LE."""
    if shutil.which("pacat"):
        return ["pacat", "--playback", "--rate", str(rate), "--channels", "1",
                "--format", "s16le", "--latency-msec", "60", "--client-name",
                "RSPSDR"]
    if shutil.which("pw-play"):
        return ["pw-play", "--rate", str(rate), "--channels", "1",
                "--format", "s16", "--latency", "60ms", "-"]
    if shutil.which("aplay"):
        return ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(rate),
                "-c", "1"]
    return None


class _PipeBackend:
    """Feeds a CLI player; restarts it if it exits."""

    name = "pipe"

    def __init__(self, rate: int, latency_ms: int = 100):
        self.rate = rate
        self.cmd = find_pipe_command(rate)
        if self.cmd is None:
            raise RuntimeError("no audio player found (pacat/pw-play/aplay)")
        self.proc: subprocess.Popen | None = None
        self.restarts = 0
        self._closed = False
        self._spawn()

    def _spawn(self):
        self.proc = subprocess.Popen(
            self.cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, bufsize=0)

    def write(self, data: bytes) -> bool:
        # A write that arrives after close() must not restart the player: the
        # process it would spawn has nobody left to terminate it, so every late
        # write leaked another player.
        if self._closed:
            return False
        if self.proc is None or self.proc.poll() is not None:
            if self.restarts > 20:
                return False
            self.restarts += 1
            time.sleep(0.05)
            self._spawn()
        try:
            self.proc.stdin.write(data)
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False

    def close(self):
        p, self.proc = self.proc, None
        self._closed = True
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
            p.terminate()
            p.wait(timeout=1.0)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def describe(self) -> str:
        return " ".join(self.cmd[:1])


class _QtBackend:
    """Feeds a QAudioSink.

    QAudioSink is event-loop driven, so this backend (and the AudioOutput that
    owns it) must be constructed on the GUI thread.  The DSP thread only ever
    calls :meth:`write`, which is lock-protected.
    """

    name = "qt"

    def __init__(self, rate: int, latency_ms: int = 100):
        from PyQt6.QtCore import QIODevice
        from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

        self._ring = bytearray()
        self._lock = threading.Lock()

        fmt = QAudioFormat()
        fmt.setSampleRate(rate)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)

        dev = QMediaDevices.defaultAudioOutput()
        if dev is None or dev.isNull():
            raise RuntimeError("no default audio output device")
        self.device = dev
        self.sink = QAudioSink(dev, fmt)
        self.sink.setBufferSize(max(int(rate * 2 * latency_ms / 1000), 4096))

        outer = self

        class _Pull(QIODevice):
            def readData(self, maxlen):          # noqa: N802
                return outer.take(int(maxlen))

            def writeData(self, data):           # noqa: N802
                return 0

            def bytesAvailable(self):            # noqa: N802
                with outer._lock:
                    return max(len(outer._ring), 1)

            def isSequential(self):              # noqa: N802
                return True

        self._io = _Pull()
        self._io.open(QIODevice.OpenModeFlag.ReadOnly | QIODevice.OpenModeFlag.Unbuffered)
        self.sink.start(self._io)

    def write(self, data: bytes) -> bool:
        with self._lock:
            self._ring.extend(data)
            limit = max(self.sink.bufferSize() * 3, 16384)
            if len(self._ring) > limit:
                del self._ring[:len(self._ring) - limit]
        return True

    def take(self, nbytes: int) -> bytes:
        with self._lock:
            take = min(nbytes, len(self._ring))
            if take <= 0:
                return b""
            out = bytes(self._ring[:take])
            del self._ring[:take]
            return out

    def close(self):
        try:
            self.sink.stop()
            self._io.close()
        except Exception:
            pass

    def describe(self) -> str:
        return "QAudioSink: " + self.device.description()


class AudioOutput:
    """Thread-safe mono audio sink with a bounded buffer."""

    def __init__(self, rate: int = AUDIO_RATE, backend: str = "auto",
                 latency_ms: int = 100, max_seconds: float = 0.4):
        self.rate = int(rate)
        self.volume = 1.0        # linear gain applied on the way out
        self.muted = False
        self.latency_ms = latency_ms
        self.max_bytes = int(self.rate * 2 * max_seconds)
        self._buf = collections.deque()
        self._nbytes = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self.underruns = 0
        self.overruns = 0
        self.peak = 0.0
        self.backend = self._make_backend(backend)
        self.status = "stopped"
        self._closed = False

    def _make_backend(self, backend: str):
        # `pipe` is tried first for "auto": pacat/pw-play/aplay work identically
        # under PipeWire, PulseAudio and plain ALSA, with no Qt coupling.
        order = {"auto": ["pipe", "qt"], "qt": ["qt"], "pipe": ["pipe"]}[backend]
        errors = []
        for name in order:
            try:
                return _QtBackend(self.rate, self.latency_ms) if name == "qt" \
                    else _PipeBackend(self.rate, self.latency_ms)
            except Exception as exc:                      # pragma: no cover
                errors.append(f"{name}: {exc}")
        raise RuntimeError("no usable audio backend -- " + "; ".join(errors))

    # -- lifecycle -------------------------------------------------
    def start(self):
        if self._running:
            return
        self._running = True
        self.status = "running"
        self._thread = threading.Thread(target=self._loop, name="audio-out",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        t = self._thread
        if t is not None:
            t.join(timeout=1.0)
            self._thread = None
        self.status = "stopped"

    def close(self):
        self.stop()
        self._closed = True
        try:
            self.backend.close()
        except Exception:
            pass

    # -- data ------------------------------------------------------
    def write(self, audio: np.ndarray):
        """Append float32 mono audio (called from the DSP thread)."""
        if audio.size == 0 or self._closed:
            return
        self.peak = float(np.max(np.abs(audio)))
        if self.muted:
            return
        data = _clip_i16(audio * self.volume)
        with self._lock:
            self._buf.append(data)
            self._nbytes += len(data)
            while self._nbytes > self.max_bytes and self._buf:
                self._nbytes -= len(self._buf.popleft())
                self.overruns += 1

    def _loop(self):
        while self._running:
            with self._lock:
                data = self._buf.popleft() if self._buf else b""
                if data:
                    self._nbytes -= len(data)
            if not data:
                self.underruns += 1
                time.sleep(0.005)
                continue
            try:
                self.backend.write(data)
            except Exception:
                time.sleep(0.01)

    def drain(self):
        with self._lock:
            self._buf.clear()
            self._nbytes = 0


def describe_audio() -> str:
    cmd = find_pipe_command(AUDIO_RATE)
    if cmd is None:
        return "no CLI audio player found"
    return " ".join(cmd)
