"""Unit tests for the pieces that need no radio and no window.

The DSP test covers the resampling chain and the demodulators on real signals;
the GUI test covers the window.  This one fills the gaps between them: every
helper that both of those depend on, plus the audio output's buffering logic
driven by a stub backend so it can be tested without a sound card.

Run with:  python3 tests/test_units.py
"""

from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rspsdr import bands  # noqa: E402
from rspsdr.audio import (  # noqa: E402
    AudioOutput, _clip_i16, describe_audio, find_pipe_command,
)
from rspsdr.dsp import (  # noqa: E402
    AUDIO_RATE, AudioAGC, DCBlock, Demodulator, Downconverter, Lowpass,
    NoiseReducer, PolyphaseResampler, SidebandFilter, Spectrum, audio_rms_db,
    design_lowpass, plan_chain, to_int16,
)
from rspsdr.memory import Channel, MemoryStore, adopt_previous, previous_path  # noqa: E402
from rspsdr.radio import SPECTRUM_SEGMENTS, Settings  # noqa: E402

failures: list[str] = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" :: {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def rms(v):
    v = np.asarray(v)
    if np.iscomplexobj(v):
        v = np.abs(v)
    v = v.astype(np.float64)
    return float(np.sqrt(np.mean(v ** 2))) if v.size else 0.0


def tone(fs, freq, n, amp=1.0, phase=0.0):
    t = np.arange(n, dtype=np.float64) / fs
    return (amp * np.exp(1j * (2 * math.pi * freq * t + phase))).astype(np.complex64)


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    except Exception:
        return False
    return False


# ---------------------------------------------------------------- filters
print("== filter design ==")
h = np.asarray(design_lowpass(1000.0, 1400.0, 48000.0, atten=70.0), np.float64)
# DC gain of a low-pass is the sum of its taps.
check("low-pass passes DC at unity gain", abs(h.sum() - 1.0) < 1e-3,
      f"sum={h.sum():.6f}")
check("the tap count is odd, so the filter is symmetric", h.size % 2 == 1,
      f"{h.size} taps")
# Response at chosen frequencies, by evaluating the transfer function by hand.
f = np.array([0.0, 1000.0, 1400.0, 4000.0])
z = np.exp(-2j * math.pi * f[:, None] * np.arange(h.size)[None, :] / 48000.0)
resp = np.abs(h @ z.T)
check("low-pass rejects the stop band",
      resp[2] < 10 ** (-60 / 20) and resp[3] < 10 ** (-70 / 20),
      "response " + ", ".join(f"{freq:.0f}Hz {20 * math.log10(r + 1e-30):.1f}dB"
                              for freq, r in zip(f, resp)))
check("an impossible pass band is clamped, not fatal",
      len(design_lowpass(99999.0, 999999.0, 48000.0)) > 0)

print("\n== low-pass object ==")
lp = Lowpass(1000.0)
x = np.cos(2 * math.pi * 200 * np.arange(20000) / AUDIO_RATE).astype(np.float32)
y = lp(x)
check("Lowpass is continuous across blocks",
      y.size == x.size and abs(rms(y[2000:]) - rms(x[2000:])) < 0.02,
      f"rms in {rms(x[2000:]):.3f} out {rms(y[2000:]):.3f}")
lp.reset()
check("Lowpass.reset clears the state", lp._zi is None)
cplx = tone(AUDIO_RATE, 200.0, 20000, amp=0.5)
check("Lowpass accepts complex input",
      np.iscomplexobj(Lowpass(1000.0)(cplx)))

print("\n== sideband filter ==")
sb = SidebandFilter(150.0, 2550.0)
up = Demodulator("usb", bw=2400, agc=False)
lo = Demodulator("lsb", bw=2400, agc=False)
hi_side = tone(AUDIO_RATE, 1500.0, 48000, amp=0.5)
lo_side = tone(AUDIO_RATE, -1500.0, 48000, amp=0.5)
r_up_hi = rms(up(hi_side.copy())[8000:])
r_up_lo = rms(up(lo_side.copy())[8000:])
check("USB passes the upper sideband and not the lower",
      r_up_hi > 0.2 and r_up_lo < r_up_hi / 20,
      f"upper {r_up_hi:.3f} lower {r_up_lo:.3f}")
