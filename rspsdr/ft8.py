"""FT8 reception: band presets, slot capture, decoding, and reporting.

FT8 is not a mode you listen to.  It is 8-FSK with 6.25 Hz tone spacing inside
a 50 Hz signal, 79 symbols long, sent in 15-second slots that begin on the UTC
quarter-minute, and what it carries is text: ``CQ W1ABC FN42``,
``W4XYZ W1ABC -07``.  Decoding it means correlating the Costas sync arrays,
extracting 174 soft bits and running LDPC(174,91) -- a code that either comes
out exactly as the standard defines it or comes out as noise.

So this module does not write one.  WSJT-X ships ``jt9``, the decoder the FT8
community actually runs, and RSPSDR drives it the same way it already drives
``pacat`` for sound: a WAV of one slot goes in, decoded messages come out.
Everything here is the part that is specific to *this* program --- knowing
where the FT8 sub-bands are, cutting the receiver's 48 kHz audio down to the
12 kHz jt9 reads, aligning that cut to the UTC slot, and turning jt9's output
into something a window can show and click on.

The three things worth knowing before reading the code:

* **A slot is a time window, not a buffer.**  15.000 s at 12 kHz is exactly
  180000 samples, and the recording has to start at the slot boundary or every
  reported ``dt`` is wrong.  Alignment is done from the audio clock with a
  single calibration against the system clock (see :meth:`FT8Spotter.feed`),
  because the receiver's sample clock is a far better short-term clock than
  anything that samples ``time.time()`` per block.
* **The decoder must not run on the audio thread.**  jt9 takes seconds; the
  DSP thread has to keep the radio live.  A finished slot goes to a worker,
  which decodes it while the next slot is already being received.
* **Nothing is decoded without the decoder.**  When ``jt9`` is not installed
  the mode still works as a receiver (USB, 3 kHz, NR and AGC off -- the
  settings that give a decoder the cleanest signal), it just says so instead of
  pretending.  See :func:`describe`.
"""

from __future__ import annotations

import io
import math
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from scipy.signal import lfilter

from .dsp import AUDIO_RATE as INPUT_RATE
from .dsp import design_lowpass, to_int16

# --- the shape of FT8 ---------------------------------------------------
SLOT_SECONDS = 15.0             # four slots to a minute, on the UTC quarter
AUDIO_RATE = 12000              # what jt9 reads, and what its WAV must be
SLOT_SAMPLES = int(round(SLOT_SECONDS * AUDIO_RATE))        # 180000
FILTER_BW = 3000.0              # the SSB width FT8 mode selects
LOW_HZ = 200.0                  # the audio window handed to jt9.  FT8 lives
HIGH_HZ = 2900.0                # inside a 3 kHz passband and never above it,
                                # so searching to jt9's default 4007 Hz would
                                # only cost time and invite phantom decodes.
DEPTH = 2                       # jt9 -d 1..3: deeper finds weaker signals
                                # and takes proportionally longer.
RECEIVER_MODE = "usb"           # FT8 is received as plain USB -- the mode the
                                # *radio* is told, never "ft8", which no
                                # demodulator here knows about.
DECODE_TIMEOUT = 30.0           # seconds for one slot's decode; a slot is 15 s
QUEUE_DEPTH = 2                 # slots that may wait for the decoder
LATE_SLACK = 0.5                # seconds into a slot that still count as being
                                # on it (see FT8Spotter._slot_boundary)


@dataclass(frozen=True)
class FT8Band:
    """One band's FT8 dial frequency and the note shown beside it."""

    name: str
    dial: float
    note: str = ""


# The standard FT8 working frequencies, as used by WSJT-X: the *dial* (suppressed
# carrier) frequency, with the signal sitting in the audio passband above it.
# The 40 m and 30 m figures are the all-region ones; Region 1 uses 7.071 and
# 10.133.  60 m is a convention rather than a universal allocation -- the note
# says so, because operating there is a national-rules question.
FT8_BANDS: list[FT8Band] = [
    FT8Band("160m", 1_840_000, "night"),
    FT8Band("80m", 3_573_000, "night"),
    FT8Band("60m", 5_357_000, "check local rules"),
    FT8Band("40m", 7_074_000, "day & night"),
    FT8Band("30m", 10_136_000, "CW/data only"),
    FT8Band("20m", 14_074_000, "day"),
    FT8Band("17m", 18_100_000, "day"),
    FT8Band("15m", 21_074_000, "day"),
    FT8Band("12m", 24_915_000, "day"),
    FT8Band("10m", 28_074_000, "day, Es"),
    FT8Band("6m", 50_313_000, "sporadic E"),
    FT8Band("2m", 144_174_000, "local"),
]


