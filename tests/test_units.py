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
import random
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

# The transform is scipy.fft, which is several times faster than numpy.fft here
# and is what makes the large FFT sizes affordable, so it has to agree with the
# plain reference.  It also has to stay in single precision: numpy.fft used to
# promote a float32 transform to double, doubling the work behind the caller's
# back, and that promotion coming back is exactly what would spoil the 32768-
# and 65536-point settings.
BIG = 8192
# Deliberately not a pure tone: an exact transform of an exact tone bottoms out
# at the round-off floor, where the two libraries are free to disagree by tens
# of dB about bins that hold a billionth of full scale.  A small noise floor
# keeps every bin well above that, so a difference means a real difference.
rng = np.random.default_rng(11)
stack = np.vstack([
    (tone(AUDIO_RATE, k * AUDIO_RATE / BIG, BIG)
     + rng.standard_normal(BIG).astype(np.float32) * 0.01).astype(np.complex64)
    for k in (-300, 0, 300)])
win = np.hanning(BIG).astype(np.float32)
ref_power = np.mean(np.abs(np.fft.fftshift(
    np.fft.fft(stack * win, axis=1), axes=1)) ** 2, axis=0)
ref_db = 10.0 * np.log10(ref_power / (float(win.sum()) ** 2) + 1e-20)
got_db = Spectrum(BIG, avg=1.0)(stack.ravel())
spread = float(np.abs(got_db - ref_db).max())
check("the spectrum matches a plain numpy transform",
      got_db.shape == ref_db.shape and spread < 0.05
      and float(ref_db.min()) > -120.0,
      f"max difference {spread:.2e} dB over {got_db.size} bins, floor "
      f"{float(ref_db.min()):.1f} dB")
check("and stays in single precision",
      got_db.dtype == np.float32 and sp2(tone(AUDIO_RATE, 0.0, N)).dtype == np.float32,
      f"{got_db.dtype}")

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

# ---------------------------------------------------------------- the meter
# The signal meter's scale is the IARU recommendation and its dial is a printed
# one, so what it reads is arithmetic that can be checked exactly; the needle's
# ballistics are the recommended quasi-peak ones and are checked against the
# time constants they are defined by.
print("\n== signal meter scale ==")
from rspsdr import smeter  # noqa: E402
check("S9 is -73 dBm (50 uV into 50 ohms at HF)", smeter.S9_DBM == -73.0)
check("a unit is 6 dB, so S1 is -121 dBm and S5 is -97 dBm",
      smeter.dbm_for_s(1) == -121.0 and smeter.dbm_for_s(5) == -97.0,
      f"S1 {smeter.dbm_for_s(1):g}, S5 {smeter.dbm_for_s(5):g}")
check("above S9 the steps are 10 dB, not 6",
      (smeter.reading(-63.0), smeter.reading(-43.0), smeter.reading(-23.0))
      == ("S9 +10 dB", "S9 +30 dB", "S9 +50 dB"),
      ", ".join(smeter.reading(v) for v in (-63.0, -43.0, -23.0)))
check("S units and dBm convert both ways",
      all(abs(smeter.dbm_for_s(smeter.s_for_dbm(smeter.dbm_for_s(u))) -
              smeter.dbm_for_s(u)) < 1e-9 for u in (1, 2.5, 9, 12, 19)))
check("the readings are the ones an operator would say",
      (smeter.reading(-121.0), smeter.reading(-97.0), smeter.reading(-79.0),
       smeter.reading(-73.0), smeter.reading(-53.0), smeter.reading(-13.0))
      == ("S1.0", "S5.0", "S8.0", "S9 +0 dB", "S9 +20 dB", "S9 +60 dB"),
      ", ".join(smeter.reading(v) for v in (-121.0, -97.0, -79.0, -73.0, -53.0,
                                          -13.0)))