r_lo_lo = rms(lo(lo_side.copy())[8000:])
r_lo_hi = rms(lo(hi_side.copy())[8000:])
check("LSB passes the lower sideband and not the upper",
      r_lo_lo > 0.2 and r_lo_hi < r_lo_lo / 20,
      f"lower {r_lo_lo:.3f} upper {r_lo_hi:.3f}")
sb.reset()
check("SidebandFilter.reset clears the phase", sb._phase == 0.0)

print("\n== dc block ==")
dc = DCBlock(40.0)
constant = dc(np.ones(48000, np.float32))
check("DCBlock removes a constant offset",
      abs(float(np.mean(constant[12000:24000]))) < 1e-3,
      f"settled mean {float(np.mean(constant[12000:24000])):+.2e}")
step_in = np.concatenate([np.zeros(24000, np.float32),
                          np.full(24000, 1.0, np.float32)])
step_out = DCBlock(40.0)(step_in)
check("DCBlock passes a step through and then re-settles",
      np.all(np.isfinite(step_out)) and float(np.max(step_out[24000:25000])) > 0.5
      and abs(float(np.mean(step_out[-2000:]))) < 1e-3,
      f"edge peak {float(np.max(step_out[24000:25000])):.3f}, "
      f"settled {float(np.mean(step_out[-2000:])):+.2e}")
dc.reset()
check("DCBlock.reset clears the state", float(abs(dc(np.zeros(1, np.float32))[0])) == 0.0)

# ---------------------------------------------------------------- resampler
print("\n== resampler ==")
rs_probe = PolyphaseResampler(1, 4, 192000.0, 48000.0, 20000.0)
check("the resampler reports the delay of its own filter",
      0 <= rs_probe.latency_samples <= len(rs_probe.h) // 2,
      f"{rs_probe.latency_samples} samples of {len(rs_probe.h)} taps")
check("rejects a zero decimation",
      raises(ValueError, PolyphaseResampler, 1, 0, 48000.0, 48000.0, 20000.0))
check("rejects a zero interpolation",
      raises(ValueError, PolyphaseResampler, 0, 1, 48000.0, 48000.0, 20000.0))
rs = PolyphaseResampler(2, 1, 24000.0, 48000.0, 10000.0)
check("an integer interpolation gets the output rate right",
      rs.up == 2 and rs.down == 1 and abs(rs.out_rate - 48000.0) < 1e-9,
      f"{rs.out_rate:g} Hz")
check("an empty block yields no output", rs.process(np.empty(0, np.complex64)).size == 0)

print("\n== downconverter buffering ==")
for rate in (62500, 1000000, 2000000):
    dc = Downconverter(rate)
    hop = dc.hop_in
    # Deliberately ragged blocks: push() must regroup them into whole hops.
    rng = np.random.default_rng(4)
    blocks = []
    fed = 0
    for size in (1000, 777, 5000, 12345, 99, 40000, 200000):
        blocks.extend(dc.push(tone(rate, 0.0, size, amp=0.1)))
        fed += size
    whole = all(b.size == hop for b in blocks)
    check(f"{rate/1e6:g} MSps push() yields whole hops",
          whole and len(blocks) == fed // hop and dc._buf.size == fed % hop,
          f"{len(blocks)} blocks of {hop} from {fed} samples, "
          f"{dc._buf.size} held back")
    dc.flush()
    check(f"{rate/1e6:g} MSps flush() discards the partial hop",
          dc._buf.size == 0)

# ---------------------------------------------------------------- agc
print("\n== audio agc ==")
agc = AudioAGC(enabled=False)
flat = np.full(1024, 0.3, np.float32)
check("AGC disabled is a pass-through", agc(flat) is flat)

agc = AudioAGC(rate=AUDIO_RATE, target=0.15)
quiet = (0.01 * np.sin(2 * math.pi * 1000 * np.arange(AUDIO_RATE) / AUDIO_RATE)
         ).astype(np.float32)
out = np.concatenate([agc(quiet[i:i + 1920]) for i in range(0, quiet.size, 1920)])
check("AGC lifts a quiet signal towards the target",
      abs(rms(out[-48000:]) - 0.15) < 0.03,
      f"rms {rms(out[-48000:]):.4f} from {rms(quiet):.4f}")
check("AGC never lets the output clip", float(np.max(np.abs(out))) <= 1.0,
      f"peak {float(np.max(np.abs(out))):.4f}")