def dial_for(name: str) -> float | None:
    """The FT8 dial frequency for a band name, or None."""
    for b in FT8_BANDS:
        if b.name == name:
            return b.dial
    return None


def receiver_mode(mode: str) -> str:
    """The demodulator mode a window mode is received in.

    "FT8" is a mode the *user* selects; it is not a demodulator, and the one
    place it could do damage is being handed to the receiver, which raises on
    a mode it does not know (and would take the window down with it at
    startup, since the settings the radio is built from carry the mode).  So
    the translation lives here, with the rest of what FT8 means, and every
    caller -- the window, the command line, the radio itself -- asks for it
    rather than spelling "usb" out again.
    """
    return RECEIVER_MODE if mode == "ft8" else mode


def next_slot(when: float) -> float:
    """The UTC instant at (or just after) ``when`` at which a slot starts.

    The 1 us of slack is deliberate: arming a few microseconds before a
    boundary should start listening *now*, not sit out another fifteen seconds,
    and a float division of an epoch second lands a hair either side of the
    multiple for reasons that have nothing to do with the clock.
    """
    return math.ceil((when - 1e-6) / SLOT_SECONDS) * SLOT_SECONDS


def slot_label(when: float) -> str:
    """HHMMSS of a slot start, in UTC."""
    return time.strftime("%H%M%S", time.gmtime(when))


def slots_until(when: float) -> float:
    """Seconds from ``when`` to the next slot boundary (0 when on one)."""
    return next_slot(when) - when


@dataclass(frozen=True)
class Decode:
    """One decoded FT8 message."""

    utc: str            # HHMMSS of the slot it was heard in
    snr: int            # dB, jt9's estimate against a 2500 Hz reference
    dt: float           # seconds after the nominal slot start
    audio_hz: int       # where in the passband, above the dial
    message: str
    dial_hz: float = 0.0

    @property
    def freq(self) -> float:
        """Absolute frequency of the signal: the dial plus the audio offset."""
        return self.dial_hz + self.audio_hz

    @property
    def time(self) -> str:
        """HH:MM:SS of the slot, which is how an operator says it."""
        if len(self.utc) != 6:
            return self.utc                        # pragma: no cover - jt9's format
        return f"{self.utc[:2]}:{self.utc[2:4]}:{self.utc[4:]}"

    def line(self) -> str:
        return (f"{self.utc} {self.snr:+3d} {self.dt:+4.1f} "
                f"{self.audio_hz:4d}  {self.message}")


# jt9 prints one line per decoded message, and a good deal else besides:
#
#     143000 -10  0.0 1500 ~  CQ W1ABC FN42
#     <DecodeFinished>   0   1        0
#
# The first field is the slot time, taken from the *file name* -- which is why
# the WAV this module writes is named for its slot.  The message runs to the end
# of the line and may hold a leading '<' (a hashed callsign), a ';' separator
# with a second message, or trailing spaces; only trailing whitespace is
# stripped.
_DECODE_RE = re.compile(
    r"^(?P<utc>\d{6})\s+(?P<snr>[+-]?\d+)\s+(?P<dt>[+-]?\d+(?:\.\d+)?)\s+"
    r"(?P<audio>\d+)\s+~\s+(?P<msg>\S.*?)\s*$")


def parse_decodes(text: str, dial_hz: float = 0.0) -> list[Decode]:
    """Decoded messages from jt9's output; everything else is ignored."""
    out: list[Decode] = []
    for raw in text.splitlines():
        m = _DECODE_RE.match(raw)
        if m is None:
            continue
        try:
            out.append(Decode(utc=m.group("utc"), snr=int(m.group("snr")),
                              dt=float(m.group("dt")),
                              audio_hz=int(m.group("audio")),
                              message=m.group("msg"), dial_hz=dial_hz))
        except (TypeError, ValueError):        # pragma: no cover - regex guards
            continue
    return out


