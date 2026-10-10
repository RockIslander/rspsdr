"""Verification tests for the DSP core.

Run with:  python3 -m pytest tests/test_dsp.py -q      (or plain python3)
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rspsdr.dsp import (  # noqa: E402
    AUDIO_RATE, Demodulator, Downconverter, NoiseReducer, plan_chain, to_int16,
)

RSP1_RATES = [62500, 96000, 125000, 192000, 250000, 384000, 500000, 768000,
              1000000, 1536000, 2000000, 2048000, 3000000, 4000000, 5000000,
              6000000, 7000000, 8000000, 9000000, 10000000]

failures: list[str] = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' :: ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def tone(fs, freq, n, amp=0.5, phase=0.0):
    t = (np.arange(n, dtype=np.float64) + 0.0) / fs
    return (amp * np.exp(1j * (2 * math.pi * freq * t + phase))).astype(np.complex64)


def peak_freq(x, rate, fmin=0.0):
    """Dominant frequency of a signal, in Hz (signed for complex input)."""
    n = x.size
    w = np.hanning(n)
    if np.iscomplexobj(x):
        sp = np.abs(np.fft.fftshift(np.fft.fft(x * w)))
        fs_ = np.arange(n) * rate / n - rate / 2
    else:
        sp = np.abs(np.fft.rfft(np.asarray(x, np.float64) * w))
        fs_ = np.arange(sp.size) * rate / n
    sp = sp.copy()
    sp[np.abs(fs_) < fmin] = 0.0
    k = int(np.argmax(sp))
    return float(fs_[k]), sp


def best_scale(ref, got):
    """Least-squares complex gain mapping ref -> got (absorbs filter delay)."""
    denom = np.vdot(ref, ref)
    if abs(denom) < 1e-30:
        return 0.0 + 0.0j
    return np.vdot(ref, got) / denom


print("== chain planning ==")
for fs in RSP1_RATES:
    stages, hop_in, hop_out = plan_chain(fs)
    # hop must be exact, rates must land on 48 kHz, hops must be integral
    rate = float(fs)
    hop = hop_in
    ok = True
    for up, down in stages:
        if hop % down != 0:
            ok = False
        rate = rate * up / down
        hop = hop * up // down
    desc = [("/%d" % dn) if up == 1 else ("x%d/%d" % (up, dn)) for up, dn in stages]
    check("plan %.6g MSps -> %s" % (fs / 1e6, desc),
          ok and abs(rate - AUDIO_RATE) < 1e-6 and hop == hop_out,
          "hop_in=%d hop_out=%d" % (hop_in, hop_out))

print("\n== tone pass-through / block continuity ==")
for fs in (250000, 1000000, 2000000, 4000000):
    dc = Downconverter(fs)
    hop = dc.hop_in
    nblocks = 30
    total = hop * nblocks
    offset = 12345.0            # DDC mix offset in Hz
    base = 1000.0               # resulting baseband tone
    x = tone(fs, offset + base, total)

    dcA = Downconverter(fs)
    outA = [dcA.process(x[i * hop:(i + 1) * hop], offset) for i in range(nblocks)]
    chunked = np.concatenate(outA)

    # Feeding the identical stream as one big call must give the identical
    # result.  Any bookkeeping error in the stateful resampler (grid drift,
    # lost history) shows up here immediately.
    dcB = Downconverter(fs)
    single = dcB.process(x, offset)
    m = min(chunked.size, single.size)
    err = float(np.max(np.abs(chunked[:m] - single[:m])))
    check("%.6g MSps block-stateful == one-shot" % (fs / 1e6),
          m == chunked.size == single.size and err < 1e-4,
          "n=%d/%d maxdiff=%.2e" % (chunked.size, single.size, err))

    # ... and must agree with a plain scipy resampler up to constant delay
    t = np.arange(total) / fs
    ref = resample_poly(x * np.exp(-2j * math.pi * offset * t),
                        AUDIO_RATE, int(fs)).astype(np.complex64)
    a, b = ref[4000:m], chunked[4000:m]
    scale = best_scale(a, b)
    resid = float(np.max(np.abs(b - scale * a)))
    rmsref = float(np.sqrt(np.mean(np.abs(a) ** 2)))
    check("%.6g MSps matches scipy reference" % (fs / 1e6),
          resid < 0.15 * rmsref and abs(abs(scale) - 1.0) < 0.02,
          "resid=%.2e (-%.0f dB) |gain|=%.4f"
          % (resid, -20 * math.log10(resid / rmsref + 1e-30), abs(scale)))

    # frequency must be preserved exactly
    f_out, _ = peak_freq(chunked[20000:], AUDIO_RATE)
    check("%.6g MSps tone frequency preserved" % (fs / 1e6),
          abs(f_out - base) < 2.0, "got %.2f Hz want %g" % (f_out, base))

    # continuity: no sample-to-sample jump at block boundaries
    bounds = np.cumsum([o.size for o in outA])[:-1]
    step = np.abs(np.diff(chunked))
    inside = float(np.median(step))
    at_boundary = step[bounds - 1] if bounds.size else np.array([0.0])
    check("%.6g MSps block boundaries continuous" % (fs / 1e6),
          float(np.max(at_boundary)) < 8 * inside + 1e-6,
          "boundary max=%.4f typical step=%.4f"
          % (float(np.max(at_boundary)), inside))

print("\n== alias rejection ==")
fs = 2000000
dc = Downconverter(fs)
hop = dc.hop_in
n = hop * 20
# a strong tone well outside the audio passband must not fold into it
interferer = tone(fs, 900000.0, n, amp=0.9)
out = np.concatenate([dc.process(interferer[i * hop:(i + 1) * hop], 0.0)
                      for i in range(20)])
tail = out[20000:]
db = 20 * math.log10(float(np.sqrt(np.mean(np.abs(tail) ** 2))) + 1e-20)
check("out-of-band tone is rejected", db < -70, f"leak level {db:.1f} dB")

print("\n== demodulators ==")


def demod_test(mode, bw, mod_hz, expect_tone):
    """The demod input is complex baseband with the carrier already at DC."""
    rate, n = AUDIO_RATE, 48000
    t = np.arange(n) / rate
    if mode in ("am", "sam"):
        x = (1.0 + 0.5 * np.cos(2 * math.pi * mod_hz * t)).astype(np.complex64)
    elif mode == "nfm":
        # carrier at DC, +/-400 Hz deviation swinging at mod_hz
        x = np.exp(2j * math.pi * (0.4 * np.sin(2 * math.pi * mod_hz * t))).astype(np.complex64)
    elif mode == "usb":
        x = (0.5 * np.exp(2j * math.pi * mod_hz * t)).astype(np.complex64)
    elif mode == "lsb":
        x = (0.5 * np.exp(-2j * math.pi * mod_hz * t)).astype(np.complex64)
    else:  # cw: carrier already offset to the +pitch position by the DDC
        x = (0.5 * np.exp(2j * math.pi * 700.0 * t)).astype(np.complex64)
    d = Demodulator(mode, bw=bw, pitch=700.0, agc=False)
    y = d(x)
    seg = y[8000:]
    rms = float(np.sqrt(np.mean(seg ** 2)))
    f_out, _ = peak_freq(seg, rate, fmin=60.0)
    ok = rms > 0.05 and abs(f_out - expect_tone) < 25
    check("demod %s" % mode.upper(), ok,
          "rms=%.3f peak=%.0fHz (want %g)" % (rms, f_out, expect_tone))
    return y, seg


demod_test("am", 6000, 1000.0, 1000)
demod_test("sam", 6000, 1000.0, 1000)
demod_test("usb", 2400, 1500.0, 1500)
demod_test("lsb", 2400, 1500.0, 1500)
demod_test("cw", 500, 0.0, 700)
demod_test("nfm", 8000, 1000.0, 1000)

print("\n== ssb sideband rejection ==")
n, rate = 48000, AUDIO_RATE
t = np.arange(n) / rate
u = (0.5 * np.exp(2j * math.pi * 1500 * t)).astype(np.complex64)
lo = np.conjugate(u)
r_usb_u = float(np.sqrt(np.mean(Demodulator("usb", bw=2400, agc=False)(u)[8000:] ** 2)))
r_lsb_u = float(np.sqrt(np.mean(Demodulator("lsb", bw=2400, agc=False)(u)[8000:] ** 2)))
r_usb_l = float(np.sqrt(np.mean(Demodulator("usb", bw=2400, agc=False)(lo)[8000:] ** 2)))
r_lsb_l = float(np.sqrt(np.mean(Demodulator("lsb", bw=2400, agc=False)(lo)[8000:] ** 2)))
check("USB takes the upper sideband only", r_usb_u > 20 * r_lsb_u and r_usb_u > 0.2,
      "usb+ =%.3f  usb- =%.3f" % (r_usb_u, r_usb_l))
check("LSB takes the lower sideband only", r_lsb_l > 20 * r_usb_l and r_lsb_l > 0.2,
      "lsb+ =%.3f  lsb- =%.3f" % (r_lsb_u, r_lsb_l))

print("\n== noise reduction ==")


def nr_run(reducer, x, block=1920):
    return np.concatenate([reducer(x[i:i + block])
                           for i in range(0, x.size, block)])


def keyed_tone(n, amp=0.35, hz=1000.0, key=8.0, ramp_ms=5.0):
    """A keyed carrier with a realistic rise/fall -- what real CW sounds like.

    An instantaneous gate would be a wideband click, and the artifact that
    produced would say more about the test signal than about the reducer.
    """
    t = np.arange(n) / AUDIO_RATE
    k = 2 * math.pi * key * ramp_ms / 1000.0
    gate = 0.5 * (1.0 + np.tanh(np.sin(2 * math.pi * key * t) / k))
    return (amp * np.sin(2 * math.pi * hz * t) * gate).astype(np.float32)


def rms(v):
    return float(np.sqrt(np.mean(np.asarray(v, np.float64) ** 2))) if v.size else 0.0


# --- bypass and length bookkeeping -------------------------------------
nr = NoiseReducer(strength=0.7, enabled=False)
probe = np.sin(2 * np.pi * 1000 * np.arange(40000) / AUDIO_RATE).astype(np.float32)
check("NR disabled is an exact pass-through",
      np.array_equal(nr(probe), probe) and nr(probe).size == probe.size)

# --- the transform itself reconstructs exactly --------------------------
# Output sample m is input sample m -- there is no delay in the *content*, the
# reducer only holds the newest samples back.  Aligning by a shift (as an
# earlier version of this test did) silently passes on a periodic tone, so the
# probe here is noise.
nr = NoiseReducer(strength=0.5, enabled=True)
nr._gains = lambda power: np.ones_like(power)      # unity gain, OLA only
probe = np.random.default_rng(11).standard_normal(60000).astype(np.float32)
out = nr(probe)
held = probe.size - out.size
interior = slice(nr.n_fft, out.size)
check("overlap-add reconstructs the input exactly",
      np.max(np.abs(out[interior] - probe[interior])) < 1e-5,
      f"maxdiff={np.max(np.abs(out[interior] - probe[interior])):.2e} "
      f"(held back {held})")
check("held-back count is the transform lookahead",
      nr.latency <= held < nr.n_fft and nr.latency == nr.n_fft - nr.hop,
      f"held={held} latency={nr.latency} n_fft={nr.n_fft}")

nr = NoiseReducer(strength=0.5, enabled=True)
total_in = total_out = 0
lost = False
for block in (1920, 1920, 1920, 997, 4000):
    total_in += block
    total_out += nr(np.zeros(block, dtype=np.float32)).size
    # Whatever is not emitted yet must still be sitting in the input buffer.
    lost = lost or (total_in - total_out != nr._in.size)
check("no sample is lost or duplicated while buffering",
      not lost and 0 <= nr._in.size < nr.n_fft,
      f"in={total_in} out={total_out} buffered={nr._in.size}")

steady = NoiseReducer(strength=0.5, enabled=True)
emitted = steady(np.zeros(10 * steady.hop, dtype=np.float32)).size
check("held-back count is exactly the latency for whole hops",
      10 * steady.hop - emitted == steady.latency,
      f"in={10 * steady.hop} out={emitted} latency={steady.latency}")

# --- a clean signal must pass untouched ---------------------------------
# A length of n_fft + k*hop makes the held-back count exactly the latency, so
# the comparison is unambiguous.
TAIL = slice(4096, None)          # past the accumulator's fade-in


def via_reducer(x, strength):
    reducer = NoiseReducer(strength=strength, enabled=True)
    return reducer(x), reducer


clean = keyed_tone(512 + 1500 * 128)
for strength in (0.25, 0.5, 1.0):
    out, reducer = via_reducer(clean, strength)
    assert clean.size - out.size == reducer.latency
    # out is index-aligned with the input, just shorter by the lookahead.
    seg_in = clean[:out.size][TAIL]
    seg_out = out[TAIL]
    gain = np.vdot(seg_in, seg_out) / np.vdot(seg_in, seg_in)
    resid = rms(seg_out - gain * seg_in) / rms(seg_in)
    check(f"clean keyed carrier untouched at strength {strength}",
          abs(abs(gain) - 1.0) < 1e-3 and resid < 5e-3,
          f"|gain|={abs(gain):.5f} resid={resid:.5f}")

steady_carrier = (0.35 * np.sin(2 * math.pi * 1000 *
                                 np.arange(clean.size) / AUDIO_RATE)).astype(np.float32)
out, _ = via_reducer(steady_carrier, 0.75)
ref = steady_carrier[:out.size][TAIL]
sg = np.vdot(ref, out[TAIL]) / np.vdot(ref, ref)
check("an unkeyed carrier is not treated as noise", abs(abs(sg) - 1.0) < 1e-3,
      f"|gain|={abs(sg):.5f}")

# --- and noisy speech does get cleaned up -------------------------------
rng = np.random.default_rng(3)
noisy = (clean + 0.35 * rng.standard_normal(clean.size)).astype(np.float32)
present = np.abs(clean) > 0.05

results = {}
for strength in (0.0, 0.25, 0.5, 0.75):
    out, _ = via_reducer(noisy, strength)
    c = clean[:out.size][TAIL]
    n = noisy[:out.size][TAIL]
    o = out[TAIL]
    m = present[:out.size][TAIL]
    snr_in = 20 * math.log10(rms(n[m]) / rms((n - c)[m]))
    snr_out = 20 * math.log10(rms(o[m]) / rms((o - c)[m]))
    floor_att = 20 * math.log10(rms(o[~m]) / rms(n[~m]))
    results[strength] = (snr_out - snr_in, floor_att)
    print(f"        strength {strength:.2f}: SNR {snr_in:.1f} -> {snr_out:.1f} dB "
          f"({snr_out - snr_in:+.1f}), noise in the gaps {floor_att:+.1f} dB")

check("SNR improves at full strength",
      results[0.75][0] > 2.0, f"{results[0.75][0]:+.1f} dB")
check("more strength gives more SNR",
      results[0.75][0] > results[0.25][0] > results[0.0][0],
      f"0.25 {results[0.25][0]:+.1f} vs 0.75 {results[0.75][0]:+.1f} dB")
check("the noise between words drops hard",
      results[0.75][1] < -8.0, f"{results[0.75][1]:+.1f} dB")
check("the lowest setting is gentle, not destructive",
      -0.5 > results[0.0][1] > -5.0, f"{results[0.0][1]:+.1f} dB")

print("\n== noise reduction wiring ==")
base = Demodulator("usb", bw=2400, agc=False)
with_nr = Demodulator("usb", bw=2400, agc=False, nr=True, nr_level=0.7)
check("demodulator exposes the reducer",
      with_nr.nr.enabled and not base.nr.enabled and abs(with_nr.nr.strength - 0.7) < 1e-9)
bb = (0.5 * np.exp(2j * math.pi * 1500 * np.arange(48000) / AUDIO_RATE)).astype(np.complex64)
plain = base(bb.copy())
filtered = with_nr(bb.copy())
check("enabling NR changes the audio path",
      plain.size == 48000 and filtered.size == 48000 - with_nr.nr.latency,
      f"plain={plain.size} nr={filtered.size} latency={with_nr.nr.latency}")
check("NR output is still finite audio",
      bool(np.all(np.isfinite(filtered))) and rms(filtered) > 0.01,
      f"rms={rms(filtered):.3f}")

import time as _time  # noqa: E402
reducer = NoiseReducer(strength=0.5, enabled=True)
SECONDS = 10
long_noise = (0.2 * rng.standard_normal(AUDIO_RATE * SECONDS)).astype(np.float32)
t0 = _time.thread_time()
nr_run(reducer, long_noise)
cost = (_time.thread_time() - t0) / SECONDS      # CPU seconds per audio second
print(f"        cost {cost * 1000:.1f} ms per second of audio "
      f"= {cost * 100:.1f}% of one core")
check("noise reduction fits inside the realtime budget", cost < 0.15,
      f"{cost * 100:.1f}% of a core")

print("\n== audio conversion ==")
sig = np.array([0.0, 1.0, -1.0, 2.0, -2.0], np.float32)
raw = to_int16(sig)
vals = np.frombuffer(raw, "<i2")
check("int16 clipping + scaling", list(vals) == [0, 32767, -32767, 32767, -32767],
      str(list(vals)))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all DSP checks passed")
