"""RSP1 device control and the streaming pipeline.

Threading model
---------------
``reader``  owns the SoapySDR device.  It performs every control call
            (retune, gain) so no other thread ever touches the handle, and
            pushes blocks of complex samples into a bounded deque.
``dsp``     pops those blocks, runs the DDC + demodulator one frame at a time
            and hands audio to the :class:`AudioOutput`.  It also computes the
            spectrum that drives the GUI.

The deque is deliberately bounded: if the DSP cannot keep up we drop the
oldest RF instead of accumulating latency forever.
"""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass, replace

import numpy as np

from . import dsp as dspmod
from .audio import AudioOutput
from .dsp import AUDIO_RATE, Demodulator, Downconverter, Spectrum

SOAPY_SDR_RX = 0
try:  # pragma: no cover - hardware import
    import SoapySDR
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX as _RX
    _HAVE_SOAPY = True
except Exception:  # pragma: no cover
    SoapySDR = None
    SOAPY_SDR_CF32 = None
    _RX = 0
    _HAVE_SOAPY = False

RX = _RX

# Tuner bandwidths the RSP1 actually offers (probed from the hardware).
TUNER_BANDWIDTHS = [200_000, 300_000, 600_000, 1_536_000,
                    5_000_000, 6_000_000, 7_000_000, 8_000_000]

# How much RF we read per call, and how long a backlog we tolerate before we
# start throwing away the *oldest* samples.  Dropping old data keeps the radio
# live (bounded latency) when the machine is too busy; buffering half a second
# absorbs short bursts of contention without dropping anything at all.
READ_BLOCK = 32768
BACKLOG_SECONDS = 0.8

# How many FFT segments of the current hop may be transformed per frame.  This
# is the ceiling on what the spectrum costs, and it has to be a ceiling rather
# than a fixed number: at 62.5 kSps one hop is 2500 IQ samples, which is less
# than a single 4096-point FFT, and at 2 MSps a hop holds nineteen of them.
SPECTRUM_SEGMENTS = 16

@dataclass
class Settings:
    """Everything the user can change.  Cheap to copy, freely shared."""

    device_args: str = "driver=sdrplay"
    sample_rate: int = 2_000_000
    center_freq: float = 7_100_000.0
    tuned_freq: float = 7_100_000.0
    mode: str = "lsb"
    filter_bw: float = 2_400.0
    cw_pitch: float = 700.0
    rf_agc: bool = False
    if_gain: float = 40.0          # IFGR, 20..59 dB
    rf_gain: int = 0               # RFGR, 0..3
    tuner_bw: int = 0              # 0 = derive from sample rate
    audio_agc: bool = True
    nr: bool = False               # spectral-subtraction noise reduction
    nr_level: float = 50.0         # 0..100, how hard to work
    volume: float = 0.6
    squelch_db: float = -200.0
    n_fft: int = 4096
    spec_avg: float = 0.55
    audio_backend: str = "auto"

    def copy_with(self, **kw) -> "Settings":
        return replace(self, **kw)

    def effective_tuner_bw(self) -> int:
        if self.tuner_bw:
            return int(self.tuner_bw)
        best = TUNER_BANDWIDTHS[0]
        for bw in TUNER_BANDWIDTHS:
            if bw <= self.sample_rate:
                best = bw
        return int(best)


@dataclass
class Status:
    running: bool = False
    sample_rate: int = 0
    center: float = 0.0
    tuned: float = 0.0
    actual_rate: float = 0.0
    rx_dbm: float = -200.0
    audio_db: float = -200.0
    cpu_load: float = 0.0        # CPU the DSP genuinely needs, / realtime
    lag: float = 0.0             # seconds behind realtime (0 when keeping up)
    dropped_chunks: int = 0
    audio_desc: str = ""
    message: str = ""
    error: str = ""


def list_devices() -> list[dict]:
    """Enumerate SoapySDR devices as plain dicts."""
    if not _HAVE_SOAPY:
        return []
    out = []
    for i, found in enumerate(SoapySDR.Device.enumerate()):
        # SoapySDRKwargs is a SWIG map: index it directly, it has no .get().
        d = {key: found[key] for key in found.keys()}
        d.setdefault("driver", "?")
        args = ",".join(f"{k}={v}" for k, v in d.items())
        out.append({"index": i, "args": args,
                    "label": d.get("label", d.get("driver", "device")),
                    **d})
    return out