def to_wav_bytes(slot: np.ndarray, peak: float = 0.5) -> bytes:
    """One slot of float audio as a 12 kHz mono 16-bit WAV.

    Normalised to a fixed peak, but with **one gain for the whole slot**: a
    per-block or per-sample normalisation would compress exactly where the
    decoder needs the dynamic range, which is a weak signal next to a strong
    one.  jt9 reads whatever level it is handed --- it estimates its own noise
    floor --- so the only thing that matters is not throwing the low bits away
    on a quiet signal.
    """
    slot = np.asarray(slot, dtype=np.float32)
    if slot.size == 0:
        return b""
    loud = float(np.percentile(np.abs(slot), 99.9))
    gain = 1.0 if loud <= 1e-9 else min(peak / loud, 1e6)
    pcm = to_int16(slot, gain)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(AUDIO_RATE)
        fh.writeframes(pcm)
    return buf.getvalue()


class Decimator:
    """48 kHz audio (the demodulator's rate) to the 12 kHz jt9 reads.

    A Kaiser-windowed low-pass from :func:`dsp.design_lowpass` and then three
    samples dropped in every four -- not the project's ``PolyphaseResampler``,
    which carries its history in complex128 and would warn about discarding an
    imaginary part that was never there, on every block, for a signal that has
    been real since the demodulator.

    The filter is applied over the stream, not the block, and the caller hands
    over whole multiples of the decimation factor, so output sample *n* is the
    same sample however the input is chopped up.  Its group delay -- half the
    filter, about 2 ms -- is not compensated: FT8 tolerates a second either
    way, and it is a constant shift, not a drift.
    """

    def __init__(self, factor: int = 4, rate: int = INPUT_RATE,
                 pass_hi: float = 4000.0, stop_lo: float = 6000.0,
                 atten: float = 70.0):
        self.factor = int(factor)
        self.h = design_lowpass(pass_hi, stop_lo, rate, atten)
        self._zi = np.zeros(len(self.h) - 1, np.float64)

    @property
    def out_rate(self) -> float:
        return INPUT_RATE / self.factor

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return np.empty(0, np.float32)
        y, self._zi = lfilter(self.h, [1.0], np.asarray(x, np.float64),
                             zi=self._zi)
        return y[::self.factor].astype(np.float32)


class FT8Error(RuntimeError):
    """The decoder could not be run, or could not be understood."""


