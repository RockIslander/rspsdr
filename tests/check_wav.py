"""Analyse a recorded WAV: is it real audio, or just noise/silence?

    python3 tests/check_wav.py artifacts/wwv10_am.wav
"""

from __future__ import annotations

import sys
import wave

import numpy as np


def main(path: str) -> int:
    with wave.open(path, "rb") as w:
        ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        print(f"{path}: unexpected sample width {sw * 8} bit")
        return 2
    x = np.frombuffer(raw, "<i2").astype(np.float64) / 32768.0
    dur = x.size / rate

    print(f"file            : {path}")
    print(f"format          : {rate} Hz, {ch} ch, {sw * 8} bit, {x.size} samples")
    print(f"duration        : {dur:.3f} s")
    print(f"rms / peak      : {x.std():.5f} / {np.abs(x).max():.5f}  "
          f"(crest {np.abs(x).max() / max(x.std(), 1e-12):.1f})")
    print(f"dc offset       : {x.mean():+.2e}")

    # Spectral shape over 1 s windows: speech/music has energy concentrated in
    # 300-3000 Hz, flat white noise does not.
    seg = x[: int(1.0 * rate)]
    if seg.size < 1024:
        print("too short to analyse")
        return 1
    win = np.hanning(seg.size)
    sp = np.abs(np.fft.rfft(seg * win, n=1 << 16))
    f = np.arange(sp.size) * rate / (1 << 16)
    p = sp ** 2
    total = p.sum() + 1e-30
    voice = p[(f >= 300) & (f <= 3000)].sum() / total
    low = p[f < 100].sum() / total
    high = p[f > 6000].sum() / total
    print(f"energy 300-3k   : {100 * voice:5.1f}%")
    print(f"energy <100 Hz  : {100 * low:5.1f}%")
    print(f"energy >6 kHz   : {100 * high:5.1f}%")

    top = np.argsort(sp)[-6:][::-1]
    print("strongest tones :", ", ".join(f"{f[i]:.0f} Hz" for i in top if f[i] > 20))

    # How many 100 ms blocks are actually audible?
    blk = int(0.1 * rate)
    blocks = x[: x.size // blk * blk].reshape(-1, blk) if blk else x.reshape(1, -1)
    rms = np.sqrt((blocks ** 2).mean(axis=1)) if blocks.size else np.array([0.0])
    active = int((rms > 0.25 * rms.max()).sum()) if rms.size and rms.max() > 0 else 0
    print(f"active blocks   : {active}/{rms.size} above 25% of peak rms")

    ok = x.std() > 1e-3 and np.abs(x).max() > 1e-2 and voice > 0.10
    print("\nWAV CHECK " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "artifacts/test.wav"))
