"""FT8 tests: band presets, slot cutting, the decode list, and the whole chain.

    python3 tests/test_ft8.py            # no radio, no display, no sound card

FT8 is 15-second slots of 8-FSK text, and everything that can go wrong here is
a *timing* or *frequency* mistake rather than a crash: audio cut at the wrong
instant reports every ``dt`` wrong, a decoder fed 48 kHz audio when it expects
12 kHz hears nothing at all, and a signal that has been through a denoiser is
not the same signal jt9 correlates against.  So the checks below are mostly
about *when* and *how fast* audio is, driving synthetic audio through the real
spotter with a clock under the test's control.

The last section is the honest one: WSJT-X's ``ft8sim`` encodes a known message,
the audio is pushed through the spotter exactly as the receiver would deliver
it, and the real ``jt9`` is asked to decode it.  If that passes, the pipeline is
right end to end -- and it is skipped, loudly, when WSJT-X is not installed.

Run with:  python3 tests/test_ft8.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from rspsdr import ft8  # noqa: E402
from rspsdr.dsp import AUDIO_RATE as RADIO_RATE  # noqa: E402
from rspsdr.dsp import Demodulator  # noqa: E402

failures: list[str] = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + (f" :: {detail}" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


def skip(name, why):
    print(f"  SKIP  {name} :: {why}", flush=True)


def tone_level(signal: np.ndarray, freq: float, rate: float) -> float:
    """Amplitude at one frequency, by correlation."""
    idx = np.arange(signal.size)
    return float(2 * np.abs(np.sum(signal * np.exp(-2j * np.pi * freq * idx / rate)))
                 / signal.size)


print("== the band presets ==")
# Everything an operator needs to find FT8 on the air: the dial frequency, not
# the signal frequency.  The signals sit above it, in the passband.
check("there is a preset for every HF band and two above it",
      len(ft8.FT8_BANDS) == 12,
      ", ".join(b.name for b in ft8.FT8_BANDS))
dial_ok = {"160m": 1_840_000, "80m": 3_573_000, "60m": 5_357_000,
           "40m": 7_074_000, "30m": 10_136_000, "20m": 14_074_000,
           "17m": 18_100_000, "15m": 21_074_000, "12m": 24_915_000,
           "10m": 28_074_000, "6m": 50_313_000, "2m": 144_174_000}
wrong = {b.name: b.dial for b in ft8.FT8_BANDS
         if dial_ok.get(b.name) != b.dial}
check("every preset is the standard FT8 dial frequency",
      not wrong, f"wrong: {wrong}" if wrong else "all twelve match WSJT-X")
check("the dials are all distinct", len({b.dial for b in ft8.FT8_BANDS}) == 12)
check("they are in frequency order",
      all(ft8.FT8_BANDS[i].dial < ft8.FT8_BANDS[i + 1].dial
          for i in range(len(ft8.FT8_BANDS) - 1)))
check("every dial sits inside the band it names",
      all(ft8.FT8_BANDS[i].dial > 1_500_000 for i in range(len(ft8.FT8_BANDS)))
      and all(b.dial % 1_000 == 0 for b in ft8.FT8_BANDS))
check("a preset can be looked up by band name",
      ft8.dial_for("20m") == 14_074_000 and ft8.dial_for("2m") == 144_174_000)
check("an unknown band has no dial", ft8.dial_for("11m") is None
      and ft8.dial_for("") is None)
check("the FT8 sub-band fits inside the filter the preset selects",
      ft8.HIGH_HZ < ft8.FILTER_BW and ft8.LOW_HZ > 0.0,
      f"{ft8.LOW_HZ:.0f}-{ft8.HIGH_HZ:.0f} Hz inside {ft8.FILTER_BW:.0f} Hz")

print("\n== the receiver is never told 'ft8' ==")
# The mode button says FT8, the demodulator knows nothing about it, and a mode
# it does not know raises.  So the translation is load-bearing: without it,
# --mode ft8 (or a memory saved in FT8) takes the window down at startup.
raised = False
try:
    Demodulator("ft8")
except ValueError:
    raised = True
check("the demodulator rejects a mode it does not know", raised)
check("FT8 is received as USB", ft8.receiver_mode("ft8") == "usb")
for other in ("am", "sam", "usb", "lsb", "cw", "nfm"):
    check(f"{other} is passed through unchanged", ft8.receiver_mode(other) == other)

print("\n== the slots start on the UTC quarter-minute ==")
check("a slot is fifteen seconds", ft8.SLOT_SECONDS == 15.0)
check("a slot of 12 kHz audio is exactly fifteen seconds",
      ft8.SLOT_SAMPLES == 180_000
      and abs(ft8.SLOT_SAMPLES / ft8.AUDIO_RATE - 15.0) < 1e-9,
      f"{ft8.SLOT_SAMPLES} samples at {ft8.AUDIO_RATE} Hz")
# The arithmetic that decides which samples are kept and which are thrown away.
check("on a boundary, the slot starts now", ft8.next_slot(1_700_000_010.0) == 1_700_000_010.0
      or ft8.next_slot(0.0) == 0.0)
# A microsecond of slack is deliberate: a block that starts a hair *before* a
# boundary (which is how a real slot is caught) must begin the slot now rather
# than sit out another fifteen seconds.
check("just after a boundary, the next one is a slot away",
      ft8.next_slot(45.001) == 60.0, ft8.next_slot(45.001))
check("a microsecond before a boundary counts as being on it",
      ft8.next_slot(44.9999995) == 45.0, ft8.next_slot(44.9999995))
check("mid-slot, the next boundary is the next multiple of 15",
      ft8.next_slot(52.5) == 60.0 and ft8.next_slot(59.9) == 60.0)
check("a hair before a boundary is that boundary, not the one after",
      ft8.next_slot(59.999999) == 60.0 and ft8.next_slot(60.0 - 1e-7) == 60.0)
check("every slot start is a multiple of fifteen",
      all(ft8.next_slot(t) % 15.0 == 0.0
          for t in (0.1, 7.5, 14.9, 15.0, 1000.3, 1_700_000_123.7)))
check("the time to the next slot is never negative and never over a slot",
      all(0.0 <= ft8.slots_until(t) <= 15.0
          for t in (0.0, 0.5, 14.999, 15.0, 3600.0)))
check("a slot is labelled by its UTC time of day",
      ft8.slot_label(45.0) == time.strftime("%H%M%S", time.gmtime(45.0)),
      ft8.slot_label(45.0))

print("\n== reading jt9's output ==")
# Real output, copied from the installed WSJT-X: the decode lines, then the
# lines that must be ignored.
# Copied from the installed jt9, trailing spaces and all: the message runs to
# the end of the line, and the lines around it are not decodes.
JT9_OUTPUT = (
    "143000  -5  0.0 1500 ~  CQ W1ABC FN42                           \n"
    "143000 -17  0.2  847 ~  K1ABC W9XYZ EN37\n"
    "143015 -24 -0.5 2310 ~  CQ <...> FN31\n"
    "143015  -8  1.4  512 ~  W1ABC K1DEF -07 ; W1ABC RR73\n"
    "<DecodeFinished>   0   1        0\n"
    "143030   3  0.0 1500 ~  CQ DX AA1AA FN20\n")
decodes = ft8.parse_decodes(JT9_OUTPUT, dial_hz=14_074_000)
check("the decode lines are read and the rest ignored", len(decodes) == 5,
      f"{len(decodes)} decoded from {len(JT9_OUTPUT.splitlines())} lines")
first = decodes[0]
check("the fields are unpacked in order",
      (first.utc, first.snr, first.dt, first.audio_hz, first.message)
      == ("143000", -5, 0.0, 1500, "CQ W1ABC FN42"),
      first.line())
check("a negative SNR parsing to a positive one would be a field slip",
      decodes[1].snr == -17 and decodes[4].snr == 3,
      f"{decodes[1].snr}, {decodes[4].snr}")
check("a negative dt is kept", decodes[2].dt == -0.5, str(decodes[2].dt))
check("a hashed callsign survives into the message",
      decodes[2].message == "CQ <...> FN31", decodes[2].message)
check("a compound message is kept whole, separator and all",
      decodes[3].message == "W1ABC K1DEF -07 ; W1ABC RR73", decodes[3].message)
check("trailing spaces are trimmed and nothing else is",
      decodes[0].message == "CQ W1ABC FN42", repr(decodes[0].message))
check("the absolute frequency is the dial plus the audio offset",
      first.freq == 14_074_000 + 1500, f"{first.freq:.0f} Hz")
check("the time is HH:MM:SS", first.time == "14:30:00", first.time)
check("junk produces nothing rather than an exception",
      ft8.parse_decodes("") == []
      and ft8.parse_decodes("not a decode\n\n<DecodeFinished> 0 0 0") == [])
check("a decode with a missing field is dropped, not half-read",
      ft8.parse_decodes(" 143000  -5  0.0 ~ CQ W1ABC FN42") == [])

print("\n== the WAV handed to jt9 ==")
slot = (0.4 * np.sin(2 * np.pi * 1000.0 * np.arange(ft8.SLOT_SAMPLES)
                     / ft8.AUDIO_RATE)).astype(np.float32)
data = ft8.to_wav_bytes(slot)
tmp = tempfile.mkdtemp(prefix="rsp1-ft8-wav-")
path = os.path.join(tmp, "probe.wav")
with open(path, "wb") as fh:
    fh.write(data)
with wave.open(path) as fh:
    check("it is mono 16-bit at 12 kHz",
          (fh.getnchannels(), fh.getsampwidth(), fh.getframerate())
          == (1, 2, ft8.AUDIO_RATE),
          f"{fh.getnchannels()} ch, {fh.getsampwidth() * 8}-bit, "
          f"{fh.getframerate()} Hz")
    check("it holds exactly one slot",
          fh.getnframes() == ft8.SLOT_SAMPLES, f"{fh.getnframes()} frames")
    back = np.frombuffer(fh.readframes(fh.getnframes()), "<i2").astype(np.float32)
# The slot is normalised to a fixed peak on purpose: a quiet band must not
# lose its low bits.  What has to survive is the *signal*, so check the tone.
check("the slot is normalised to the peak jt9 is given",
      abs(float(np.max(np.abs(back / 32768.0))) - 0.5) < 0.002,
      f"peak {float(np.max(np.abs(back / 32768.0))):.4f}")
check("... with the tone still at 1 kHz, not shifted by the scaling",
      abs(tone_level(back / 32768.0, 1000.0, ft8.AUDIO_RATE) - 0.5) < 0.01,
      f"{tone_level(back / 32768.0, 1000.0, ft8.AUDIO_RATE):.3f} at 1 kHz")
check("silence is a valid WAV rather than a divide by zero",
      len(ft8.to_wav_bytes(np.zeros(1000, np.float32))) == 44 + 2000)
# One gain for the whole slot: a per-block normalisation would flatten exactly
# the difference between a weak signal and the strong one beside it.
mixed = np.concatenate((0.005 * np.ones(ft8.SLOT_SAMPLES // 2, np.float32),
                        0.5 * np.ones(ft8.SLOT_SAMPLES // 2, np.float32)))
with open(os.path.join(tmp, "mixed.wav"), "wb") as fh:
    fh.write(ft8.to_wav_bytes(mixed))
with wave.open(os.path.join(tmp, "mixed.wav")) as fh:
    m = np.frombuffer(fh.readframes(fh.getnframes()), "<i2").astype(np.float32)
quiet, loud = float(np.mean(np.abs(m[:1000]))), float(np.mean(np.abs(m[-1000:])))
check("a quiet passage stays 100x quieter than a loud one",
      abs((loud / max(quiet, 1e-9)) - 100.0) < 5.0,
      f"ratio {loud / max(quiet, 1e-9):.1f}")

print("\n== the receiver's 48 kHz audio down to the 12 kHz jt9 reads ==")
n = 48_000
t = np.arange(n) / RADIO_RATE
tone = (0.5 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
dec = ft8.Decimator()
out = dec.process(tone)
check("four samples in, one out", dec.out_rate == 12_000.0
      and abs(out.size - n / 4) <= 1, f"{out.size} from {n}")


in_level = tone_level(tone[len(tone) // 2:], 1000.0, RADIO_RATE)
out_level = tone_level(out[len(out) // 2:], 1000.0, dec.out_rate)
check("a 1 kHz tone survives decimation at the same level",
      abs(out_level - in_level) < 0.02 * max(in_level, 0.1),
      f"{in_level:.3f} -> {out_level:.3f}")
high = (0.5 * np.sin(2 * np.pi * 7000.0 * t)).astype(np.float32)
hi_out = ft8.Decimator().process(high)
full = tone_level(hi_out[len(hi_out) // 2:], 5000.0, 12_000.0)
check("a tone above the new Nyquist is filtered out, not folded in",
      full < 0.01, f"5 kHz alias level {full:.4f}")
check("... so it cannot reappear as a 5 kHz alias either",
      tone_level(hi_out[len(hi_out) // 2:], 5000.0, 12_000.0) < 0.01,
      f"5 kHz level {tone_level(hi_out[len(hi_out) // 2:], 5000.0, 12_000.0):.4f}")
# The property slot alignment depends on: sample n of the output must not
# depend on how the input was chopped up.
whole = ft8.Decimator().process(tone)
ragged = ft8.Decimator()
bits = [ragged.process(tone[i:i + size])
        for i, size in ((0, 4000), (4000, 1236), (5236, 33332), (38568, n - 38568))]
piecewise = np.concatenate(bits)
check("the output does not depend on the block sizes it is fed",
      piecewise.size == whole.size
      and float(np.max(np.abs(piecewise - whole))) < 1e-6,
      f"max difference {float(np.max(np.abs(piecewise - whole))):.2e}")
# A block that is not a whole number of output samples keeps its ragged tail
# and so shifts the phase of everything after it -- which is exactly why the
# spotter accumulates its input and only ever decimates whole multiples.
check("a ragged block keeps a ragged tail (so callers must keep it whole)",
      ft8.Decimator().process(tone[:1001]).size == 251,
      f"1001 in -> {ft8.Decimator().process(tone[:1001]).size} out")

print("\n== cutting the audio into slots ==")


class Clock:
    """A clock the test advances, so a slot takes no real time to arrive."""

    def __init__(self, t0: float):
        self.t = float(t0)

    def __call__(self) -> float:
        return self.t


class RecordingDecoder(ft8.FT8Decoder):
    """Stands in for jt9: records what it is handed, answers to order."""

    def __init__(self, answer=None, error=None):
        super().__init__(exe=sys.executable)      # exists, so `available` is true
        self.slots: list[tuple] = []
        self.answer = list(answer or [])
        self.error = error

    def decode(self, slot, when, dial):
        self.slots.append((slot, when, dial))
        if self.error is not None:
            raise self.error
        return list(self.answer)


def feed(spot, clock, audio, block=8192):
    """Hand audio to the spotter in blocks, moving the clock along with it.

    The clock is advanced by exactly the audio's own duration before each
    block is handed over, which is what the receiver does implicitly: the
    newest sample in a block was taken now.  Nothing here sleeps.
    """
    for i in range(0, audio.size, block):
        chunk = np.ascontiguousarray(audio[i:i + block], dtype=np.float32)
        clock.t += chunk.size / RADIO_RATE
        spot.feed(chunk)


SLOT0 = 15.0 * (1_700_000_000 // 15)          # a real slot boundary
clock = Clock(SLOT0 - 3.0)                    # three seconds before it
decoder = RecordingDecoder()
spotter = ft8.FT8Spotter(clock=clock, decoder=decoder, auto_start=False,
                         dial_hz=14_074_000)
check("an unarmed spotter ignores the audio it is given",
      not spotter.armed and spotter.idle_seconds() == float("inf"))
feed(spotter, clock, np.ones(RADIO_RATE, np.float32))
check("... and arms nothing behind the user's back",
      spotter.stats()["slots"] == 0 and decoder.slots == [])

clock.t = SLOT0 - 3.0                       # put the clock back where it was
spotter.arm()
check("arming clears the idle state and the partial slot", spotter.armed
      and spotter.idle_seconds() == float("inf") and spotter.progress() == (False, 0.0))
# Three seconds of an idle band up to the boundary, then signal.  What is left
# in the slot must be the signal, starting at the boundary: this is the check
# that would have caught the audio being cut a block late, which shifts every
# dt the decoder reports.
for _ in range(3):
    feed(spotter, clock, np.zeros(RADIO_RATE, np.float32))
check("audio before a boundary is dropped, not kept as the start of a slot",
      spotter.stats()["queued"] == 0 and decoder.slots == []
      and abs(clock.t - SLOT0) < 1e-6, f"the clock had reached {clock.t!r}")
feed(spotter, clock, np.ones(RADIO_RATE, np.float32))
filling, secs = spotter.progress()
check("a slot begins on the boundary", filling and secs > 0.0,
      f"filling {filling}, {secs:.2f} s in")
check("... and its start is the boundary itself",
      spotter.slot_start() == SLOT0, f"{spotter.slot_start()} vs {SLOT0}")
check("... and it is not queued before it is full", decoder.slots == [])
# The rest of the slot.
feed(spotter, clock, np.ones(14 * RADIO_RATE, np.float32))
check("... and once it is full the next slot has already begun",
      not spotter.progress()[0] and spotter.stats()["queued"] == 1)
check("exactly one slot is queued per fifteen seconds of audio",
      spotter.stats()["queued"] == 1, str(spotter.stats()))
ran = spotter.run_pending()
check("the queued slot is decoded", ran == 1 and len(decoder.slots) == 1,
      f"{ran} slot(s) run, {len(decoder.slots)} recorded")
slot_audio, slot_when, slot_dial = decoder.slots[0]
check("the slot handed over is exactly fifteen seconds long",
      slot_audio.size == ft8.SLOT_SAMPLES, f"{slot_audio.size} samples")
check("it is stamped with the slot it came from",
      slot_when.timestamp() == SLOT0 and slot_dial == 14_074_000,
      f"{slot_when} @ {slot_dial:.0f} Hz")
# Past the decimation filter's own transient (a few tens of samples), every
# sample in the slot must be the signal that started at the boundary.  The
# ringing at the start is the filter, not misalignment.
check("the audio in it starts at the boundary and not a block later",
      float(np.max(np.abs(slot_audio[300:] - 1.0))) < 1e-3
      and abs(float(np.mean(slot_audio)) - 1.0) < 0.01,
      f"mean over the whole slot {float(np.mean(slot_audio)):.4f}")
check("the slot is counted and reported", spotter.stats()["slots"] == 1
      and spotter.stats()["decodes"] == 0)
spotter.disarm()
feed(spotter, clock, np.ones(4 * RADIO_RATE, np.float32))
check("disarming stops the slot filling", spotter.progress() == (False, 0.0)
      and spotter.stats()["queued"] == 0)
check("... and a decode that lands after it is not reported",
      spotter.run_pending() == 0)

# Ten minutes of an unbroken stream, every slot accounted for -- fed the way
# the receiver really feeds it: blocks of 1920 samples (40 ms), and a clock that
# started at an arbitrary point inside a slot, so every boundary is crossed
# part-way through a block.  This is the check that found the worst bug here.
# A slot is 180000 additions of 1/12000 s and drifts by microseconds, and the
# collector used to re-derive each slot's start from that clock: once the audio
# was a hair *past* a boundary, next_slot() answered with the boundary fifteen
# seconds later, so the whole slot was discarded -- and then the next one, over
# and over.  It decoded one slot and then nothing at all, for ever, while the
# strip went on saying it was listening to every one.
IRREGULAR = 1920
long_clock = Clock(SLOT0 - 5.37)
long_decoder = RecordingDecoder()
long_run = ft8.FT8Spotter(clock=long_clock, decoder=long_decoder,
                          auto_start=False, queue_depth=64)
starts: list[float] = []
real_finish = long_run._finish_slot


def note_finish():
    starts.append(long_run.slot_start())
    real_finish()


long_run._finish_slot = note_finish
long_run.arm()
feed(long_run, long_clock, np.zeros(int(5.37 * RADIO_RATE), np.float32),
     block=IRREGULAR)
feed(long_run, long_clock, np.ones(601 * RADIO_RATE, np.float32),
     block=IRREGULAR)
want = [SLOT0 + 15.0 * i for i in range(40)]
check("ten minutes of audio gives forty slots, one per boundary",
      len(starts) == 40 and starts == want,
      f"{len(starts)} slots, first wrong {next((s for s, w in zip(starts, want)
                                              if s != w), None)}")
check("... with no slot dropped and none decoded twice",
      long_run.stats()["dropped"] == 0 and long_run.stats()["queued"] == 40,
      str(long_run.stats()))
# The audio clock is the only other thing that can drift, and it is the reason
# the grid is advanced rather than re-derived: after ten minutes it must still
# agree with the audio that was fed to within a millisecond.
check("... and the slot clock still on the grid to within a millisecond",
      abs(long_run._now - (SLOT0 + 601.0)) < 1e-3,
      f"off by {(long_run._now - SLOT0 - 601.0) * 1e6:.1f} us after ten minutes")
long_run.run_pending()
check("... and each slot holds the signal, not the idle band before it",
      len(long_decoder.slots) == 40
      and all(abs(float(np.mean(audio[1000:])) - 1.0) < 0.01
              for audio, _, _ in long_decoder.slots),
      f"{len(long_decoder.slots)} slots decoded, mean level "
      f"{float(np.mean(long_decoder.slots[0][0])):.3f}")

# Arming in the middle of a slot must not decode the tail of it: a slot is
# stamped with the boundary it started at, so five seconds of one would be
# presented to jt9 as fifteen seconds of a slot that began elsewhere.
mid_clock = Clock(SLOT0 + 5.0)
mid = ft8.FT8Spotter(clock=mid_clock, decoder=RecordingDecoder(),
                     auto_start=False, queue_depth=8)
mid.arm()
feed(mid, mid_clock, np.ones(4 * RADIO_RATE, np.float32), block=IRREGULAR)
check("arming in the middle of a slot waits for the next one",
      not mid.progress()[0] and mid.stats()["queued"] == 0
      and mid._slot_utc == 0.0,
      f"{mid.progress()}, slot {mid._slot_utc}")
feed(mid, mid_clock, np.ones(11 * RADIO_RATE, np.float32), block=IRREGULAR)
check("... and then starts on the boundary, not late",
      mid.slot_start() == SLOT0 + 15.0, f"{mid.slot_start()} vs {SLOT0 + 15.0}")
# But arming a hair *after* a boundary is on it, not fifteen seconds early for
# the next: the microseconds between the boundary and the first sample are not
# worth waiting a slot for.
edge_clock = Clock(SLOT0 + 0.05)
edge = ft8.FT8Spotter(clock=edge_clock, decoder=RecordingDecoder(),
                      auto_start=False, queue_depth=8)
edge.arm()
feed(edge, edge_clock, np.ones(RADIO_RATE, np.float32), block=IRREGULAR)
check("arming just after a boundary catches that slot",
      edge.progress()[0] and edge.slot_start() == SLOT0,
      f"{edge.progress()}, slot {edge.slot_start()}")

# A decoder that cannot keep up must drop its backlog rather than grow it: the
# radio is always in the present, and a slot decoded a minute late is a slot
# that misrepresents the band.
slow_clock = Clock(SLOT0)
slow = ft8.FT8Spotter(clock=slow_clock, decoder=RecordingDecoder(),
                      auto_start=False, queue_depth=1)
slow.arm()
feed(slow, slow_clock, np.ones(45 * RADIO_RATE, np.float32))
check("a decoder that falls behind drops slots instead of queueing them",
      slow.stats()["queued"] == 1 and slow.stats()["dropped"] == 2,
      str(slow.stats()))

print("\n== nothing here may take the receiver down with it ==")
# feed() runs on the radio's DSP thread.  Whatever the decoder path gets wrong,
# an exception there would stop the sound, the spectrum and the tuner.
problems: list[str] = []
guarded = ft8.FT8Spotter(clock=Clock(SLOT0), decoder=RecordingDecoder(),
                         auto_start=False, on_error=problems.append)
guarded.arm()
try:
    guarded.feed(np.array(["not audio"], dtype=object))
    raised = False
except Exception as exc:                                    # pragma: no cover
    raised = True
    check("... the exception escapes (it must not)", False, repr(exc))
check("a block it cannot read is reported, not raised", not raised
      and len(problems) == 1, f"{problems}")
check("... and only once, however often it happens",
      (guarded.feed(np.array([None], dtype=object)) or True)
      and len(problems) == 1, f"{len(problems)} report(s)")

# The decoder itself is optional: without WSJT-X the mode has to keep working
# as a receiver, and say what is missing rather than fail every slot.
missing = ft8.FT8Spotter(clock=Clock(SLOT0), auto_start=False,
                         decoder=ft8.FT8Decoder(exe="/nonexistent/jt9"),
                         on_error=problems.append)
check("a missing decoder is a known state, not an exception",
      not missing.decoder.available and "MISSING" in missing.decoder.describe(),
      missing.decoder.describe())
missing.clock = Clock(SLOT0)
missing.arm()
before = len(problems)
feed(missing, missing.clock, np.ones(16 * RADIO_RATE, np.float32))
check("a slot is still captured without a decoder",
      missing.stats()["queued"] == 1, str(missing.stats()))
missing.run_pending()
check("... and decoding it reports what is missing instead of crashing",
      len(problems) == before + 1 and "MISSING" in problems[-1]
      and missing.stats()["decodes"] == 0,
      problems[-1] if problems else "nothing reported")

print("\n== the whole chain, with WSJT-X at both ends ==")
# ft8sim encodes a known message; the audio is handed to the spotter as if the
# receiver had produced it; the real jt9 decodes it.  This is the only check
# that can catch a mistake the two ends agree on -- a frequency convention that
# is mirrored, a rate that is wrong by a factor, a slot cut a second late.
JT9 = ft8.FT8Decoder()
FT8SIM = shutil.which("ft8sim")
if not JT9.available or FT8SIM is None:
    skip("a known message survives the whole chain, decoded by jt9",
         "needs WSJT-X (jt9 and ft8sim): sudo apt install wsjtx")
else:
    if not shutil.which("wsjtx_app_version"):
        skip("... the WSJT-X version can be named", "wsjtx_app_version missing")
    work = tempfile.mkdtemp(prefix="rsp1-ft8-sim-")
    message, audio_hz, snr = "CQ W1ABC FN42", 1500.0, -5
    sim = subprocess.run([FT8SIM, message, str(audio_hz), "0", "0", "0", "1",
                          str(snr)], cwd=work, capture_output=True, text=True)
    made = sorted(f for f in os.listdir(work) if f.endswith(".wav"))
    check("ft8sim makes one slot of a known message",
          sim.returncode == 0 and len(made) == 1,
          f"rc {sim.returncode}, files {made}, {sim.stderr.strip()[:80]}")
    with wave.open(os.path.join(work, made[0])) as fh:
        sim_rate = fh.getframerate()
        sim_audio = np.frombuffer(fh.readframes(fh.getnframes()),
                                  "<i2").astype(np.float32) / 32768.0
    check("... at the 12 kHz jt9 reads, one slot long",
          sim_rate == ft8.AUDIO_RATE and sim_audio.size == ft8.SLOT_SAMPLES,
          f"{sim_rate} Hz, {sim_audio.size} samples")
    check("... and it decodes on its own, so the rest of this is about us",
          len(JT9.decode(sim_audio, __import__("datetime").datetime
                         .fromtimestamp(SLOT0, tz=__import__("datetime")
                                        .timezone.utc), 0.0)) == 1)

    # Now the same audio as the *receiver* would hand over: 48 kHz, arriving in
    # blocks, in a stream that started before the slot did.
    from scipy.signal import resample_poly
    as_radio = resample_poly(sim_audio, RADIO_RATE // ft8.AUDIO_RATE, 1)
    as_radio = np.ascontiguousarray(as_radio, dtype=np.float32)
    # Exactly 2.5 s of an idle band first, so the signal itself starts on the
    # boundary -- which is what the receiver would be delivering.
    e2e_clock = Clock(SLOT0 - 2.5)
    e2e = ft8.FT8Spotter(dial_hz=14_074_000, clock=e2e_clock, decoder=JT9,
                         auto_start=False)
    heard: list = []
    e2e.on_decodes = lambda found, when: heard.extend(found)
    e2e.arm()
    feed(e2e, e2e_clock, np.zeros(int(2.5 * RADIO_RATE), np.float32))
    feed(e2e, e2e_clock, as_radio)                              # the slot
    feed(e2e, e2e_clock, np.zeros(RADIO_RATE, np.float32))       # and after it
    check("the receiver's audio fills exactly one slot",
          e2e.stats()["queued"] + e2e.stats()["slots"] >= 1,
          str(e2e.stats()))
    e2e.run_pending()
    check("jt9 decodes the message out of the receiver's audio",
          any(d.message == message for d in heard),
          "; ".join(d.line() for d in heard) or "nothing decoded")
    if heard:
        d = heard[0]
        check("... with the frequency it was sent on",
              abs(d.audio_hz - audio_hz) < 30,
              f"{d.audio_hz} Hz (sent on {audio_hz:.0f} Hz)")
        check("... and the absolute frequency on the band",
              abs(d.freq - (14_074_000 + audio_hz)) < 30,
              f"{d.freq/1e6:.6f} MHz")
        check("... in the slot it was sent in, and on time",
              d.utc == ft8.slot_label(SLOT0) and abs(d.dt) < 0.5,
              f"slot {d.utc} (expected {ft8.slot_label(SLOT0)}), dt {d.dt:+.1f}")
        check("... with jt9's own SNR estimate",
              abs(d.snr - snr) <= 3, f"{d.snr} dB (ft8sim was told {snr} dB)")
    shutil.rmtree(work, ignore_errors=True)

print("\n== the window ==")
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class FakeStream:
    def __init__(self, ret=0):
        self.ret = ret


class FakeDevice:
    """The parts of a SoapySDR device that Radio.open() touches."""

    def __init__(self):
        self.closed = False
        self.deliver = True

    def setSampleRate(self, *a):
        pass

    def setFrequency(self, *a):
        pass

    def setAntenna(self, *a):
        pass

    def setGainMode(self, *a):
        pass

    def setGain(self, *a):
        pass

    def setBandwidth(self, *a):
        pass

    def setDCOffsetMode(self, *a):
        pass

    def setupStream(self, *a):
        return FakeStream()

    def activateStream(self, *a):
        pass

    def deactivateStream(self, *a):
        pass

    def closeStream(self, *a):
        pass

    def readStream(self, *a, **kw):
        time.sleep(0.002)
        if not self.deliver:
            return FakeStream(0)
        return FakeStream(min(int(a[2]) if len(a) > 2 else 0, 256))

    def getSampleRate(self, *a):
        return 2_000_000

    def close(self):
        self.closed = True


class FakeDevices:
    def __init__(self, soapy):
        self.soapy = soapy

    def __call__(self, args):
        dev = FakeDevice()
        self.soapy.made.append(dev)
        return dev

    def enumerate(self):
        return []


class FakeSoapy:
    def __init__(self):
        self.made: list[FakeDevice] = []
        self.Device = FakeDevices(self)


class _StubBackend:
    def describe(self):
        return "stub"


class StubAudio:
    def __init__(self, rate, backend="auto"):
        self.rate = rate
        self.backend = _StubBackend()
        self.volume = 1.0
        self.muted = False

    def start(self):
        pass

    def close(self):
        pass

    def write(self, data):
        return True


import rspsdr.radio as radio_mod  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from rspsdr.radio import Settings  # noqa: E402
from rspsdr.ui import FT8_KEEP, MainWindow  # noqa: E402

real_soapy, real_have = radio_mod.SoapySDR, radio_mod._HAVE_SOAPY
real_audio = radio_mod.AudioOutput
app = QApplication.instance() or QApplication([])


def wait_for_audio(window, seconds: float = 4.0) -> float:
    """Wait for the stand-in radio's audio to reach the window's decoder.

    The fake device hands over 256 IQ samples at a time, so the DSP produces a
    block only every few hundred milliseconds -- real hardware is far quicker
    (the live check drives 48 kHz continuously).  Bounded, and it says how long
    it actually waited rather than assuming.
    """
    started = time.monotonic()
    deadline = started + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
        if window.spotter.idle_seconds() < 1.0:
            break
    return time.monotonic() - started
try:
    radio_mod.AudioOutput = StubAudio
    fake = FakeSoapy()
    radio_mod.SoapySDR, radio_mod._HAVE_SOAPY = fake, True
    win = MainWindow(Settings(), os.path.join(tempfile.mkdtemp(
        prefix="rsp1-ft8-gui-"), "memories.json"))
    win.show()                                  # isVisible() needs a shown window
    app.processEvents()
    check("the window starts in the mode it was given, with that button lit",
          win.mode_buttons["lsb"].isChecked()
          and sum(b.isChecked() for b in win.mode_buttons.values()) == 1,
          f"{[k for k, b in win.mode_buttons.items() if b.isChecked()]}")
    check("the FT8 strip is hidden until FT8 is selected",
          not win.ft8_panel.isVisible() and not win.spotter.armed)
    check("... and the button is there to select it", "ft8" in win.mode_buttons
          and win.mode_buttons["ft8"].text() == "FT8")
    check("the band list is the presets, each named with its dial",
          win.cmb_ft8band.count() == len(ft8.FT8_BANDS)
          and "20m" in win.cmb_ft8band.itemText(5)
          and "14.074" in win.cmb_ft8band.itemText(5),
          win.cmb_ft8band.itemText(5))

    win.mode_buttons["ft8"].click()
    app.processEvents()
    check("selecting FT8 shows the strip, in USB with a 3 kHz filter",
          win.ft8_panel.isVisible() and win.radio.settings.mode == "usb"
          and abs(win.radio.settings.filter_bw - 3000.0) < 1.0,
          f"mode {win.radio.settings.mode}, "
          f"{win.radio.settings.filter_bw:.0f} Hz")
    check("... and arms the decoder", win.spotter.armed)
    check("... with the receiver left in the mode the decoder wants",
          win._current_mode() == "ft8" and win.settings.mode == "ft8",
          f"window {win._current_mode()}, settings {win.settings.mode}")
    # A decoder wants the passband as it arrived.  Each of these three is a
    # subtract-from-the-signal operation, and each is on by default or set by
    # the user for listening.
    win.chk_nr.setChecked(True)
    win.chk_aagc.setChecked(True)
    win.spn_sq.setValue(-60.0)
    win._ft8_prepare()
    check("entering FT8 turns off the things that damage a decode",
          not win.chk_nr.isChecked() and not win.chk_aagc.isChecked()
          and win.spn_sq.value() <= -140.0,
          f"NR {win.chk_nr.isChecked()}, AGC {win.chk_aagc.isChecked()}, "
          f"squelch {win.spn_sq.value():.0f} dB")

    win.cmb_ft8band.setCurrentIndex(5)          # 20m
    app.processEvents()
    check("a band preset tunes the receiver to the dial frequency",
          abs(win.radio.settings.tuned_freq - 14_074_000) < 1.0
          and abs(win.spn_freq.value() - 14.074) < 1e-6,
          f"{win.radio.settings.tuned_freq/1e6:.6f} MHz")
    check("... and tells the decoder which dial the audio belongs to",
          win.spotter.dial_hz == 14_074_000, f"{win.spotter.dial_hz:.0f} Hz")

    # The whole path a decode takes: the spotter's callback (on the decoder's
    # thread) through the bridge to the GUI thread.
    found = [ft8.Decode("143000", -12, 0.1, 1550, "CQ W1ABC FN42", 14_074_000),
             ft8.Decode("143000", -4, -0.2, 640, "K1ABC W9XYZ EN37", 14_074_000)]
    win.spotter.on_decodes(found, None)
    app.processEvents()
    check("a decoded slot appears in the list, one row per message",
          win.tbl_ft8.rowCount() == 2, f"{win.tbl_ft8.rowCount()} rows")
    check("... in frequency order, with the columns the decoder reports",
          [win.tbl_ft8.item(r, 4).text() for r in range(2)]
          == ["K1ABC W9XYZ EN37", "CQ W1ABC FN42"]
          and [win.tbl_ft8.item(r, 0).text() for r in range(2)]
          == ["14:30:00", "14:30:00"]
          and win.tbl_ft8.item(1, 3).text() == "1550",
          f"|".join(win.tbl_ft8.item(0, c).text() for c in range(5)))
    check("... and the row knows the frequency it was heard on",
          "14.075550 MHz" in win.tbl_ft8.item(1, 4).toolTip()
          and "above the 14.074 MHz dial" in win.tbl_ft8.item(1, 4).toolTip(),
          win.tbl_ft8.item(1, 4).toolTip().replace("\n", " "))
    check("the strip reports the totals", "2 message(s)" in win.lbl_ft8.text(),
          win.lbl_ft8.text())
    win._ft8_tune_to_row(1, 4)
    app.processEvents()
    check("clicking a decode tunes to the frequency it was heard on",
          abs(win.radio.settings.tuned_freq - 14_075_550) < 1.0,
          f"{win.radio.settings.tuned_freq/1e6:.6f} MHz")
    win._ft8_clear()
    check("Clear empties the list", win.tbl_ft8.rowCount() == 0)

    # An operator's log, not a recording: a night's listening must not grow the
    # window's memory without bound.
    burst = [ft8.Decode("143000", -10, 0.0, 500 + i, f"CQ W1ABC FN42", 14_074_000)
             for i in range(FT8_KEEP + 25)]
    win._ft8_add_decodes(burst, None)
    check("the list is capped, oldest first",
          win.tbl_ft8.rowCount() == FT8_KEEP
          and win.tbl_ft8.item(0, 3).text() == "525",
          f"{win.tbl_ft8.rowCount()} rows, oldest {win.tbl_ft8.item(0, 3).text()} Hz")
    win._ft8_clear()

    # The strip takes height from the plots, and the window may be as small as
    # 780x560 -- where the wrapped control rows have already left the plots
    # about 55 px each.  The strip yields to them: the list shortens to three
    # rows and each plot gives up 21 px (measured), which is the price of
    # having the decoder on a window this small at all.
    win.resize(win.minimumWidth(), win.minimumHeight())
    win.mode_buttons["lsb"].click()
    app.processEvents()
    without = (win.spectrum.height(), win.waterfall.height())
    win.mode_buttons["ft8"].click()
    app.processEvents()
    with_strip = (win.spectrum.height(), win.waterfall.height())
    panel_bottom = win.ft8_panel.mapTo(win, win.ft8_panel.rect().bottomLeft()).y()
    check("the strip fits at the smallest window size without eating the plots",
          win.ft8_panel.isVisible() and panel_bottom <= win.height()
          and with_strip[1] >= 30
          and without[1] - with_strip[1] <= 25
          and win.tbl_ft8.height() > 0 and win.lbl_ft8.text() != "",
          f"plots {without} -> {with_strip} px at 780x560, "
          f"panel bottom {panel_bottom} of {win.height()} px, "
          f"list {win.tbl_ft8.height()} px")
    win.resize(1280, 860)
    app.processEvents()
    check("... and at a normal size the list is the full six rows",
          win.tbl_ft8.height() == 24 + 6 * 16,
          f"{win.tbl_ft8.height()} px")

    # A receiver that is not running yet: the strip has to say so rather than
    # count down to a slot that will never be filled.
    win._ft8_tick()
    check("with no audio arriving the strip says it is waiting",
          "waiting for the receiver" in win.lbl_ft8.text(), win.lbl_ft8.text())

    # And the radio itself: opening it proves no demodulator was ever asked for
    # a mode called "ft8" (which raises), and produces the audio the decoder
    # was waiting for.  Only the radio can have delivered it: the last thing
    # fed to the spotter by hand was long enough ago to have timed out.
    check("the radio opens in FT8 mode without tripping over the mode name",
          win.start_radio() and win.radio.opened
          and win.radio.demod.mode == "usb",
          f"demod {getattr(win.radio.demod, 'mode', None)}")
    waited = wait_for_audio(win)
    check("... and the window is listening to the receiver's audio",
          win.spotter.idle_seconds() < 1.0,
          f"idle {win.spotter.idle_seconds():.2f} s after {waited:.1f} s")
    win._ft8_tick()
    check("... so the strip stops saying it is waiting",
          "waiting for the receiver" not in win.lbl_ft8.text(),
          win.lbl_ft8.text())
    win._toggle()                               # the user presses Stop
    check("stopping the receiver does not disarm the decoder",
          win.spotter.armed)

    # A channel saved while the mode is FT8 has to come back in it -- the mode
    # the *user* chose is stored, not the USB it is received through.
    saved = win._current_channel()
    saved.name = "20m FT8 test"
    win._mem_write(saved)
    win.mode_buttons["usb"].click()
    app.processEvents()
    check("leaving FT8 hides the strip and stops the decoding",
          not win.ft8_panel.isVisible() and not win.spotter.armed)
    check("... and the receiver goes back to the mode that was asked for",
          win.radio.settings.mode == "usb"
          and abs(win.radio.settings.filter_bw - 2400.0) < 1.0,
          f"{win.radio.settings.mode} {win.radio.settings.filter_bw:.0f} Hz")
    on_disk = json.load(open(win.memory.path))["channels"]
    check("... with the channel stored as FT8, not as USB",
          on_disk[0]["mode"] == "ft8" and abs(on_disk[0]["filter_bw"] - 3000.0) < 1.0,
          str(on_disk[0]))
    win._mem_recall()
    app.processEvents()
    check("recalling it brings the mode, the strip and the decoder back",
          win._current_mode() == "ft8" and win.settings.mode == "ft8"
          and win.ft8_panel.isVisible() and win.spotter.armed
          and win.radio.settings.mode == "usb"
          and abs(win.radio.settings.tuned_freq - saved.freq) < 1.0,
          f"{win._current_mode()}, panel {win.ft8_panel.isVisible()}, "
          f"armed {win.spotter.armed}, radio {win.radio.settings.mode}")

    # The mode can also be asked for before the window exists: --mode ft8 has to
    # open with the strip up and the decoder armed.
    from rspsdr.__main__ import _make_settings, build_parser
    args = build_parser().parse_args(["--mode", "ft8"])
    settings = _make_settings(args)
    check("--mode ft8 keeps the mode and asks the radio for USB",
          settings.mode == "ft8" and Demodulator(ft8.receiver_mode(settings.mode))
          and abs(settings.filter_bw - 3000.0) < 1.0,
          f"mode {settings.mode}, filter {settings.filter_bw:.0f} Hz")
    win2 = MainWindow(settings, os.path.join(tempfile.mkdtemp(
        prefix="rsp1-ft8-cli-"), "memories.json"))
    win2.show()
    app.processEvents()
    check("a window started with --mode ft8 opens in FT8",
          win2.mode_buttons["ft8"].isChecked() and win2.ft8_panel.isVisible()
          and win2.spotter.armed,
          f"button {win2.mode_buttons['ft8'].isChecked()}, "
          f"panel {win2.ft8_panel.isVisible()}, armed {win2.spotter.armed}")
    # The mode is in the settings the radio is *built* from here, which is the
    # path that would raise on a mode no demodulator knows -- the window's own
    # apply() would otherwise have translated it away before the radio saw it.
    check("... and it streams, with the receiver in USB",
          win2.start_radio() and win2.radio.opened
          and win2.radio.settings.mode == "ft8"
          and win2.radio.demod.mode == "usb",
          f"radio mode {win2.radio.settings.mode}, "
          f"demod {getattr(win2.radio.demod, 'mode', None)}")
    waited = wait_for_audio(win2)
    check("... and the decoder hears it, on a radio built from --mode ft8",
          win2.spotter.idle_seconds() < 1.0,
          f"idle {win2.spotter.idle_seconds():.2f} s after {waited:.1f} s")
    win2.close()
    win.close()
finally:
    radio_mod.AudioOutput = real_audio
    radio_mod.SoapySDR, radio_mod._HAVE_SOAPY = real_soapy, real_have

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all FT8 checks passed")