loud = (0.9 * np.sin(2 * math.pi * 1000 * np.arange(AUDIO_RATE) / AUDIO_RATE)
        ).astype(np.float32)
out2 = np.concatenate([agc(loud[i:i + 1920]) for i in range(0, loud.size, 1920)])
check("AGC pulls a loud signal down without a step at the block edge",
      float(np.max(np.abs(out2))) <= 1.0
      and float(np.max(np.abs(np.diff(out2)))) < 0.2,
      f"peak {float(np.max(np.abs(out2))):.3f}, "
      f"largest sample step {float(np.max(np.abs(np.diff(out2)))):.3f}")

# ---------------------------------------------------------------- demod
print("\n== demodulator ==")
check("an unknown mode is rejected",
      raises(ValueError, Demodulator, "fm-wide"))
check("an empty block yields empty audio",
      Demodulator("usb").__call__(np.empty(0, np.complex64)).size == 0)
d = Demodulator("usb", bw=2400, agc=False)
check("USB applies no mixer shift", d.mix_shift == 0.0)
check("CW shifts the baseband by its pitch",
      Demodulator("cw", bw=500, pitch=700.0).mix_shift == -700.0)

# Every mode must survive a reconfiguration in mid-stream, which is what
# changing mode in the GUI does.
d = Demodulator("am", bw=6000, agc=False)
for mode in ("usb", "lsb", "cw", "nfm", "sam", "am"):
    d.configure(mode, 2400.0, 700.0)
    y = d(tone(AUDIO_RATE, 800.0, 9600, amp=0.4))
    if y.size != 9600 or not bool(np.all(np.isfinite(y))):
        break
check("the demodulator survives being reconfigured between modes",
      d.mode == "am" and y.size == 9600 and bool(np.all(np.isfinite(y))),
      f"last mode {d.mode}, {y.size} samples")

sq = Demodulator("am", bw=6000, agc=False, squelch_db=-30.0)
loud = tone(AUDIO_RATE, 0.0, 9600, amp=0.5)
sq(loud)
open_high = sq.open and sq.signal_db > -30.0
sq(tone(AUDIO_RATE, 0.0, 9600, amp=1e-4))
check("the squelch closes below its threshold and reopens above it",
      open_high and not sq.open,
      f"loud {20 * math.log10(0.5):.1f} dB, quiet "
      f"{20 * math.log10(1e-4):.1f} dB, threshold -30 dB")
check("a closed squelch emits true silence",
      float(np.max(np.abs(sq(tone(AUDIO_RATE, 0.0, 9600, amp=1e-4))))) == 0.0)

print("\n== noise reducer ==")
check("a hop that does not divide the FFT is rejected",
      raises(ValueError, NoiseReducer, AUDIO_RATE, 512, 100))
nr = NoiseReducer(strength=0.5, enabled=True)
nr(np.random.default_rng(5).standard_normal(4096).astype(np.float32))
nr.reset()
check("NoiseReducer.reset clears its history",
      nr._in.size == 0 and nr._ps is None and nr._nf is None
      and nr._gain is None and float(np.max(np.abs(nr._ola))) == 0.0)