check("a level between two marks reads between them",
      smeter.reading(-76.0) == "S8.5" and smeter.reading(-100.0) == "S4.5",
      f"{smeter.reading(-76.0)}, {smeter.reading(-100.0)}")
check("below the first mark the needle is off the scale, and says so",
      smeter.reading(-140.0) == "S<1" and smeter.reading(-121.0) == "S1.0")
check("the dial starts below S1 and ends at S9+60",
      smeter.MARKS[0][0] == -127.0 and smeter.MARKS[-1][0] == -13.0,
      f"{smeter.MARKS[0][0]:g} .. {smeter.MARKS[-1][0]:g} dBm")
check("every mark is a half unit below S9 or a 10 dB step above it",
      all(((smeter.S9_DBM - dbm) % 3.0 < 1e-9 if dbm < smeter.S9_DBM
           else (dbm - smeter.S9_DBM) % 5.0 < 1e-9)
          for dbm, _label, _major in smeter.MARKS))
check("the three dB marks above S9 are numbered, and the standard's steps are long",
      [m[1] for m in smeter.MARKS if m[1] and m[0] > smeter.S9_DBM]
      == ["+20", "+40", "+60"]
      and [m[0] for m in smeter.MARKS if m[0] > smeter.S9_DBM and m[2]]
      == [-63.0, -53.0, -43.0, -33.0, -23.0, -13.0],
      str([m[1] for m in smeter.MARKS if m[1] and m[0] > smeter.S9_DBM]))

# What reads S9 is one number, and it moves the whole scale by exactly itself.
check("the default reference puts S9 at S9",
      smeter.dbm_for_dbfs(smeter.DEFAULT_REF_DBFS) == smeter.S9_DBM)
check("raising the reference by 6 dB lowers every reading by one unit",
      abs((smeter.s_for_dbm(smeter.dbm_for_dbfs(-30.0, -51.0))
           - smeter.s_for_dbm(smeter.dbm_for_dbfs(-30.0, -45.0))) - 1.0) < 1e-9)

print("\n== signal meter dial ==")
dial = smeter.Dial()
check("the marks are evenly spaced around the arc",
      all(abs((dial.mark_angle(i + 1) - dial.mark_angle(i))
              - (smeter.SPAN_DEG / (len(smeter.MARKS) - 1))) < 1e-9
          for i in range(len(smeter.MARKS) - 1)))
check("the ends of the dial are the ends of the sweep",
      dial.mark_angle(0) == smeter.START_DEG
      and abs(dial.mark_angle(len(smeter.MARKS) - 1)
              - (smeter.START_DEG + smeter.SPAN_DEG)) < 1e-9,
      f"{dial.mark_angle(0):g} .. {dial.mark_angle(len(smeter.MARKS) - 1):g}")
check("every mark points where it is drawn",
      all(abs(dial.angle(dbm) - dial.mark_angle(i)) < 1e-9
          for i, (dbm, _l, _m) in enumerate(smeter.MARKS)))
# The needle is the instrument, so what matters is what a level reads back as:
# the angle for a level must round-trip to within half the spacing of the marks
# it falls between, which is the finest a needle can be read to.
lo, hi = int(smeter.MARKS[0][0]), int(smeter.MARKS[-1][0])
round_trip = []
for dbm in range(lo, hi + 1):
    got = dial.dbm_at_angle(dial.angle(float(dbm)))
    round_trip.append(abs(got - dbm) - dial.resolution_db(float(dbm)) / 2.0)
check("every level on the dial round-trips through the needle to within half a mark",
      max(round_trip) <= 0.0, f"worst {max(round_trip):+.2f} dB of a tolerance")
check("the dial is monotonic across its whole range",
      all(dial.angle(lo + i) < dial.angle(lo + i + 1) for i in range(hi - lo)))
check("the dial resolves 3 dB in the S region and 5 dB above S9",
      dial.resolution_db(-100.0) == 3.0 and dial.resolution_db(-60.0) == 5.0,
      f"{dial.resolution_db(-100.0):g} dB, {dial.resolution_db(-60.0):g} dB")
