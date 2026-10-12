"""End-to-end receive chain, with no radio and no window attached.

These tests drive ``Radio._process_frame`` -- the same function the streaming
thread calls -- with synthetic IQ, so the whole path is exercised: the
downconverter, the spectrum, the mode filter, the demodulator and the audio
callback.  Every sample rate the receiver offers is checked, because a hop is
sized for the demodulator and not for the FFT, and the two collide at the low
rates: at 62.5 kSps a hop holds 2500 IQ samples, which is less than a single
4096-point FFT, so no spectrum frame was ever produced there and both displays
stayed blank.

Run with:  python3 tests/test_chain.py
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rspsdr.dsp import AUDIO_RATE  # noqa: E402
from rspsdr.radio import Radio, Settings  # noqa: E402

failures: list[str] = []

# Every rate the GUI offers, including both ends of it.
CHAIN_RATES = [62500, 96000, 125000, 192000, 250000, 384000, 500000, 768000,
               1000000, 1536000, 2000000, 2048000, 4000000, 10000000]

TUNED = 10_000_000.0


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" :: {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def peak_freq(x, rate, fmin=60.0):
    """Dominant frequency of a real signal, in Hz."""
    x = np.asarray(x, np.float64)
    if x.size < 1024:
        return 0.0
    w = np.hanning(x.size)
    sp = np.abs(np.fft.rfft(x * w))
    f = np.arange(sp.size) * rate / x.size
    sp = sp.copy()
    sp[f < fmin] = 0.0
    return float(f[int(np.argmax(sp))])


def rms(v):
    v = np.asarray(v, np.float64)
    return float(np.sqrt(np.mean(v ** 2))) if v.size else 0.0


class _Sink:
    """Stands in for AudioOutput; collects the demodulated audio."""

    volume = 1.0
    muted = False

    def __init__(self):
        self.blocks = []

    def write(self, audio):
        self.blocks.append(np.asarray(audio, np.float32).copy())

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


def build(rate, n_fft=4096, **kw):
    opts = dict(center_freq=TUNED, tuned_freq=TUNED, n_fft=n_fft,
                sample_rate=rate, audio_agc=False)
    opts.update(kw)
    radio = Radio(Settings(**opts))
    radio._build_dsp()
    radio._actual_center = TUNED
    spectra = []
    radio.on_spectrum = lambda c, db, sr: spectra.append(np.asarray(db).copy())
    radio.audio = _Sink()
    return radio, spectra


def run(radio, iq, hop, hops):
    """Feed the chain as the streaming thread does and return the audio."""
    for i in range(hops):
        radio._process_frame(iq[i * hop:(i + 1) * hop])
    blocks = radio.audio.blocks
    return np.concatenate(blocks) if blocks else np.zeros(0, np.float32)


def am_iq(rate, n, offset, tone=1000.0, depth=0.5, amp=0.3):
    t = np.arange(n) / rate
    return (amp * (1.0 + depth * np.cos(2 * math.pi * tone * t))
            * np.exp(2j * math.pi * offset * t)).astype(np.complex64)


print("== every sample rate produces spectrum frames ==")
for rate in CHAIN_RATES:
    hops = 40
    for n_fft in ((4096, 16384) if rate <= 250000 else (4096, 8192)):
        radio, spectra = build(rate, n_fft, mode="am", filter_bw=6000.0)
        hop = radio.ddc.hop_in
        # Enough hops for the FFT to complete, with a few to spare.
        n_hops = max(hops, int(3.0 * n_fft / hop) + 4)
        iq = am_iq(rate, hop * n_hops, 0.0)
        run(radio, iq, hop, n_hops)
        # With a 4096-point FFT at 2 MSps one hop holds nineteen segments, so a
        # frame per hop is expected; at 62.5 kSps it takes several hops to fill
        # one segment, and the requirement is simply that they keep coming.
        least = 1 if n_fft > hop else n_hops // 2
        got = len(spectra)
        if got < least:
            break
    check(f"{rate/1e6:g} MSps, {n_fft}-point FFT, hop {hop}"
          f"{' (> one FFT)' if hop < n_fft else ''}",
          got >= least and all(s.size == n_fft for s in spectra),
          f"{got} spectrum frames, wanted {least}")

print("\n== the spectrum puts the carrier in the right bin ==")
for rate in (62500, 250000, 2000000, 10000000):
    for offset in (0.0, 0.25 * 62500.0, -0.25 * 62500.0):
        n_fft = 4096
        radio, spectra = build(rate, n_fft, mode="am", filter_bw=6000.0)
        hop = radio.ddc.hop_in
        n_hops = max(24, int(4.0 * n_fft / hop) + 4)
        iq = am_iq(rate, hop * n_hops, offset, depth=0.0)
        run(radio, iq, hop, n_hops)
        if not spectra:
            break
        db = spectra[-1]
        step = rate / n_fft
        f_peak = TUNED + (int(np.argmax(db)) - n_fft // 2) * step
        if abs(f_peak - (TUNED + offset)) > 2.0 * step:
            break
    check(f"carrier at {offset:+.0f} Hz lands within two bins at "
          f"{rate/1e6:g} MSps",
          abs(f_peak - (TUNED + offset)) <= 2.0 * step,
          f"peak {f_peak - TUNED:+.0f} Hz, wanted {offset:+.0f} Hz "
          f"(bin {step:.1f} Hz)")

print("\n== every mode demodulates a real signal ==")
MODE_TONE = {"am": 1000.0, "sam": 1000.0, "usb": 1500.0, "lsb": 1500.0,
             "cw": 700.0, "nfm": 1000.0}
for mode, want in MODE_TONE.items():
    rate, n_fft = 2000000, 4096
    bw = {"am": 6000.0, "sam": 6000.0, "usb": 2400.0, "lsb": 2400.0,
          "cw": 500.0, "nfm": 8000.0}[mode]
    radio, spectra = build(rate, n_fft, mode=mode, filter_bw=bw)
    hop = radio.ddc.hop_in
    hops = 30
    n = hop * hops
    t = np.arange(n) / rate
    if mode in ("am", "sam"):
        iq = am_iq(rate, n, 0.0, tone=want, amp=0.4)
    elif mode == "usb":
        iq = (0.3 * np.exp(2j * math.pi * want * t)).astype(np.complex64)
    elif mode == "lsb":
        iq = (0.3 * np.exp(-2j * math.pi * want * t)).astype(np.complex64)
    elif mode == "cw":
        iq = (0.3 * np.exp(2j * math.pi * 0.0 * t)).astype(np.complex64)
    else:  # nfm -- a carrier swinging +/-1 kHz
        iq = (0.3 * np.exp(2j * math.pi * want / (2 * math.pi)
                           * np.sin(2 * math.pi * 700.0 * t))).astype(np.complex64)
    audio = run(radio, iq, hop, hops)
    expect = want if mode != "nfm" else 700.0
    got = peak_freq(audio[AUDIO_RATE // 10:], AUDIO_RATE)
    ok = (audio.size == hops * radio.ddc.hop_out and bool(np.all(np.isfinite(audio)))
          and rms(audio[len(audio) // 4:]) > 0.01
          and abs(got - expect) < 30.0)
    check(f"{mode.upper()} produces {expect:.0f} Hz audio",
          ok, f"{audio.size} samples, rms {rms(audio[len(audio) // 4:]):.3f}, "
              f"tone {got:.0f} Hz")

print("\n== a changing sample rate does not starve the display ==")
# The reported bug in one test: start at 2 MSps, drop to 62.5 kSps the way the
# GUI's rate list does, and require frames to keep arriving at both.
radio = Radio(Settings(center_freq=TUNED, tuned_freq=TUNED, mode="am",
                       filter_bw=6000.0, sample_rate=2000000))
radio._build_dsp()
radio._actual_center = TUNED
counts = []
for rate in (2000000, 62500, 96000, 125000, 2000000):
    radio.apply(sample_rate=rate)          # not running: rebuilds in place
    radio._build_dsp()
    spectra = []
    radio.on_spectrum = lambda c, db, sr: spectra.append(1)
    radio.audio = _Sink()
    hop = radio.ddc.hop_in
    hops = 40
    iq = am_iq(rate, hop * hops, 0.0)
    run(radio, iq, hop, hops)
    counts.append((rate, len(spectra)))
check("frames keep arriving at every rate after a rate change",
      all(n > 0 for _, n in counts),
      ", ".join(f"{r/1e6:g} MSps {n}" for r, n in counts))

print("\n== the demodulator's level meter and the spectrum agree ==")
rate, n_fft = 2000000, 4096
for amp in (0.5, 0.25, 0.125):
    radio, spectra = build(rate, n_fft, mode="am", filter_bw=6000.0)
    hop = radio.ddc.hop_in
    hops = 24
    iq = am_iq(rate, hop * hops, 0.0, depth=0.0, amp=amp)
    run(radio, iq, hop, hops)
    db_peak = float(spectra[-1].max())
    if amp == 0.5:
        first_spec, first_meter = db_peak, radio.demod.signal_db
    last_spec, last_meter = db_peak, radio.demod.signal_db
check("both meters move 6 dB for a halving of amplitude",
      abs((first_spec - last_spec) - 12.0) < 1.0
      and abs((first_meter - last_meter) - 12.0) < 1.0,
      f"spectrum {first_spec:.1f} -> {last_spec:.1f} dB, "
      f"meter {first_meter:.1f} -> {last_meter:.1f} dB "
      f"(two halvings = 12 dB)")

print("\n== the level the signal meter reads ==")
# The meter is fed ``Demodulator.passband_db`` -- the level inside the channel
# filter, taken before the noise reduction, the AGC, the squelch and the volume.
# That is what makes it a measurement of what arrived at the receiver rather
# than of how the user is listening, and every part of that claim is checkable
# here: the level has to follow the carrier faithfully, and none of the
# listening controls may move it.
from rspsdr import smeter  # noqa: E402


def meter_run(amp, interferer=0.0, **kw):
    """(passband level, whole-baseband level, audio) for a carrier."""
    opts = dict(nr=False, nr_level=50.0, squelch_db=-140.0, volume=0.6,
                audio_agc=False)
    opts.update(kw)
    radio, _spectra = build(2000000, mode="am", filter_bw=6000.0, **opts)
    hop = radio.ddc.hop_in
    hops = 24
    n = hop * hops
    iq = am_iq(2000000, n, 0.0, depth=0.0, amp=amp)
    if interferer:
        t = np.arange(n) / 2000000.0
        iq = iq + (interferer
                   * np.exp(2j * math.pi * 18000.0 * t)).astype(np.complex64)
    audio = run(radio, iq, hop, hops)
    return radio.demod.passband_db, radio.demod.signal_db, audio


loud = [meter_run(amp)[0] for amp in (0.4, 0.2, 0.1)]
steps = [loud[i] - loud[i + 1] for i in range(2)]
check("the level falls 6.02 dB for every halving of the carrier",
      all(abs(step - 6.0206) < 0.05 for step in steps),
      ", ".join(f"{step:.3f} dB" for step in steps))
# ... and the needle is the standard's scale on top of it: one unit per 6 dB.
units = [smeter.s_for_dbm(smeter.dbm_for_dbfs(_l)) for _l in loud]
check("so the needle falls exactly one S unit for a halving",
      all(abs((units[i] - units[i + 1]) - 1.0) < 0.01 for i in range(2)),
      ", ".join(f"S{u:.2f}" for u in units))
check("over S9 the face counts dB over S9, as the standard's steps do",
      smeter.reading(smeter.dbm_for_dbfs(loud[1]))
      == f"S9 +{smeter.dbm_for_dbfs(loud[1]) - smeter.S9_DBM:.0f} dB",
      f"{smeter.reading(smeter.dbm_for_dbfs(loud[1]))} at {loud[1]:.2f} dBFS")
# A weak carrier, the other end of the dial, where the face reads in S units.
faint = [meter_run(amp)[0] for amp in (0.005, 0.0025, 0.00125)]
faint_units = [smeter.s_for_dbm(smeter.dbm_for_dbfs(v)) for v in faint]
check("below S9 the same 6 dB is one whole S unit on the face",
      all(abs((faint[i] - faint[i + 1]) - 6.0206) < 0.05 for i in range(2))
      and all(abs((faint_units[i] - faint_units[i + 1]) - 1.0) < 0.01
              for i in range(2))
      and all(smeter.reading(smeter.dbm_for_dbfs(v)) == f"S{u:.1f}"
              for v, u in zip(faint, faint_units)),
      ", ".join(f"{v:.2f} dBFS {smeter.reading(smeter.dbm_for_dbfs(v))}"
                for v in faint))

plain = loud[1]
for name, kw in (("the noise reduction", dict(nr=True, nr_level=80.0)),
                 ("the audio AGC", dict(audio_agc=True)),
                 ("the volume", dict(volume=0.02)),
                 ("the squelch", dict(squelch_db=-10.0))):
    got, _sig, audio = meter_run(0.2, **kw)
    quiet = not audio.size or float(np.max(np.abs(audio))) == 0.0
    check(f"... and does not move when {name} changes",
          abs(got - plain) < 1e-9,
          f"{got:.3f} dBFS against {plain:.3f} dBFS with "
          f"{'silent audio' if quiet else 'audio playing'}")
# The squelch is the sharpest case of the same point: pressed shut, this
# receiver makes no sound at all, and the meter still shows the signal that is
# arriving -- which is the whole reason the level is taken where it is.
muted = meter_run(0.2, squelch_db=-10.0)
check("a squelched receiver is silent and still reads its signal",
      float(np.max(np.abs(muted[2]))) == 0.0 and abs(muted[0] - plain) < 1e-9,
      f"{muted[0]:.3f} dBFS ({smeter.reading(smeter.dbm_for_dbfs(muted[0]))}) "
      f"while the audio is silent")

# A carrier outside the passband is the other half of "inside the filter": the
# whole-baseband level moves, the meter does not.
here = meter_run(0.1)
there = meter_run(0.1, interferer=0.6)
check("a carrier 18 kHz away moves the baseband level but not the meter's",
      (there[1] - here[1]) > 10.0 and abs(there[0] - here[0]) < 0.05,
      f"baseband {here[1]:.1f} -> {there[1]:.1f} dB, meter "
      f"{here[0]:.2f} -> {there[0]:.2f} dBFS")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all chain checks passed")