# ---------------------------------------------------------------- spectrum
print("\n== spectrum ==")
N = 4096
sp = Spectrum(N, avg=1.0)
short = np.zeros(N // 2, np.complex64)
check("a short block yields no spectrum", sp(short) is None)

# A tone whose frequency is k bins from the centre, where one bin is
# span/N, lands at index N//2 + k once the FFT is shifted.
for k_bin in (-500, -1, 0, 1, 500):
    x = tone(AUDIO_RATE, k_bin * AUDIO_RATE / N, N, amp=1.0)
    db = Spectrum(N, avg=1.0)(x)
    peak = int(np.argmax(db))
    if peak != N // 2 + k_bin:
        break
check("a tone lands in the bin its frequency says",
      peak == N // 2 + k_bin,
      f"{k_bin} bins from centre -> index {peak}, wanted {N // 2 + k_bin}")
check("DC is the middle bin, not the first",
      int(np.argmax(Spectrum(N, avg=1.0)(tone(AUDIO_RATE, 0.0, N)))) == N // 2)
db = Spectrum(N, avg=1.0)(tone(AUDIO_RATE, 0.0, N, amp=1.0))
check("a full-scale tone reads about 0 dBFS",
      abs(float(db.max())) < 2.0, f"{float(db.max()):.2f} dBFS")

# With avg=0.5 the tracked level is the mean of the old and the new frame:
# a full-scale frame reads 0 dB, so a following -20 dB frame reads -10 dB.
sp2 = Spectrum(N, avg=0.5)
h1 = sp2(tone(AUDIO_RATE, 0.0, N, amp=1.0)).copy()
h2 = sp2(tone(AUDIO_RATE, 0.0, N, amp=0.1)).copy()
check("the average blends successive frames",
      abs(float(h1[N // 2])) < 1.0 and abs(float(h2[N // 2]) + 10.0) < 1.0,
      f"full-scale frame {float(h1[N // 2]):.2f} dB, after a -20 dB frame "
      f"{float(h2[N // 2]):.2f} dB with avg=0.5")

print("\n== audio conversion ==")
sig = np.array([0.0, 1.0, -1.0, 2.0, -2.0], np.float32)
check("to_int16 clips and scales",
      list(np.frombuffer(to_int16(sig), "<i2")) == [0, 32767, -32767, 32767, -32767])
check("to_int16 applies the volume it is given",
      list(np.frombuffer(to_int16(sig, 0.5), "<i2"))[:2] == [0, 16383])
check("to_int16 of nothing is nothing", to_int16(np.zeros(0, np.float32)) == b"")

loud = np.array([3.0, -3.0], np.float32)
was = loud.copy()
raw = _clip_i16(loud)
check("_clip_i16 does not modify the buffer it is handed",
      np.array_equal(loud, was) and list(np.frombuffer(raw, "<i2")) == [32767, -32767],
      f"input now {loud.tolist()}")

check("audio_rms_db reaches its -200 dB floor for silence",
      audio_rms_db(np.zeros(4, np.float32)) == -200.0,
      f"{audio_rms_db(np.zeros(4, np.float32)):.1f} dB")
check("audio_rms_db of an empty buffer is the floor",
      audio_rms_db(np.zeros(0, np.float32)) == -200.0)
# Both ends of the Kaiser design formula, and a resampler asked for fewer
# samples than one output period.
from rspsdr.dsp import _kaiser_beta  # noqa: E402
check("the filter window tapers off at low attenuation",
      _kaiser_beta(60.0) > 5.0 and abs(_kaiser_beta(20.0)) == 0.0
      and _kaiser_beta(25.0) != _kaiser_beta(60.0),
      f"60 dB -> {_kaiser_beta(60.0):.2f}, 20 dB -> {_kaiser_beta(20.0):.2f}")
stingy = PolyphaseResampler(1, 4, 192000.0, 48000.0, 20000.0)
# The calls have to be made once, in order, and not inside the detail string:
# a resampler carries state, so an extra call to describe the result would
# change the very thing being described.  A single input sample does yield one
# output sample -- the filter works out output 0 from its zero-filled history --
# so what has to hold is that the output grid never slips, however the input is
# chopped up.
from_one = stingy.process(np.zeros(1, np.complex64))
from_next = stingy.process(np.zeros(1024, np.complex64))
produced = from_one.size + from_next.size
due = (1 + 1024 - 1) // 4 + 1
check("a ragged pair of blocks yields exactly the samples due from them",
      produced == due and from_one.size == 1,
      f"{from_one.size} sample from a one-sample block plus "
      f"{from_next.size} from the next 1024 = {produced}, and {due} are due "
      f"for 1025 input samples at 1/4")
sine = np.sin(2 * math.pi * 1000 * np.arange(48000) / AUDIO_RATE).astype(np.float32)
check("audio_rms_db of a full-scale sine is -3 dB, not -6",
      abs(audio_rms_db(sine) + 3.01) < 0.05, f"{audio_rms_db(sine):.2f} dB")
# The same convention as the spectrum, which is the whole point of the fix:
# a signal twice the amplitude must read 6 dB louder in both places.
demod_lvl = Demodulator("am", bw=6000, agc=False)
demod_lvl(tone(AUDIO_RATE, 0.0, 4096, amp=0.5))
loud_db = demod_lvl.signal_db
demod_lvl(tone(AUDIO_RATE, 0.0, 4096, amp=0.25))
check("the signal meter moves 6 dB for a halving of amplitude",
      abs((loud_db - demod_lvl.signal_db) - 6.02) < 0.1,
      f"{loud_db:.2f} -> {demod_lvl.signal_db:.2f} dB")

print("\n== audio player fallbacks ==")
import rspsdr.audio as audio_mod  # noqa: E402

real_which = audio_mod.shutil.which

try:
    # Only the first available player is ever reached on a machine that has
    # pacat, so the rest of the chain has to be exercised deliberately.
    audio_mod.shutil.which = lambda name: "/usr/bin/" + name if name == "pw-play" else None
    cmd = find_pipe_command(AUDIO_RATE)
    check("with no pacat it falls back to pw-play",
          bool(cmd) and cmd[0] == "pw-play", " ".join(cmd or []))
    audio_mod.shutil.which = lambda name: "/usr/bin/" + name if name == "aplay" else None
    cmd = find_pipe_command(AUDIO_RATE)
    check("with neither it falls back to aplay",
          bool(cmd) and cmd[0] == "aplay", " ".join(cmd or []))
    audio_mod.shutil.which = lambda name: None
    check("with none of them it says so instead of guessing",
          find_pipe_command(AUDIO_RATE) is None)
    described = describe_audio()
    check("and --check reports the absence",
          "no CLI audio player" in described, described)
finally:
    audio_mod.shutil.which = real_which

print("\n== audio output ==")


class _StubBackend:
    """Stands in for pacat: records what it was given, nothing else."""

    name = "stub"

    def __init__(self):
        self.chunks = []
        self.closed = False

    def write(self, data):
        if self.closed:
            return False
        self.chunks.append(data)
        return True

    def close(self):
        self.closed = True

    def describe(self):
        return "stub"


class StubAudio(AudioOutput):
    def __init__(self, **kw):
        self.stub = _StubBackend()
        kw.setdefault("backend", "pipe")
        super().__init__(**kw)

    def _make_backend(self, backend):
        return self.stub


block = np.full(960, 0.5, np.float32)
def peak_of(chunks):
    raw = b"".join(chunks)
    return max(abs(int(v)) for v in np.frombuffer(raw, "<i2")) if raw else 0


def drained(a, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and a._nbytes > 0:
        time.sleep(0.005)
    return a._nbytes == 0


out = StubAudio(rate=AUDIO_RATE)
out.start()
out.write(block)                                    # a half-scale block
check("the audio reaches the backend", drained(out) and peak_of(out.stub.chunks) == 16383,
      f"peak {peak_of(out.stub.chunks)}")
out.volume = 0.5
sent_before = len(out.stub.chunks)
out.write(block)
drained(out)
half = peak_of(out.stub.chunks[sent_before:])
check("volume is applied on the way out", abs(half - 16383 / 2) <= 1,
      f"peak {half} at volume 0.5")
before_mute = len(out.stub.chunks)
out.muted = True
out.write(block)
drained(out)
check("mute stops the audio but still reports the peak",
      out.peak == 0.5 and len(out.stub.chunks) == before_mute)
out.muted = False
out.stop()

# The writer thread must actually drain the buffer into the backend.
out2 = StubAudio(rate=AUDIO_RATE)
out2.start()
for _ in range(5):
    out2.write(block)
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline and out2._nbytes > 0:
    time.sleep(0.01)
out2.stop()
check("the writer thread drains everything it is given",
      out2._nbytes == 0 and len(out2.stub.chunks) == 5,
      f"{len(out2.stub.chunks)} chunks, {out2._nbytes} bytes buffered, "
      f"{out2.underruns} underruns")

# A stalled sound card must cost audio, never the radio thread.
out3 = StubAudio(rate=AUDIO_RATE, max_seconds=0.01)
for _ in range(50):
    out3.write(block)
check("the buffer is bounded and drops the oldest audio",
      out3._nbytes <= out3.max_bytes and out3.overruns > 0,
      f"{out3.overruns} overruns, {out3._nbytes} bytes held "
      f"(limit {out3.max_bytes})")
out3.drain()
check("drain empties the buffer", out3._nbytes == 0 and len(out3._buf) == 0)

# The regression that leaked a player process per late write.
out4 = StubAudio(rate=AUDIO_RATE)
out4.close()
out4.write(block)
check("a write after close is refused, not a new process",
      out4.stub.closed and out4._closed and not out4.stub.chunks)
out4.write(np.zeros(0, np.float32))
check("an empty write after close is still a no-op", not out4.stub.chunks)

cmd = find_pipe_command(AUDIO_RATE)
check("the chosen pipe player is one that is installed",
      cmd is None or (isinstance(cmd, list) and bool(cmd)
                      and shutil.which(cmd[0]) is not None),
      " ".join(cmd) if cmd else "none installed")
described = describe_audio()
check("the audio path can describe itself for --check",
      bool(described) and (cmd is None or described.split()[0] == cmd[0]),
      described)

# The recorder's stand-in sink: headless recording swaps it in for the real
# output, so it has to survive the same lifecycle calls.  (It used to be that
# the running output was left half-stopped instead of being stopped.)
from rspsdr.__main__ import _NullAudio  # noqa: E402
sink = []
null = _NullAudio(sink)
null.start()
null.write(np.array([0.25, -0.25], np.float32))
null.stop()
null.close()
check("the recording stand-in takes audio and closes cleanly",
      len(sink) == 1 and list(sink[0]) == [0.25, -0.25]
      and null.volume == 1.0 and null.muted is False,
      f"{len(sink)} block(s) captured")

# ---------------------------------------------------------------- settings
print("\n== settings ==")
s = Settings()
check("copy_with returns a new object",
      s.copy_with(mode="usb") is not s and s.copy_with(mode="usb").mode == "usb")
check("copy_with leaves the original alone", s.mode == "lsb")
check("the tuner bandwidth is derived from the sample rate",
      s.effective_tuner_bw() == 1_536_000, str(s.effective_tuner_bw()))
check("the smallest offered tuner bandwidth is never exceeded from below",
      Settings(sample_rate=62500).effective_tuner_bw() == 200_000,
      str(Settings(sample_rate=62500).effective_tuner_bw()))
check("an explicit tuner bandwidth wins",
      Settings(sample_rate=2000000, tuner_bw=600000).effective_tuner_bw() == 600000)
check("the spectrum segment ceiling bounds the FFT work per hop",
      SPECTRUM_SEGMENTS >= 1 and Settings().n_fft >= 1024,
      f"{SPECTRUM_SEGMENTS} segments of {Settings().n_fft} points")

# The radio object without a device: the accessors the GUI leans on, and the
# path a failed device call takes to the status bar.
from rspsdr.radio import Radio  # noqa: E402
errors: list[str] = []
radio = Radio(Settings(sample_rate=1500000, center_freq=9_000_000.0,
                       tuned_freq=9_000_000.0), on_error=errors.append)
check("the receiver span is its sample rate", radio.span == 1_500_000.0, str(radio.span))
radio.set_tuned(9_600_000.0)
radio.recenter_on_tuned()
check("recentring puts the receiver on the tuned frequency",
      radio.settings.center_freq == 9_600_000.0
      and radio.settings.tuned_freq == 9_600_000.0,
      f"centre {radio.settings.center_freq:g}, tuned {radio.settings.tuned_freq:g}")
radio.set_center(2_000_000_000.0 + 1)
check("the tuning range is clamped at both ends",
      radio.settings.center_freq == 2_000_000_000.0, str(radio.settings.center_freq))
radio._error("probe failure")
check("a device error reaches both the status and the callback",
      radio.status.error == "probe failure" and errors == ["probe failure"],
      f"status {radio.status.error!r}, callback {errors}")

# ---------------------------------------------------------------- band plan
print("\n== band plan ==")
check("formatted like a dial readout", bands.format_hz(14_070_000) == "14.070.000",
      bands.format_hz(14_070_000))
check("thousands are grouped in kHz", bands.format_hz(1_234_567) == "1.234.567",
      bands.format_hz(1_234_567))
check("a sub-kHz frequency still reads three digits",
      bands.format_hz(500) == "0.500", bands.format_hz(500))
check("MHz formatting keeps six decimals",
      bands.format_mhz(14_074_000.0) == "14.074000", bands.format_mhz(14_074_000.0))
check("a band is found by frequency",
      bands.find_band(7_030_000).name == "40m"
      and bands.find_band(9_600_000).name == "31m")
check("a frequency in no band returns None", bands.find_band(13_000_000) is None)
check("a band's centre is its midpoint",
      bands.find_band(3_700_000).centre == 3_750_000.0)
check("ham and shortwave bands are ordered and non-overlapping",
      all(a.low < a.high for a in bands.HAM_BANDS + bands.SW_BANDS))
check("every preset names a mode the demodulator has",
      all(p.mode in ("am", "sam", "usb", "lsb", "cw", "nfm") for p in bands.PRESETS))
check("every preset is inside the receiver's tuning range",
      all(10_000.0 <= p.freq <= 2_000_000_000.0 for p in bands.PRESETS))
# A preset that names a band ("40m CW", "10m FM") has to actually be in it;
# the time and utility channels (WWV, CHU, VOLMET, HFDL) sit between bands on
# purpose, so they are not checked against one.
named = [(p, p.name.split()[0]) for p in bands.PRESETS
         if p.name.split()[0][:-1].isdigit() and p.name.split()[0].endswith("m")]
wrong_band = [f"{p.name} ({p.freq/1e6:.3f} MHz)" for p, want in named
              if (bands.find_band(p.freq) or bands.Band("", 0, 0, "am")).name != want]
check("every preset that names a band is inside that band",
      named and not wrong_band, ", ".join(wrong_band) or f"{len(named)} band-named presets")

# ---------------------------------------------------------------- migration
print("\n== command line interface ==")
import argparse  # noqa: E402
import io  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402

from rspsdr.__main__ import cmd_memories, main  # noqa: E402

cli_tmp = tempfile.mkdtemp(prefix="rspsdr-cli-")
old_cli_xdg = os.environ.get("XDG_CONFIG_HOME")
try:
    os.environ["XDG_CONFIG_HOME"] = cli_tmp
    MemoryStore().save_channel(Channel("cli channel", 9_400_000.0, "am", 6000.0))
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_memories(argparse.Namespace())
    text = out.getvalue()
    check("--memories lists what is saved",
          rc == 0 and "cli channel" in text and "1 memory" in text,
          text.strip().splitlines()[0] if text.strip() else "no output")
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--list"])
    check("--list reports the devices it found, or says there are none",
          rc in (0, 1), f"exit {rc}")
finally:
    shutil.rmtree(cli_tmp, ignore_errors=True)
    if old_cli_xdg is None:
        os.environ.pop("XDG_CONFIG_HOME", None)
    else:
        os.environ["XDG_CONFIG_HOME"] = old_cli_xdg

print("\n== config directory ==")
tmp = tempfile.mkdtemp(prefix="rspsdr-units-")
old_xdg = os.environ.get("XDG_CONFIG_HOME")
try:
    os.environ["XDG_CONFIG_HOME"] = tmp
    from rspsdr.memory import config_dir, default_path
    os.environ.pop("XDG_CONFIG_HOME", None)
    check("with no XDG_CONFIG_HOME it falls back to ~/.config",
          config_dir() == os.path.join(os.path.expanduser("~"), ".config", "rspsdr"),
          config_dir())
    os.environ["XDG_CONFIG_HOME"] = tmp
    check("the config directory carries the new name",
          config_dir() == os.path.join(tmp, "rspsdr"), config_dir())
    check("the pre-rename path is remembered",
          previous_path() == os.path.join(tmp, "rsp1_listener", "memories.json"),
          previous_path())
    check("nothing to adopt when there is no old file",
          adopt_previous(default_path()) is False)

    os.makedirs(os.path.dirname(previous_path()), exist_ok=True)
    with open(previous_path(), "w", encoding="utf-8") as fh:
        fh.write('{"version": 1, "channels": ['
                 '{"name": "legacy 40m", "freq": 7030000.0, "mode": "cw",'
                 ' "filter_bw": 500.0, "cw_pitch": 700.0}]}')
    store = MemoryStore()
    check("saved channels survive the rename",
          len(store) == 1 and store[0].name == "legacy 40m"
          and abs(store[0].freq - 7_030_000.0) < 1e-9,
          f"{len(store)} channel(s) from {store.path}")
    check("the pre-rename file is left in place as a fallback",
          os.path.exists(previous_path()))
    store.save_channel(Channel("new", 5_000_000.0, "am", 6000.0))
    check("the new file becomes the live one, and the old one is not touched",
          len(MemoryStore()) == 2 and len(MemoryStore(previous_path())) == 1,
          f"{len(MemoryStore())} live vs {len(MemoryStore(previous_path()))} legacy")
    check("adoption runs once and does not overwrite later edits",
          adopt_previous(default_path()) is False)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    if old_xdg is None:
        os.environ.pop("XDG_CONFIG_HOME", None)
    else:
        os.environ["XDG_CONFIG_HOME"] = old_xdg

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all unit checks passed")