check("a needle below the dial and one above it both stop at the ends",
      dial.angle(-200.0) == dial.mark_angle(0)
      and dial.angle(0.0) == dial.mark_angle(len(smeter.MARKS) - 1))

# The arc the needle sweeps: a distant pivot below the window, so the scale is a
# shallow bow across the face rather than a dial, and the tip stays inside it.
w, h = float(smeter.WIDTH), float(smeter.HEIGHT)
tips = [smeter.needle_geometry(w, h, dial.mark_angle(i)) for i in range(len(smeter.MARKS))]
radii = [((x - w / 2) ** 2 + (y - tips[0][1]) ** 2) ** 0.5 for _px, _py, x, y in tips]
check("every mark sits on one circle about the pivot",
      max(radii) - min(radii) < 1e-9, f"radius {min(radii):.1f} .. {max(radii):.1f}")
check("the tip stays inside the face at every mark",
      all(2.0 <= x <= w - 2.0 and 2.0 <= y <= h - 2.0 for _x, _y, x, y in tips),
      f"x {min(t[2] for t in tips):.1f}..{max(t[2] for t in tips):.1f}, "
      f"y {min(t[3] for t in tips):.1f}..{max(t[3] for t in tips):.1f}")
check("the pivot is out of sight below the window",
      tips[0][1] > h and abs(tips[0][0] - w / 2) < 1e-9,
      f"pivot y {tips[0][1]:.1f} of {h:.0f}")