class Radio:
    """Owns the SDR device and the two worker threads.

    Deliberately free of Qt: the GUI attaches to :attr:`on_spectrum` and
    :attr:`on_status` callbacks, and a headless console tool can use the same
    object to check the radio without opening a window.
    """

    def __init__(self, settings: Settings | None = None,
                 on_spectrum=None, on_status=None, on_error=None):
        self.settings = settings or Settings()
        self.on_spectrum = on_spectrum
        self.on_status = on_status
        self.on_error = on_error
        self.status = Status()

        self._dev = None
        self._stream = None
        self._chunks: collections.deque = collections.deque(maxlen=8)
        self._chunk_bytes = 0
        self._lock = threading.Lock()
        self._running = False
        self._reader: threading.Thread | None = None
        self._dsp: threading.Thread | None = None

        self._pending_center: float | None = None
        self._pending_gain: Settings | None = None
        self._actual_center = self.settings.center_freq
        self._last_status = 0.0

        self.spectrum = Spectrum(self.settings.n_fft, self.settings.spec_avg)
        self.ddc: Downconverter | None = None
        self.demod: Demodulator | None = None
        self.audio: AudioOutput | None = None
        self._pending: np.ndarray | None = None
        self._spec_buf = np.empty(0, dtype=np.complex64)
        self._squelch_reported = None

    # ---------------------------------------------------------- device
    @property
    def opened(self) -> bool:
        return self._dev is not None

    def open(self):
        """Open the device and start streaming.  Idempotent."""
        if self._running:
            return
        if not _HAVE_SOAPY:
            raise RuntimeError("SoapySDR Python bindings are not available "
                               "(install python3-soapysdr)")

        s = self.settings
        # NB: this build of the bindings only accepts a *string* kwargs blob;
        # passing a dict raises "SoapySDR::Device::make() no match".
        self._dev = SoapySDR.Device(s.device_args)
        self._dev.setSampleRate(RX, 0, s.sample_rate)
        self._dev.setFrequency(RX, 0, s.center_freq)
        try:
            self._dev.setAntenna(RX, 0, "RX")
        except Exception:
            pass
        self._apply_gain(s)
        try:
            self._dev.setBandwidth(RX, 0, s.effective_tuner_bw())
        except Exception:
            pass
        try:
            self._dev.setDCOffsetMode(RX, 0, True)
        except Exception:
            pass

        self._stream = self._dev.setupStream(RX, SOAPY_SDR_CF32)
        self._dev.activateStream(self._stream)
        # Drain whatever the device buffered before we asked for data.  It can
        # be holding about 100 ms (measured), so reading a fixed number of small
        # blocks leaves most of it to arrive in one burst the moment streaming
        # starts -- precisely the backlog a small queue cannot absorb.  Read
        # until the device stops returning full blocks, or we run out of
        # patience; a partial block means it has nothing more queued.
        warm = np.zeros(READ_BLOCK, np.complex64)
        for _ in range(32):
            sr = self._dev.readStream(self._stream, [warm], warm.size,
                                      timeoutUs=50_000)
            if sr.ret <= 0 or sr.ret < warm.size:
                break

        depth = int(BACKLOG_SECONDS * s.sample_rate / READ_BLOCK) + 1
        self._chunks = collections.deque(maxlen=max(8, min(48, depth)))
        self._chunk_bytes = READ_BLOCK * 8

        self._build_dsp()
        self.audio = AudioOutput(AUDIO_RATE, backend=s.audio_backend)
        self.audio.start()

        self._running = True
        self.status = Status(
            running=True, sample_rate=s.sample_rate, center=s.center_freq,
            tuned=s.tuned_freq, actual_rate=self._dev.getSampleRate(RX, 0),
            audio_desc=self.audio.backend.describe(), message="streaming")
        self._reader = threading.Thread(target=self._reader_loop, name="sdr-reader",
                                        daemon=True)
        self._dsp = threading.Thread(target=self._dsp_loop, name="sdr-dsp",
                                     daemon=True)
        # Start the consumer first: it is then already draining when the first
        # samples land, instead of the reader filling the backlog while the
        # DSP thread is still being created.
        self._dsp.start()
        self._reader.start()
        self._emit_status()

    def _apply_gain(self, s: Settings):
        try:
            self._dev.setGainMode(RX, 0, bool(s.rf_agc))
            if not s.rf_agc:
                self._dev.setGain(RX, 0, "IFGR", float(s.if_gain))
            self._dev.setGain(RX, 0, "RFGR", float(s.rf_gain))
        except Exception as exc:                        # pragma: no cover
            self._error(f"gain: {exc}")

    def close(self):
        self._running = False
        for t in (self._reader, self._dsp):
            if t is not None:
                t.join(timeout=2.0)
        self._reader = self._dsp = None
        if self.audio is not None:
            self.audio.close()
            self.audio = None
        if self._dev is not None:
            try:
                if self._stream is not None:
                    self._dev.deactivateStream(self._stream)
                    self._dev.closeStream(self._stream)
            except Exception:
                pass
            try:
                self._dev.close()
            except Exception:
                pass
        self._dev = None
        self._stream = None
        with self._lock:
            self._chunks.clear()
        self._pending = None
        self.status.running = False
        self.status.message = "stopped"
        self._emit_status()

    # ------------------------------------------------------- settings
    def _build_dsp(self):
        s = self.settings
        self.ddc = Downconverter(float(s.sample_rate))
        self.demod = Demodulator(s.mode, s.filter_bw, s.cw_pitch,
                                 agc=s.audio_agc, squelch_db=s.squelch_db,
                                 nr=s.nr, nr_level=s.nr_level / 100.0)
        self.spectrum = Spectrum(s.n_fft, s.spec_avg)
        self._pending = np.zeros(0, dtype=np.complex64)
        self._spec_buf = np.empty(0, dtype=np.complex64)

    def apply(self, **changes):
        """Update settings live.  Sample-rate changes need a restart."""
        s = self.settings.copy_with(**changes)
        restart = s.sample_rate != self.settings.sample_rate or \
            s.device_args != self.settings.device_args
        self.settings = s
        if restart:
            if self._running:
                self.close()
                self.open()
            return
        if s.n_fft != getattr(self.spectrum, "n_fft", None) or \
                abs(s.spec_avg - getattr(self.spectrum, "avg", -1)) > 1e-9:
            self.spectrum = Spectrum(s.n_fft, s.spec_avg)
        if self.demod is not None:
            self.demod.configure(s.mode, s.filter_bw, s.cw_pitch)
            self.demod.agc.enabled = s.audio_agc
            self.demod.squelch_db = s.squelch_db
            # Plain attribute writes: the DSP thread reads these once per
            # audio block, and both are single scalars, so a torn read is not
            # possible under CPython.
            self.demod.nr.enabled = bool(s.nr)
            self.demod.nr.strength = min(max(s.nr_level / 100.0, 0.0), 1.0)
        if self.audio is not None:
            self.audio.volume = s.volume
        if self._running:
            with self._lock:
                self._pending_gain = s

    def set_center(self, freq: float):
        freq = max(10_000.0, min(float(freq), 2_000_000_000.0))
        self.settings = self.settings.copy_with(center_freq=freq)
        self._pending_center = freq

    def set_tuned(self, freq: float):
        freq = max(10_000.0, min(float(freq), 2_000_000_000.0))
        self.settings = self.settings.copy_with(tuned_freq=freq)

    @property
    def span(self) -> float:
        return float(self.settings.sample_rate)

    def recenter_on_tuned(self):
        self.set_center(self.settings.tuned_freq)

    # ---------------------------------------------------------- loops
    def _reader_loop(self):
        buf = np.empty(READ_BLOCK, dtype=np.complex64)
        while self._running:
            with self._lock:
                want_center = self._pending_center
                self._pending_center = None
                gain = self._pending_gain
                self._pending_gain = None
            if want_center is not None:
                try:
                    self._dev.setFrequency(RX, 0, want_center)
                    self._actual_center = want_center
                except Exception as exc:
                    self._error(f"retune: {exc}")
            if gain is not None:
                self._apply_gain(gain)
            try:
                sr = self._dev.readStream(self._stream, [buf], buf.size,
                                          timeoutUs=500_000)
            except Exception as exc:
                self._error(f"readStream: {exc}")
                time.sleep(0.1)
                continue
            if sr.ret > 0:
                chunk = buf[:sr.ret].copy()
                with self._lock:
                    if len(self._chunks) == self._chunks.maxlen:
                        self.status.dropped_chunks += 1
                    self._chunks.append(chunk)
            elif sr.ret == -4:            # SOAPY_SDR_OVERFLOW: just continue
                continue

    def _dsp_loop(self):
        hop = self.ddc.hop_in
        load = 0.0
        while self._running:
            with self._lock:
                chunks = list(self._chunks)
                self._chunks.clear()
            for chunk in chunks:
                if self._pending.size:
                    self._pending = np.concatenate((self._pending, chunk))
                else:
                    self._pending = chunk

            rate = float(self.settings.sample_rate)
            frame_seconds = hop / rate
            processed = False
            while self._pending is not None and self._pending.size >= hop:
                frame = self._pending[:hop]
                self._pending = self._pending[hop:]
                # thread_time() is CPU actually burned by this thread.  Wall
                # clock would also count time we were merely descheduled by a
                # busy machine, which is not a property of this code.
                t0 = time.thread_time()
                self._process_frame(frame)
                # EMA of CPU seconds needed per frame, expressed as a fraction
                # of the one core of realtime we have to fit inside.
                load = 0.9 * load + 0.1 * (time.thread_time() - t0) / frame_seconds
                processed = True
            if processed:
                self.status.cpu_load = load
                self.status.lag = max(self._pending.size / rate, 0.0)
                now = time.monotonic()
                if now - self._last_status > 0.4:
                    self._last_status = now
                    self.status.center = self._actual_center
                    self.status.tuned = self.settings.tuned_freq
                    self._emit_status()
            else:
                time.sleep(0.002)

    def _spectrum_block(self, frame: np.ndarray) -> np.ndarray | None:
        """Up to SPECTRUM_SEGMENTS whole FFT segments of the newest IQ.

        The segments cannot simply be cut out of the frame in hand.  A hop is
        sized for the demodulator, not for the FFT, and at 62.5 kSps it holds
        only 2500 IQ samples -- fewer than one 4096-point FFT.  Requiring the
        transform to fit inside a single hop meant ``nseg`` was always zero
        there, no frame was ever handed to the spectrum, and both the spectrum
        and the waterfall stayed blank at 62.5 and 96 kSps (and at 125, 192,
        250 and 384 kSps once the FFT was set to 8192 or more).

        Carrying the remainder into the next call lets the FFT be assembled
        from as many hops as it needs, while the cost per hop stays bounded by
        SPECTRUM_SEGMENTS however high the sample rate goes.
        """
        n_fft = self.settings.n_fft
        buf = frame if not self._spec_buf.size else \
            np.concatenate((self._spec_buf, frame))
        nseg = buf.size // n_fft
        if nseg < 1:
            self._spec_buf = buf
            return None
        if nseg > SPECTRUM_SEGMENTS:
            # More IQ than we are willing to transform in one go -- keep the
            # newest and throw the rest away rather than fall behind.  Dropping
            # old RF is the same bargain the reader thread makes.
            start = (nseg - SPECTRUM_SEGMENTS) * n_fft
            self._spec_buf = buf[start + SPECTRUM_SEGMENTS * n_fft:]
            return buf[start:]
        self._spec_buf = buf[nseg * n_fft:]
        return buf[:nseg * n_fft]

    def _process_frame(self, frame: np.ndarray):
        s = self.settings
        center = self._actual_center

        # --- spectrum -----------------------------------------------------
        seg = self._spectrum_block(frame)
        if seg is not None:
            db = self.spectrum(seg)
            if db is not None and self.on_spectrum is not None:
                self.on_spectrum(center, db, s.sample_rate)

        # --- demodulation -------------------------------------------------
        offset = s.tuned_freq - center + self.demod.mix_shift
        try:
            bb = self.ddc.process(frame, offset)
        except Exception as exc:                        # pragma: no cover
            self._error(f"ddc: {exc}")
            return
        if bb.size == 0:
            return
        audio = self.demod(bb)
        self.status.rx_dbm = self.demod.signal_db
        self.status.audio_db = dspmod.audio_rms_db(audio)
        if self.audio is not None:
            self.audio.write(audio)

        # --- status -------------------------------------------------------
        if self.demod.open != self._squelch_reported:
            self._squelch_reported = self.demod.open
            self._emit_status()

    def _emit_status(self):
        if self.on_status is not None:
            self.on_status(self.status)

    def _error(self, msg: str):
        self.status.error = msg
        if self.on_error is not None:
            self.on_error(msg)
