"""DSP core for RSPSDR.

Signal chain
------------
    IQ @ Fs  ->  DDC (mix + cascaded resample)  ->  complex @ 48 kHz
             ->  mode filter  ->  demodulator  ->  real audio @ 48 kHz

Everything here is *stateful*: the classes are fed consecutive blocks of
samples and produce a continuous output stream with no block-boundary clicks.

`PolyphaseResampler` is the workhorse.  It performs rational resampling
``up``/``down`` with exact continuity across calls; the caller must feed hops
that are multiples of ``down`` (the chain planner below guarantees this).
"""

from __future__ import annotations

import math

import numpy as np
from scipy.signal import butter, firwin, lfilter, sosfilt, upfirdn

AUDIO_RATE = 48000

# ---------------------------------------------------------------- helpers


def _kaiser_beta(atten: float) -> float:
    if atten > 50:
        return 0.1102 * (atten - 8.7)
    if atten > 21:
        return 0.5842 * (atten - 21) ** 0.4 + 0.07886 * (atten - 21)
    return 0.0


def design_lowpass(pass_hi: float, stop_lo: float, rate: float, atten: float = 70.0):
    """Kaiser-windowed low-pass.  ``pass_hi``/``stop_lo`` in Hz, ``rate`` = Fs."""
    nyq = rate / 2.0
    pass_hi = min(pass_hi, 0.95 * nyq)
    stop_lo = min(max(stop_lo, pass_hi * 1.02 + 1.0), nyq)
    dw = 2.0 * math.pi * (stop_lo - pass_hi) / rate
    numtaps = int(math.ceil((atten - 8.0) / (2.285 * dw)))
    numtaps = max(numtaps, 8) | 1  # odd -> symmetric
    cutoff = 0.5 * (pass_hi + stop_lo) / nyq
    return firwin(numtaps, cutoff, window=("kaiser", _kaiser_beta(atten)))