check("the scale bows upwards away from the centre",
      tips[0][3] > tips[len(tips) // 2][3] and tips[-1][3] > tips[len(tips) // 2][3],
      f"ends {tips[0][3]:.1f}, middle {tips[len(tips) // 2][3]:.1f}")
check("the needle is left of centre at rest and right of it at full scale",
      tips[0][2] < w / 2 < tips[-1][2],
      f"left {tips[0][2]:.1f}, right {tips[-1][2]:.1f}, centre {w/2:.1f}")
short = smeter.needle_geometry(w, h, 0.0, 0.94)
check("the peak marker is a shorter needle, pointing the same way",
      abs(short[2] - smeter.needle_geometry(w, h, 0.0)[2]) < 1e-9
      and short[3] > smeter.needle_geometry(w, h, 0.0)[3],
      f"peak tip {short[3]:.1f} vs needle {smeter.needle_geometry(w, h, 0.0)[3]:.1f}")

print("\n== signal meter ballistics ==")
ball = smeter.Ballistics()
ball.update(-100.0, 0.05)
check("the first sample is taken as it comes, not risen to",
      ball.value == -100.0, str(ball.value))
ball.reset()
check("reset puts the needle back to where it knows nothing",
      ball.value is None and ball.peak is None)

# Rising: the recommended 10 ms attack, so 63% of a step arrives in 10 ms.
ball = smeter.Ballistics()
ball.update(-120.0, 0.05)
ball.update(-90.0, smeter.Ballistics().attack)
check("a step up is 63% of the way there after the 10 ms attack time",
      abs(ball.value - (-120.0 + 30.0 * (1.0 - 1.0 / math.e))) < 0.2,
      f"{ball.value:.2f} dB of a 30 dB step")
ball = smeter.Ballistics()
ball.update(-120.0, 0.05)
for _ in range(50):
    ball.update(-90.0, 0.001)
check("... and five time constants later is within 0.2 dB of it",
      abs(ball.value + 90.0) < 0.25, f"{ball.value:.2f} dB after 50 ms")

# Falling: the recommended decay is no faster than 500 ms.
ball = smeter.Ballistics()
ball.update(-90.0, 0.05)
ball.update(-150.0, ball.decay)
check("a step down is 63% of the way there after the 500 ms decay time",
      abs(ball.value - (-150.0 + 60.0 / math.e)) < 0.2,
      f"{ball.value:.2f} dB after 0.5 s of a 60 dB drop")
ball = smeter.Ballistics()
ball.update(-90.0, 0.05)
for _ in range(45):                      # 45 x 45 ms: one window repaint rate
    ball.update(-150.0, 0.045)
check("the needle is still visibly falling two seconds after the signal went",
      -150.0 < ball.value < -105.0, f"{ball.value:.1f} dB of -150 dBm")
check("the fall is slower than the rise, as a meter's is",
      smeter.Ballistics().decay > 10 * smeter.Ballistics().attack,
      f"attack {smeter.Ballistics().attack:g} s, decay {smeter.Ballistics().decay:g} s")

# Peak hold: a spike between repaints must still be seen on the peak needle.
ball = smeter.Ballistics()
ball.update(-100.0, 0.045)
for _ in range(20):
    ball.update(-70.0 if _ == 3 else -100.0, 0.001)
check("a spike shorter than the needle's own rise is still caught by the peak",
      abs(ball.peak + 70.0) < 0.01, f"peak {ball.peak:.2f} of a -70 dBm spike")
value_at_hold = ball.value
for _ in range(49):                      # 490 ms: still inside the half second
    ball.update(-100.0, 0.01)
check("the peak is held for the half second Icom documents",
      abs(ball.peak + 70.0) < 1.0 and abs(ball.value - value_at_hold) > 0.0,
      f"peak {ball.peak:.2f} dB after 0.49 s")
for _ in range(60):                      # and then it starts to fall away
    ball.update(-100.0, 0.01)
check("and then it falls away towards the needle",
      ball.peak > ball.value + 5.0 and ball.peak < -71.0,
      f"peak {ball.peak:.1f} vs needle {ball.value:.1f} dBm")
ball = smeter.Ballistics()
rng = random.Random(7)
levels = [-90.0] + [rng.uniform(-140.0, -60.0) for _ in range(300)]
lowest = 99.0
for target in levels:
    value, peak = ball.update(target, 0.045)
    lowest = min(lowest, peak - value)
check("the peak needle never reads below the needle -- it is a maximum",
      lowest >= -1e-9, f"worst {lowest:+.4f} dB under it")
check("and it always holds the highest level of the last half second",
      all(ball.peak >= t - 1e-9 for t in levels[-11:]),
      f"peak {ball.peak:.1f} dBm against {min(levels[-11:]):.1f}.."
      f"{max(levels[-11:]):.1f} in the last 0.5 s")

# ------------------------------------------------------------- themes
# A colour scheme is a table and a style sheet built from it, with nothing of
# Qt in either (see rspsdr/theme.py), so what can be checked here is what
# matters about the table: that every scheme is complete, that every colour
# parses, and that every scheme is *readable* -- the text against the window,
# and the trace against the plot it is drawn on.  A scheme that cannot be read
# is a bug the eye catches slowly and a number catches at once.
print("\n== window themes ==")
from rspsdr import theme  # noqa: E402


def _luminance(colour: str) -> float:
    """Relative luminance, as WCAG defines it (sRGB, linearised)."""
    parts = []
    for value in theme.rgb(colour):
        u = value / 255.0
        parts.append(u / 12.92 if u <= 0.03928
                     else ((u + 0.055) / 1.055) ** 2.4)
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def _contrast(a: str, b: str) -> float:
    """How many times brighter one colour is than the other, +1 either way."""
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


check("there is more than one scheme, and the default is one of them",
      len(theme.names()) >= 3 and theme.DEFAULT in theme.names(),
      theme.describe())
check("every scheme is offered once and has a name to show",
      len(set(theme.names())) == len(theme.names())
      and all(theme.label(n) and theme.note(n) for n in theme.names()),
      ", ".join(f"{theme.label(n)} ({theme.note(n)})" for n in theme.names()))

incomplete = []
bad_colour = []
for _name in theme.names():
    _pal = theme.palette(_name)
    for _key in theme.KEYS:
        if _key not in _pal:
            incomplete.append(f"{_name}.{_key}")
    for _key in theme.KEYS:
        _value = _pal.get(_key)
        if _key == "grid_alpha":
            if not (isinstance(_value, float) and 0.0 < _value <= 1.0):
                bad_colour.append(f"{_name}.{_key}={_value!r}")
            continue
        if not (isinstance(_value, str) and len(_value) == 7
                and _value.startswith("#")):
            bad_colour.append(f"{_name}.{_key}={_value!r}")
            continue
        try:
            if len(theme.rgb(_value)) != 3:
                bad_colour.append(f"{_name}.{_key}={_value!r}")
        except ValueError:
            bad_colour.append(f"{_name}.{_key}={_value!r}")
check(f"every scheme sets all {len(theme.KEYS)} fields",
      not incomplete, ", ".join(incomplete[:6]) or
      " ".join(theme.KEYS))
check("and every colour in it is a #rrggbb that parses",
      not bad_colour, ", ".join(bad_colour[:6]))
check("an unknown scheme is the default rather than an error",
      theme.palette("no-such-theme") == theme.palette(theme.DEFAULT),
      f"{theme.label('no-such-theme')} -- "
      f"{theme.note('no-such-theme') or 'no note'}")
check("and the default scheme is what the window actually paints",
      theme.palette(theme.DEFAULT)["plot"]
      == theme.THEMES[theme.DEFAULT]["plot"],
      theme.palette(theme.DEFAULT)["plot"])

# The floors are readability, not taste: 7:1 is WCAG's AAA body text, 4.5:1
# its AA, and 3:1 the least that a large mark may be seen at all.  Every
# scheme clears them, and a scheme added later has to as well.
READABILITY = {
    ("text", "window"): (7.0, "ordinary text on the window"),
    ("bright", "window"): (7.0, "the application's name"),
    ("muted", "window"): (3.0, "the secondary readouts"),
    ("text", "field"): (7.0, "text inside an input"),
    ("text", "panel"): (7.0, "text on a raised panel"),
    ("accent", "plot"): (4.5, "the spectrum trace"),
    ("peak", "plot"): (3.0, "the peak-hold trace"),
    ("marker", "plot"): (3.0, "the tuning marker"),
    ("band", "plot"): (2.0, "the passband wash"),
    ("grid", "plot"): (1.2, "the grid (visible, but not the subject)"),
    ("highlight_text", "highlight"): (4.5, "selected text"),
}
faint = []
muted_grid = []
for _name in theme.names():
    _pal = theme.palette(_name)
    for (_a, _b), (_floor, _what) in READABILITY.items():
        _got = _contrast(_pal[_a], _pal[_b])
        if _got < _floor:
            faint.append(f"{_name}: {_what} {_got:.2f}:1 < {_floor}")
    # A grid that shouts louder than the trace it is drawn under is worse than
    # no grid: this is the one relationship between two contrasts that has to
    # hold the right way round, not just above a floor.
    if _contrast(_pal["grid"], _pal["plot"]) >= _contrast(_pal["accent"],
                                                          _pal["plot"]):
        muted_grid.append(_name)
check("every scheme is readable: text, trace, marker and grid",
      not faint, "; ".join(faint) or
      f"{len(theme.names())} schemes x {len(READABILITY)} pairings")
def _pairing(name: str) -> str:
    """How one scheme's grid compares with its own trace, for the report."""
    pal = theme.palette(name)
    return (f"{_contrast(pal['grid'], pal['plot']):.2f}"
            f"<{_contrast(pal['accent'], pal['plot']):.2f}")


check("and no scheme draws its grid louder than its trace",
      not muted_grid, ", ".join(muted_grid) or
      " ".join(f"{name} {_pairing(name)}" for name in theme.names()))

# The controls are styled from these; the rest of a scheme is painted by the
# window itself (the plots' pens, the passband wash, the buttons that carry a
# meaning), so a scheme colour is not required to appear in the style sheet.
SHEET_KEYS = ("window", "panel", "field", "text", "muted", "bright",
              "accent", "border", "highlight", "highlight_text")
sheets = {name: theme.stylesheet(theme.palette(name)) for name in theme.names()}
check("the style sheet built for every scheme is balanced and non-trivial",
      all(s.count("{") == s.count("}") and len(s) > 500
          for s in sheets.values()),
      ", ".join(f"{n} {len(s)} chars / {s.count('{')} blocks"
                for n, s in sheets.items()))
check("and it names every colour the controls are styled with",
      all(all(str(theme.palette(name)[k]) in sheet for k in SHEET_KEYS)
          for name, sheet in sheets.items()),
      f"{len(SHEET_KEYS)} colours in each of {len(sheets)} sheets, "
      f"{len(next(iter(sheets.values())))} chars each")
check("with nothing left unfilled in it",
      all("None" not in s and "{{" not in s and "}}" not in s
          for s in sheets.values()),
      f"no unfilled values, no doubled braces, in {len(sheets)} sheets")
check("the style sheet never styles a bare QWidget",
      all("QWidget {" not in sheet and "QWidget{" not in sheet
          for sheet in sheets.values()),
      "a blanket rule reaches the plots and paints over their own background")
check("two schemes are actually different from each other",
      len({tuple(sorted(theme.palette(n).items())) for n in theme.names()})
      == len(theme.names()),
      ", ".join(f"{n}={theme.palette(n)['plot']}" for n in theme.names()))
check("rgb() turns a colour into the three bytes Qt wants",
      theme.rgb("#7fe7ff") == (0x7f, 0xe7, 0xff)
      and theme.rgb("000000") == (0, 0, 0),
      f"{theme.rgb('#7fe7ff')}, {theme.rgb('000000')}")

# -------------------------------------------------------------- zoom ladder
# The zoom ladder and the FFT list are tied together by arithmetic: the window
# on show is the receiver span divided by the zoom, the FFT divides that span
# into bins, so the bins left across the window are the FFT size divided by the
# zoom.  Keep the deepest step four bins wide at the smallest transform and the
# picture is never a single column stretched across the plot -- which looks like
# a signal and is not one.  Both tables live in rspsdr/ui.py, which imports Qt;
# importing it needs no display, and none of the numbers below do.
print("\n== zoom ladder ==")
from rspsdr import ui  # noqa: E402

steps = [int(val) for _, val in ui.ZOOMS]
check("the zoom ladder is a run of doubling steps from 1x",
      steps[0] == 1 and steps == [2 ** i for i in range(len(steps))]
      and ui.ZOOM_MAX == steps[-1],
      f"{len(steps)} steps, 1x to {ui.ZOOM_MAX}x")
check("the deepest step still has bins to draw at the smallest FFT",
      ui.FFTS[0] // ui.ZOOM_MAX >= 4,
      f"{ui.FFTS[0]} bins over {ui.ZOOM_MAX}x = "
      f"{ui.FFTS[0] / ui.ZOOM_MAX:.0f} bins across the window")
# The readout names the window in Hz, kHz or MHz.  At the lowest sample rate on
# offer the deepest step still has to be a real width rather than "0 Hz".
lowest_rate = min(rate for _, rate in ui.RATES)
highest_rate = max(rate for _, rate in ui.RATES)
check("and the window is still a real width at the lowest sample rate",
      lowest_rate / ui.ZOOM_MAX >= 1.0,
      f"{lowest_rate/1e3:g} kSps at {ui.ZOOM_MAX}x = "
      f"{lowest_rate/ui.ZOOM_MAX:.0f} Hz, "
      f"{highest_rate/ui.ZOOM_MAX/1e3:.1f} kHz at {highest_rate/1e6:g} MSps")

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