class FT8Decoder:
    """WSJT-X's ``jt9``, run on one slot of audio at a time."""

    def __init__(self, exe: str | None = None, depth: int = DEPTH,
                 timeout: float = DECODE_TIMEOUT, keep_wav: bool = False):
        self.exe = exe if exe is not None else (shutil.which("jt9") or "")
        self.depth = int(depth)
        self.timeout = float(timeout)
        self.keep_wav = bool(keep_wav)
        self.last_wav = None            # for --check / debugging when kept

    @property
    def available(self) -> bool:
        return bool(self.exe) and os.path.exists(self.exe)

    def version(self) -> str:
        """The WSJT-X release the decoder belongs to, if it can be asked."""
        exe = shutil.which("wsjtx_app_version")
        if not exe:
            return ""
        try:
            done = subprocess.run([exe, "--version"], capture_output=True,
                                  text=True, timeout=10.0)
        except Exception:                                   # pragma: no cover
            return ""
        text = (done.stdout or "") + (done.stderr or "")
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
        return m.group(1) if m else ""

    def describe(self) -> str:
        """One line for --check: the decoder, or how to get one."""
        if not self.available:
            return ("MISSING -- FT8 needs WSJT-X's jt9 "
                    "(sudo apt install wsjtx)")
        ver = self.version()
        who = f"jt9 from WSJT-X {ver}" if ver else f"jt9 ({self.exe})"
        return f"{who}, {len(FT8_BANDS)} bands, {SLOT_SECONDS:.0f} s slots"

    def command(self, wav: str) -> list[str]:
        """The jt9 invocation for one slot."""
        return [self.exe, "-8", "-p", str(int(SLOT_SECONDS)),
                "-d", str(self.depth),
                "-L", str(int(LOW_HZ)), "-H", str(int(HIGH_HZ)), wav]

    def decode_wav(self, path: str, dial_hz: float = 0.0) -> list[Decode]:
        """Decode a 12 kHz WAV of one slot.  Raises FT8Error if jt9 fails."""
        if not self.available:
            raise FT8Error("jt9 is not installed (apt install wsjtx)")
        # Run it from, and with its scratch files in, the file's own directory:
        # jt9 writes temporary files next to where it is told to work, and that
        # must not be somebody's home directory.
        workdir = os.path.dirname(os.path.abspath(path)) or "."
        try:
            done = subprocess.run(self.command(path), cwd=workdir,
                                  capture_output=True, text=True,
                                  timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise FT8Error(f"jt9 timed out after {self.timeout:.0f} s") from exc
        except OSError as exc:
            raise FT8Error(f"could not run {self.exe}: {exc}") from exc
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip().splitlines()
            raise FT8Error(f"jt9 exited {done.returncode}"
                           + (f": {detail[-1]}" if detail else ""))
        return parse_decodes(done.stdout, dial_hz)

    def decode(self, slot: np.ndarray, when: datetime,
               dial_hz: float = 0.0) -> list[Decode]:
        """Decode one slot of 12 kHz audio captured at UTC time ``when``.

        The WAV is named for the slot because that is where jt9 takes each
        decode's time from -- the file name, not the file's contents, which is
        also why this could not simply pipe audio into stdin.
        """
        stamp = when.astimezone(timezone.utc).strftime("%y%m%d_%H%M%S")
        with tempfile.TemporaryDirectory(prefix="rspsdr-ft8-") as tmp:
            path = os.path.join(tmp, stamp + ".wav")
            data = to_wav_bytes(slot)
            if not data:
                return []
            with open(path, "wb") as fh:
                fh.write(data)
            decodes = self.decode_wav(path, dial_hz)
            if self.keep_wav:
                keep = os.path.join(tempfile.gettempdir(),
                                    f"rspsdr-ft8-{stamp}.wav")
                shutil.copyfile(path, keep)
                self.last_wav = keep
            return decodes


class FT8Spotter:
    """Cut the receiver's audio into UTC-aligned slots and decode each one.

    ``feed()`` is called from the radio's DSP thread and must not block: it
    decimates 48 kHz to 12 kHz, aligns to the slot boundary, and copies into the
    slot buffer.  A full slot goes to a worker thread, so the decode (seconds)
    happens while the *next* slot is already being received -- which is the only
    way to keep up with a band that transmits every fifteen seconds, for ever.
    """

    def __init__(self, dial_hz: float = FT8_BANDS[0].dial,
                 decoder: FT8Decoder | None = None,
                 on_decodes=None, on_status=None, on_error=None,
                 clock=time.time, queue_depth: int = QUEUE_DEPTH, auto_start: bool = True):
        self.dial_hz = float(dial_hz)
        self.decoder = decoder if decoder is not None else FT8Decoder()
        self.on_decodes = on_decodes
        self.on_status = on_status
        self.on_error = on_error
        self.clock = clock
        # Reentrant on purpose.  The callbacks fired from inside the lock
        # ("listening to slot...", the decode list) are allowed to ask the
        # spotter about itself -- the window's status handler reads stats() to
        # put the counts in the status bar -- and with a plain Lock that is a
        # deadlock: disarm() held the lock while the status callback re-entered
        # stats().  Found by driving a slot through feed() in a test, where the
        # whole program stopped rather than the radio.
        self._lock = threading.RLock()
        self._armed = False
        self._feeding = False
        self._slot_utc = 0.0                    # epoch of the slot being filled
        self._slot_dial = self.dial_hz          # its dial, snapshotted
        self._slot = np.zeros(0, np.float32)
        self._have = 0
        self._pending = np.zeros(0, np.float32)     # 48 kHz remainder
        self._resampler = None
        self._now = 0.0                         # UTC of the next sample to come
        self._last_feed = 0.0                   # when audio last arrived
        self._decoded_slots = 0
        self._dropped_slots = 0
        self._decodes = 0
        self._last_error = ""
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(queue_depth)))
        self._worker = threading.Thread(target=self._work, name="ft8-decode",
                                        daemon=True)
        if auto_start:
            self._worker.start()

    # ------------------------------------------------------------ arming
    @property
    def armed(self) -> bool:
        return self._armed

    def set_dial(self, dial_hz: float):
        self.dial_hz = float(dial_hz)

    def arm(self):
        """Start listening for the next slot."""
        with self._lock:
            self._armed = True
            self._feeding = False
            self._slot_utc = 0.0                # re-align on the next boundary
            self._have = 0
            self._slot = np.zeros(0, np.float32)
            self._pending = np.zeros(0, np.float32)
            self._now = 0.0                     # re-calibrate on the next block
            self._last_feed = 0.0               # no audio has arrived yet
            # A fresh decimator: it carries filter state, and a slot must not
            # begin with the tail of whatever was being received before.
            self._resampler = Decimator()
            self._drain_queue()

    def disarm(self):
        """Stop listening.  A decode already running may still finish."""
        with self._lock:
            self._armed = False
            self._feeding = False
            self._have = 0
            self._last_feed = 0.0
            self._drain_queue()
            self._emit_status()

    def _drain_queue(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    # ------------------------------------------------------------ audio in
    def feed(self, audio: np.ndarray):
        """Take demodulated audio at 48 kHz.  Called from the DSP thread.

        Nothing that happens in here may escape: this is called from the
        radio's DSP loop, where an exception would take the whole receiver --
        sound, spectrum and all -- down over an optional extra.  Anything the
        decoder path can get wrong (a malformed block, a filter state gone
        bad) is caught, reported once, and survived; the next slot is fifteen
        seconds away.
        """
        if not self._armed or audio is None or len(audio) == 0:
            return
        try:
            self._feed(audio)
        except Exception as exc:                            # pragma: no cover
            self._fail(f"{type(exc).__name__}: {exc}")

    def _fail(self, msg: str):
        """Record and report a failure, but only when it is a new one."""
        with self._lock:
            changed = msg != self._last_error
            self._last_error = msg
        if changed and self.on_error is not None:
            self.on_error(msg)

    def _feed(self, audio: np.ndarray):
        with self._lock:
            self._last_feed = self.clock()
            if not self._armed:
                return
            if self._pending.size:
                self._pending = np.concatenate((self._pending,
                                                np.asarray(audio, np.float32)))
            else:
                self._pending = np.asarray(audio, np.float32).copy()
            step = self._resampler.factor
            take = (self._pending.size // step) * step
            if take == 0:
                return
            block = self._pending[:take]
            self._pending = self._pending[take:]
            out = self._resampler.process(block)
            if out.size:
                self._collect(np.asarray(out, np.float32))

    def _collect(self, a12: np.ndarray):
        """Place a block of 12 kHz audio into the slot grid.  Lock held."""
        if self._now == 0.0:
            # One calibration against the system clock, and from then on the
            # audio itself is the clock.  The newest sample in this block was
            # taken *now*, so the block starts a block's worth earlier.
            self._now = self.clock() - a12.size / AUDIO_RATE
        pos = 0
        while pos < a12.size:
            if not self._feeding:
                # Which fifteen seconds this sample belongs to.  All of the
                # timing correctness is decided in there.
                target = self._slot_boundary()
                # A drop of zero or less is not a mistake: it means the audio
                # has already reached (or is a hair past) the boundary, so the
                # slot starts at the sample in hand, stamped with the boundary
                # it is inside -- which is what it is.
                drop = int(math.ceil((target - self._now) * AUDIO_RATE - 1e-6))
                if drop > 0:
                    skip = min(drop, a12.size - pos)
                    pos += skip
                    self._now += skip / AUDIO_RATE
                    continue
                self._feeding = True
                self._slot_utc = target
                self._slot_dial = self.dial_hz
                self._slot = np.empty(SLOT_SAMPLES, np.float32)
                self._have = 0
                self._emit_status(listening=True)
            take = min(SLOT_SAMPLES - self._have, a12.size - pos)
            self._slot[self._have:self._have + take] = a12[pos:pos + take]
            self._have += take
            pos += take
            self._now += take / AUDIO_RATE
            if self._have >= SLOT_SAMPLES:
                self._finish_slot()

    def _slot_boundary(self) -> float:
        """Which fifteen seconds the audio in hand belongs to.  Lock held.

        Two mistakes are possible here and both are silent on the air, so this
        is the one place worth being careful about:

        * Taking the *next* boundary when the audio is already inside a slot
          throws that slot away.  Worse, a slot is 180000 additions of
          1/12000 s and drifts by microseconds, so the audio is always a hair
          *past* each boundary -- and a hair past makes ``next_slot()`` answer
          with the boundary fifteen seconds later, every time.  Measured on a
          real RSP1: zero slots captured in seventy-five seconds, while the
          window said it was listening to every one.
        * Taking the *last* boundary when a slot has been joined late hands jt9
          fifteen seconds that begin in the middle of a slot, stamped as if
          they began at its start: a decode of nothing, reported as one.

        So: the boundary of the slot now is in, unless that slot is already too
        old to start -- and once a slot has been captured the grid advances a
        whole slot at a time rather than being re-derived, so the drift above
        can never add up to a lost slot.
        """
        if self._slot_utc:
            nxt = self._slot_utc + SLOT_SECONDS
            if self._now < nxt + LATE_SLACK:
                return nxt
        # The floor rather than the ceiling: the boundary this sample is inside
        # of.  The 1 us of slack is for a clock that lands exactly on one.
        started = math.floor((self._now + 1e-6) / SLOT_SECONDS) * SLOT_SECONDS
        if self._now - started > LATE_SLACK:
            return next_slot(self._now)
        return started

    def _finish_slot(self):
        """A slot is full: hand it to the worker and start the next one."""
        slot, when, dial = self._slot, self._slot_utc, self._slot_dial
        self._feeding = False
        self._have = 0
        self._slot = np.zeros(0, np.float32)
        try:
            self._queue.put_nowait((slot, when, dial))
        except queue.Full:
            # The decoder is behind by more than a slot and a half.  Dropping
            # the *newest* is the same bargain the radio makes with RF: stay in
            # the present rather than fall further behind.
            self._dropped_slots += 1
            self._emit_status(dropped=True)

    # ------------------------------------------------------------ audio out
    def idle_seconds(self) -> float:
        """Seconds since audio last arrived.

        Infinite when none has arrived at all since arming -- which is not the
        same thing as "zero seconds ago", and is what makes a window able to
        tell "waiting for the receiver" from "the next slot is in 3 s".
        """
        with self._lock:
            if self._last_feed == 0.0:
                return float("inf")
            return max(self.clock() - self._last_feed, 0.0)

    def progress(self) -> tuple[bool, float]:
        """(filling a slot, seconds into it) -- for the window's readout."""
        with self._lock:
            if not self._feeding:
                return (False, 0.0)
            return (True, self._have / AUDIO_RATE)

    def slot_start(self) -> float:
        """Epoch of the slot being filled, or 0.0 when none is."""
        with self._lock:
            return self._slot_utc if self._feeding else 0.0

    def stats(self) -> dict:
        with self._lock:
            return {"slots": self._decoded_slots, "decodes": self._decodes,
                    "dropped": self._dropped_slots,
                    "queued": self._queue.qsize(),
                    "error": self._last_error}

    def stop(self):
        """Shut the worker down.  Only for a clean exit."""
        self.disarm()
        try:
            self._queue.put_nowait(None)
        except queue.Full:                                  # pragma: no cover
            pass
        self._worker.join(timeout=2.0)

    # ------------------------------------------------------------ worker
    def run_pending(self) -> int:
        """Decode every queued slot on this thread, and say how many there were.

        The worker thread does this in the program; a test, or a command-line
        run that wants its decodes before it exits, can ask for the same work
        synchronously rather than waiting on a timer.
        """
        done = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return done
            self._decode_item(item)
            done += 1

    def _decode_item(self, item):
        slot, when, dial = item
        if not self.decoder.available:
            # Nothing is decoded without the decoder -- so say what is missing
            # once, rather than letting every slot raise into the same report.
            # The mode still works as a receiver (see the module docstring).
            if not self._last_error:
                self._last_error = self.decoder.describe()
                if self.on_error is not None:
                    self.on_error(self._last_error)
            return
        stamp = datetime.fromtimestamp(when, tz=timezone.utc)
        try:
            decodes = self.decoder.decode(slot, stamp, dial)
        except FT8Error as exc:
            self._last_error = str(exc)
            if self.on_error is not None:
                self.on_error(str(exc))
            return
        except Exception as exc:                            # pragma: no cover
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self.on_error is not None:
                self.on_error(self._last_error)
            return
        with self._lock:
            self._decoded_slots += 1
            self._decodes += len(decodes)
            still_armed = self._armed
        # A slot that finished before the mode was switched off is not
        # reported: the window is showing something else by now.
        if not still_armed:
            return
        if self.on_decodes is not None:
            self.on_decodes(decodes, stamp)
        self._emit_status()

    def _work(self):
        while True:
            item = self._queue.get()
            if item is None:                                # pragma: no cover
                return
            self._decode_item(item)

    # ------------------------------------------------------------ reporting
    def _emit_status(self, listening: bool = False, dropped: bool = False):
        if self.on_status is None:
            return
        if dropped:
            self.on_status("decoder behind -- slot dropped")
            return
        if listening:
            self.on_status(f"listening to slot {slot_label(self._slot_utc)}")
            return
        st = self.stats()
        if not self._armed:
            self.on_status("FT8 idle")
            return
        self.on_status(f"{st['slots']} slot(s) decoded, "
                       f"{st['decodes']} message(s)"
                       + (f", {st['dropped']} dropped" if st["dropped"] else ""))


def describe() -> str:
    """How the decoder looks right now, for --check."""
    return FT8Decoder().describe()