class PolyphaseResampler:
    """Continuous-state rational resampler (``up``/``down``).

    Output sample ``n`` of the whole stream is produced exactly once.  Feed
    blocks whose length is a multiple of ``down`` (see :func:`plan_chain`).
    """

    def __init__(self, up, down, in_rate, out_rate, pass_hi, atten=70.0, dtype=np.complex64):
        up, down = int(up), int(down)
        if up < 1 or down < 1:
            raise ValueError("up/down must be >= 1")
        g = math.gcd(up, down)
        self.up, self.down = up // g, down // g
        self.in_rate = float(in_rate)
        self.out_rate = self.in_rate * self.up / self.down
        self.dtype = dtype is np.complex64 and np.complex64 or dtype

        # Filter runs in the *interpolated* domain, so its rate is in_rate*up.
        filt_rate = self.in_rate * self.up
        stop_lo = min(filt_rate / 2.0, self.out_rate / 2.0)
        h = design_lowpass(pass_hi, stop_lo, filt_rate, atten)
        # Compensate the gain lost by inserting (up-1) zeros between samples.
        self.h = (h * self.up).astype(np.float64)

        lmin = int(math.ceil((len(self.h) - 1) / self.up))
        # History must be a multiple of `down` so the output grid stays aligned.
        self.hist_len = int(math.ceil(lmin / self.down) * self.down)
        self._hist = np.zeros(self.hist_len, dtype=np.complex128)
        self._t = 0          # absolute index of the next input sample
        self._n_next = 0     # absolute index of the next output sample

    @property
    def latency_samples(self) -> int:
        return int(len(self.h) // (2 * self.up))

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return np.empty(0, dtype=self.dtype)
        xs = np.concatenate((self._hist, x)) if self.hist_len else x
        xs_start = self._t - self.hist_len

        # xs_start is a multiple of `down`, so this division is exact.
        n_off = (xs_start // self.down) * self.up
        y = upfirdn(self.h, xs, self.up, self.down)

        m0 = self._n_next - n_off
        n_last = ((self._t + x.size - 1) * self.up) // self.down
        m1 = min(n_last - n_off, y.size - 1)

        if m1 < m0:
            out = y[:0]
            self._n_next = n_off + m1 + 1  # may lag; kept monotone below
            self._n_next = max(self._n_next, 0)
        else:
            out = y[max(m0, 0):m1 + 1]
            self._n_next = n_off + m1 + 1

        self._t += x.size
        if self.hist_len:
            self._hist = xs[-self.hist_len:]
        return out.astype(self.dtype, copy=False)


# ---------------------------------------------------------------- chain


def plan_chain(fs: float, target: int = AUDIO_RATE, max_f: int = 16):
    """Split Fs -> target into integer stages plus one rational stage.

    Returns ``(stages, hop_in, hop_out)`` where ``stages`` is a list of
    ``(up, down)`` and ``hop_in`` is the block size the chain expects.
    """
    fs = int(round(fs))
    r = fs
    stages = []
    while r > target * max_f:
        for f in (16, 15, 12, 10, 9, 8, 6, 5, 4, 3, 2):
            if r % f == 0 and r // f >= target:
                stages.append((1, f))
                r //= f
                break
        else:  # pragma: no cover - all RSP1 rates factor into 2,3,5
            break
    g = math.gcd(r, int(target))
    stages.append((int(target) // g, r // g))

    up_last, down_last = stages[-1]
    # ~40 ms of audio per block.
    k = max(1, int(round(0.040 * target / up_last)))
    hop = k * down_last
    for up, down in reversed(stages[:-1]):
        hop = hop * down // up
    return stages, int(hop), int(k * up_last)


class Downconverter:
    """Mix a tuned offset to baseband and resample to ``AUDIO_RATE``."""

    def __init__(self, fs: float, pass_hi: float = 20000.0, atten: float = 70.0,
                 target: int = AUDIO_RATE, peak_budget: float = 0.0005):
        self.fs = float(fs)
        self.stages, self.hop_in, self.hop_out = plan_chain(fs, target)
        self.resamplers = []
        rate = self.fs
        for up, down in self.stages:
            nxt = rate * up / down
            self.resamplers.append(
                PolyphaseResampler(up, down, rate, nxt, pass_hi, atten))
            rate = nxt
        self.out_rate = rate
        self.nco_phase = 0.0
        self._buf = np.zeros(0, dtype=np.complex64)
        # Worst-case CPU is dominated by the first FIR; expose it for the UI.
        self.taps = sum(len(r.h) for r in self.resamplers)
        self.peak_budget = peak_budget
        self.open_loop_offset = 0.0

    # -- mixing -----------------------------------------------------
    def _mix(self, x: np.ndarray, offset: float) -> np.ndarray:
        if abs(offset) < 1e-9:
            self.nco_phase = (self.nco_phase + 2 * math.pi * offset * x.size / self.fs) % (2 * math.pi)
            return x
        n = x.size
        inc = -2.0 * math.pi * offset / self.fs
        ph = self.nco_phase + inc * np.arange(n, dtype=np.float64)
        self.nco_phase = float((ph[-1] + inc) % (2 * math.pi))
        lo = np.exp(1j * ph).astype(np.complex64)
        return x * lo

    def push(self, iq: np.ndarray):
        """Feed IQ; returns whatever baseband audio-rate IQ is ready."""
        if self._buf.size:
            self._buf = np.concatenate((self._buf, iq))
        else:
            self._buf = iq
        blocks = []
        while self._buf.size >= self.hop_in:
            chunk = self._buf[:self.hop_in]
            self._buf = self._buf[self.hop_in:]
            blocks.append(chunk)
        return blocks

    def process(self, chunk: np.ndarray, offset: float) -> np.ndarray:
        x = self._mix(chunk, offset)
        for res in self.resamplers:
            x = res.process(x)
        return x

    def flush(self):
        self._buf = np.zeros(0, dtype=np.complex64)


# ---------------------------------------------------------------- filters


class Lowpass:
    """Stateful real-coefficient low-pass; accepts real or complex input.

    Because the coefficients are real the response is symmetric about 0 Hz,
    so this must be used *before* taking a real part, never to pick a
    sideband (see :class:`SidebandFilter`).
    """

    def __init__(self, cutoff, rate=AUDIO_RATE, order=6, dtype=np.complex64):
        nyq = rate / 2.0
        wn = min(max(cutoff, 1.0), nyq * 0.98) / nyq
        self.sos = butter(order, wn, btype="low", output="sos")
        self.dtype = dtype
        self._zi = None

    def __call__(self, x):
        if self._zi is None:
            # The state takes its dtype from the signal.  Always allocating it
            # complex made the filter work in complex arithmetic for a real
            # input too, so the FM discriminator's post-detection low-pass ran
            # complex and threw its imaginary part away again at the end of
            # every call -- with a ComplexWarning to say so.
            self._zi = np.zeros((self.sos.shape[0], 2),
                                dtype=np.complex64 if np.iscomplexobj(x)
                                else np.float32)
        y, self._zi = sosfilt(self.sos, x, zi=self._zi)
        return y.astype(self.dtype, copy=False)

    def reset(self):
        self._zi = None


class SidebandFilter:
    """Asymmetric (complex) band-pass -- selects one sideband.

    Implemented as shift-down / real low-pass / shift-back / take real part,
    which is algebraically identical to filtering with the complex impulse
    response ``h[n]*exp(j*2*pi*fc*n/rate)`` but much cheaper.  A symmetric
    real filter could never separate USB from LSB, because it has the same
    gain at +f and -f.
    """

    def __init__(self, f_lo, f_hi, rate=AUDIO_RATE, order=6):
        self.fc = 0.5 * (f_lo + f_hi)
        self.half = max(0.5 * abs(f_hi - f_lo), 1.0)
        self.rate = rate
        self.lp = Lowpass(self.half, rate, order)
        self._phase = 0.0

    def __call__(self, x):
        n = x.size
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        inc = -2.0 * math.pi * self.fc / self.rate
        ph = self._phase + inc * np.arange(n, dtype=np.float64)
        self._phase = float((ph[-1] + inc) % (2 * math.pi))
        e = np.exp(1j * ph).astype(np.complex64)
        y = self.lp(x * e)
        return (y * np.conjugate(e)).real.astype(np.float32)

    def reset(self):
        self.lp.reset()
        self._phase = 0.0


class DCBlock:
    """One-pole high-pass used to strip DC / envelope offset.

    y[n] = x[n] - x[n-1] + r*y[n-1], evaluated with a stateful IIR so the
    block boundaries stay continuous.
    """

    def __init__(self, cutoff=30.0, rate=AUDIO_RATE, dtype=np.float32):
        r = float(np.exp(-2 * math.pi * cutoff / rate))
        self.b = np.array([1.0, -1.0], dtype=dtype)
        self.a = np.array([1.0, -r], dtype=dtype)
        self._zi = np.zeros(1, dtype=dtype if np.issubdtype(np.dtype(dtype), np.floating) else np.complex64)

    def __call__(self, x):
        y, self._zi = lfilter(self.b, self.a, x, zi=self._zi)
        return y

    def reset(self):
        self._zi = np.zeros_like(self._zi)


class AudioAGC:
    """RMS-tracking AGC sized for *block* updates.

    The coefficients are derived from the real duration of each block, so the
    AGC behaves identically whether it is called with 40 ms or 5 ms of audio.
    (Getting this wrong is a classic trap: per-sample coefficients applied
    once per block give a time constant hundreds of times too long.)
    """

    def __init__(self, rate=AUDIO_RATE, target=0.15, attack_s=0.005,
                 decay_s=0.4, gain_tau_s=0.08, max_gain=1000.0, enabled=True):
        self.enabled = enabled
        self.rate = float(rate)
        self.target = float(target)
        self.attack_s = float(attack_s)
        self.decay_s = float(decay_s)
        self.gain_tau_s = float(gain_tau_s)
        self.max_gain = float(max_gain)
        self.env = 0.0
        self.gain = 1.0

    def __call__(self, x):
        if not self.enabled or x.size == 0:
            return x
        dt = x.size / self.rate
        a_att = math.exp(-dt / self.attack_s)
        a_dec = math.exp(-dt / self.decay_s)
        rms = math.sqrt(float(np.mean(x.astype(np.float64) ** 2)))
        self.env = rms + (a_att if rms > self.env else a_dec) * (self.env - rms)

        want = self.max_gain if self.env < 1e-9 else min(self.target / self.env,
                                                         self.max_gain)
        self.gain += (1.0 - math.exp(-dt / self.gain_tau_s)) * (want - self.gain)
        y = x * self.gain

        # Fold a safety limit into the gain so the correction stays continuous
        # across block boundaries instead of stepping.
        over = float(np.max(np.abs(y))) if y.size else 0.0
        if over > 0.95:
            self.gain *= 0.95 / over
            y = x * self.gain
        return y


# ------------------------------------------------------------ noise reduction


class NoiseReducer:
    """Spectral-subtraction hiss reduction for demodulated audio.

    A short-time Fourier transform -- 75 % overlap with a square-root Hann
    window -- estimates a noise level, and each bin is scaled by a Wiener-style
    gain derived from its signal-to-noise ratio against that level.  Analysis
    and synthesis use the same window and their squared overlap sums to a
    constant, so a gain of exactly 1 reconstructs the input exactly.

    The floor is a low percentile of the smoothed power taken *across all
    bins* each frame, so a carrier -- which occupies a handful of bins -- cannot
    drag the estimate up, and the estimate does not care whether the signal is
    keyed, steady or fading.  A slow decaying minimum on top of it stops the
    level from being pulled up by a burst of loud broadband signal.

    Timing: the emitted samples are index-aligned with the input -- output
    sample *m* is input sample *m* -- but the newest ``latency`` samples are
    held back while the last transform fills, so the audio comes out delayed by
    that much (8 ms at the defaults).  The first ``n_fft`` samples fade in,
    because the overlap-add accumulator starts empty; that is inaudible at the
    moment the feature is switched on.
    """

    def __init__(self, rate=AUDIO_RATE, n_fft=512, hop=None, strength=0.5,
                 enabled=False):
        self.rate = float(rate)
        self.n_fft = int(n_fft)
        self.hop = int(hop) if hop else max(1, self.n_fft // 4)
        if self.n_fft % self.hop:
            raise ValueError("n_fft must be a multiple of hop")
        self.enabled = bool(enabled)
        self.strength = float(strength)

        n = np.arange(self.n_fft, dtype=np.float64)
        hann = 0.5 * (1.0 - np.cos(2.0 * np.pi * n / self.n_fft))
        self.win = np.sqrt(hann).astype(np.float32)
        # A periodic Hann's squared window sums to hop/2 per sample at any hop
        # that divides n_fft, which is exactly the overlap-add gain to undo.
        self.norm = float(0.5 * self.n_fft / self.hop)

        # Tuning knobs.  In words: smooth the power, take a low percentile
        # across the band as the noise level, subtract `over` times it, never
        # attenuate a bin by more than `floor`, and smooth the gain over
        # neighbouring bins and over time so the residue sounds like quiet noise
        # instead of "musical" warble.
        #
        # A per-bin *time* minimum was tried first and rejected: it cannot make
        # sense of a keyed signal (whose bins never sit still long enough for
        # the floor to be observed) and it eventually absorbs a steady carrier.
        # A percentile across bins needs neither assumption.
        self.alpha_p = 0.6         # power smoothing, ~5 ms at 48 kHz
        self.percentile = 20.0     # noise level = this percentile across bins
        self.rise = 1.02           # noise level may climb by e in ~0.13 s
        self.over_db = (2.0, 9.0)  # over-subtraction, dB (low, high strength)
        self.floor_db = (2.0, 18.0)  # residual floor, dB below unity
        self.gain_smooth = 0.6     # gain smoothing across frames

        self._in = np.zeros(0, dtype=np.float32)
        self._ola = np.zeros(self.n_fft, dtype=np.float32)
        self._kernel = np.array([0.25, 0.5, 0.25])
        self._k = max(1, int(self.percentile / 100.0 * (self.n_fft // 2 + 1)))
        self._ps = None
        self._nf = None
        self._gain = None

    @property
    def latency(self) -> int:
        """Samples held back (the exact count for input that is a hop multiple)."""
        return self.n_fft - self.hop

    def reset(self) -> None:
        self._in = np.zeros(0, dtype=np.float32)
        self._ola = np.zeros(self.n_fft, dtype=np.float32)
        self._ps = None
        self._nf = None
        self._gain = None

    def _gains(self, power: np.ndarray) -> np.ndarray:
        """Per-bin gains for the current frame, from the tracked noise level."""
        # np.partition rather than np.percentile: the latter costs ~100 us per
        # call at this size (almost all fixed overhead), which would dominate
        # the whole feature.  This does the same select in ~3 us.
        if self._ps is None:
            # First frame: open wide and let the estimate settle, otherwise the
            # noise level is by definition the signal and the start is squashed.
            self._ps = power
            self._nf = float(np.partition(power, self._k)[self._k])
            return np.ones_like(power)

        self._ps = self.alpha_p * self._ps + (1.0 - self.alpha_p) * power
        smooth = np.convolve(self._ps, self._kernel, mode="same")
        band = float(np.partition(smooth, self._k)[self._k])
        self._nf = min(band, self._nf * self.rise)

        s = min(max(self.strength, 0.0), 1.0)
        over = 10.0 ** ((self.over_db[0] +
                         (self.over_db[1] - self.over_db[0]) * s) / 10.0)
        floor = 10.0 ** (-(self.floor_db[0] +
                           (self.floor_db[1] - self.floor_db[0]) * s) / 20.0)
        ref = over * self._nf + 1e-30
        snr = np.maximum(power / ref - 1.0, 0.0)
        g = snr / (snr + 1.0)
        g = np.convolve(g, self._kernel, mode="same")
        g = floor + (1.0 - floor) * g
        if self._gain is None or self._gain.size != g.size:
            self._gain = g
        else:
            a = self.gain_smooth
            self._gain = a * self._gain + (1.0 - a) * g
        return self._gain

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if x.size == 0 or not self.enabled:
            return x
        self._in = np.concatenate((self._in, x)) if self._in.size else x
        n, hop = self.n_fft, self.hop
        out = []
        while self._in.size >= n:
            frame = self._in[:n]
            # Advance by one hop; the overlap stays in the buffer for the next
            # frame, which is what makes consecutive frames continuous.
            self._in = self._in[hop:]
            spec = np.fft.rfft(frame * self.win)
            power = (spec.real ** 2 + spec.imag ** 2).astype(np.float64)
            spec = spec * self._gains(power)
            y = np.fft.irfft(spec, n).astype(np.float32) * self.win
            self._ola += y
            out.append(self._ola[:hop] / self.norm)
            self._ola = np.concatenate(
                (self._ola[hop:], np.zeros(hop, dtype=np.float32)))
        if not out:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out)


# ---------------------------------------------------------------- demod


class Demodulator:
    """Complex baseband @ 48 kHz -> real audio @ 48 kHz."""

    def __init__(self, mode="usb", bw=2400.0, pitch=700.0, agc=True,
                 rate=AUDIO_RATE, squelch_db=-999.0, nr=False, nr_level=0.5):
        self.rate = rate
        self.pitch = float(pitch)
        self.mode = mode.lower()
        self.bw = float(bw)
        self.agc = AudioAGC(rate=rate, enabled=agc)
        self.nr = NoiseReducer(rate=rate, strength=nr_level, enabled=nr)
        self.dc = None
        self.squelch_db = float(squelch_db)
        self.signal_db = -200.0
        self.open = True
        self._prev = 0.0 + 0.0j
        self._nfm_lp = None
        self._sam_zi = None
        self.configure(mode, bw, pitch)

    # -- configuration ---------------------------------------------
    def configure(self, mode, bw=None, pitch=None):
        if bw is not None:
            self.bw = float(bw)
        if pitch is not None:
            self.pitch = float(pitch)
        self.mode = mode.lower()
        m = self.mode
        self._sam_zi = None
        top = min(self.bw, 15000.0)
        if m in ("am", "sam"):
            # Must pass DC: the carrier *is* the reference for the detector,
            # so this has to be a low-pass and not a band-pass.
            self.band = Lowpass(top / 2.0)
            self.dc = DCBlock(40.0)
        elif m == "nfm":
            self.band = Lowpass(min(top / 2.0, 8000.0))
            self._nfm_lp = Lowpass(3000.0, dtype=np.float32)
            self.dc = DCBlock(60.0)
        elif m == "usb":
            self.band = SidebandFilter(150.0, self.bw + 150.0)
            self.dc = DCBlock(100.0, dtype=np.float32)
        elif m == "lsb":
            self.band = SidebandFilter(150.0, self.bw + 150.0)
            self.dc = DCBlock(100.0, dtype=np.float32)
        elif m == "cw":
            half = max(self.bw, 50.0) / 2.0
            lo = max(30.0, self.pitch - half)
            self.band = SidebandFilter(lo, self.pitch + half)
            self.dc = DCBlock(100.0, dtype=np.float32)
        else:
            raise ValueError(f"unknown mode {mode!r}")

    @property
    def mix_shift(self) -> float:
        """Extra frequency shift applied when mixing the DDC output."""
        if self.mode == "cw":
            return -self.pitch
        return 0.0

    # -- processing ------------------------------------------------
    def __call__(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)
        # 10*log10 of a *power*, not 20*log10 of one.  The old form doubled
        # every dB on the readout -- a signal 6 dB stronger showed as 12 dB --
        # so the status bar and the spectrum disagreed by a factor of two about
        # the same signal.  Silence now reaches exactly the -200 dB "unknown"
        # sentinel too, instead of -400.
        self.signal_db = 10.0 * math.log10(float(np.mean(np.abs(x) ** 2)) + 1e-20)
        self.open = self.signal_db >= self.squelch_db

        m = self.mode
        if m == "lsb":
            # Flip the spectrum so the lower sideband lands where the
            # asymmetric filter expects it, then treat it as USB.
            x = np.conjugate(x)
        if m == "nfm":
            f = self.band(x)
            prev = np.empty_like(f)
            prev[0] = self._prev
            prev[1:] = f[:-1]
            self._prev = f[-1]
            d = np.angle(f * np.conjugate(prev)).astype(np.float32)
            y = self._nfm_lp(d)
            y = self.dc(y)
        elif m == "am":
            y = self.dc(np.abs(self.band(x)).astype(np.float32))
        elif m == "sam":
            # Feed-forward synchronous detector: track the carrier with a very
            # narrow low-pass, normalise it, then product-detect.  Much better
            # than an envelope detector on a fading shortwave signal.
            a = 0.02
            if self._sam_zi is None:
                self._sam_zi = np.zeros(1, dtype=np.complex64)
            car, self._sam_zi = lfilter(np.array([a], np.float32),
                                        np.array([1.0, -(1.0 - a)], np.float32),
                                        x.astype(np.complex64), zi=self._sam_zi)
            mag = np.abs(car)
            mag[mag < 1e-9] = 1e-9
            y = self.dc((self.band(x) * np.conjugate(car / mag)).real.astype(np.float32))
        else:  # usb / cw -- the filter already returns real audio
            y = self.dc(self.band(x))

        # Denoise *before* the AGC: a frequency-domain floor estimate only works
        # on a signal whose level is not being rescaled underneath it.
        if self.nr.enabled:
            y = self.nr(y)
        y = self.agc(y)
        if not self.open:
            y = np.zeros_like(y)
        return y


# ---------------------------------------------------------------- spectrum


class Spectrum:
    """Welch-averaged power spectrum in dBFS."""

    def __init__(self, n_fft=4096, avg=0.6):
        self.n_fft = int(n_fft)
        self.window = np.hanning(self.n_fft).astype(np.float32)
        self.wsum = float(self.window.sum())
        self.avg = float(avg)
        self._acc = None

    def __call__(self, iq: np.ndarray) -> np.ndarray | None:
        n = self.n_fft
        nseg = iq.size // n
        if nseg < 1:
            return None
        segs = iq[:nseg * n].reshape(nseg, n).astype(np.complex64)
        segs = segs * self.window
        spec = np.fft.fftshift(np.fft.fft(segs, axis=1), axes=1)
        power = np.mean(np.abs(spec) ** 2, axis=0)
        db = 10.0 * np.log10(power / (self.wsum ** 2) + 1e-20)
        if self._acc is None:
            self._acc = db
        else:
            self._acc = self.avg * self._acc + (1.0 - self.avg) * db
        return self._acc


# ---------------------------------------------------------------- utility


def to_int16(audio: np.ndarray, volume: float = 1.0) -> bytes:
    """Float audio -> interleaved mono int16 little-endian bytes."""
    if audio.size == 0:
        return b""
    a = audio * float(volume)
    np.clip(a, -1.0, 1.0, out=a)
    return (a * 32767.0).astype("<i2").tobytes()


def audio_rms_db(audio: np.ndarray) -> float:
    """Mean power of the audio in dBFS (a full-scale sine reads -3 dB).

    Power, so 10*log10: the doubled 20*log10 form made every level on the
    readout twice the size it should have been.
    """
    if audio.size == 0:
        return -200.0
    return 10.0 * math.log10(float(np.mean(audio.astype(np.float64) ** 2)) + 1e-20)
