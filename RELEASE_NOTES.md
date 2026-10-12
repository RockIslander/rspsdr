# RSPSDR 1.3.0

**A small HF / shortwave SDR receiver for the SDRplay RSP1.** Version 1.3.0 is
the first release since 1.0.1, and it adds an analogue signal meter, remembered
settings, a built-in FT8 decoder, and a set of fixes to tuning accuracy and
device recovery. It is the same program throughout: one window, one RSP1, no
setup file to write.

* [Download the package](#downloads)
* [What's new](#whats-new)
* [Requirements](#requirements)
* [Verified](#verified)
* [Known limitations](#known-limitations)

## Downloads

| File | What it is |
|---|---|
| `rspsdr_1.3.0_all.deb` | The installable Debian/Ubuntu package (architecture-independent) |
| `rspsdr-1.3.0.tar.gz` | The source tree at the release commit |
| `SHA256SUMS` | Checksums for both of the above |

Install it with `apt`, not `dpkg` — the package needs its dependencies pulled
in:

```bash
sudo apt install ./rspsdr_1.3.0_all.deb
```

It installs `/usr/bin/rspsdr`, the module under
`/usr/lib/python3/dist-packages/rspsdr/`, a menu entry, an icon, and the
README, copyright and changelog under `/usr/share/doc/rspsdr/`. Removing it
with `sudo apt remove rspsdr` leaves your saved channels and settings in
`~/.config/rspsdr/` alone.

The SDRplay module and the vendor API are not in Debian, so they are
recommended rather than required: the package installs and `rspsdr --check`
runs on a plain Debian system, and reports what is missing if the radio is not
there yet.

## What's new

### 1.3.0 — an analogue signal meter

An S-meter drawn to look like the one on an Icom HF transceiver: a needle
sweeping a shallow arc over a printed dial, S1 to S9 and then +20/+40/+60 dB,
in either the dark face of the later radios or the cream one of the older
analogue sets. It is painted rather than assembled from widgets, so the needle
pivots below the face the way a real one does and the top of the scale is
compressed the way a printed dial is.

The scale is the IARU recommendation — S9 is −73 dBm (50 µV into 50 Ω at the
antenna socket), 6 dB per S unit below it, the standard's 10 dB steps above —
and the needle has the recommended quasi-peak ballistics: 10 ms attack, 500 ms
decay, with a peak marker holding the highest level for half a second so a
signal that comes and goes between repaints is not missed. The exact figure is
printed along the foot of the face too, because a needle can only be read to
its own marks.

It measures the level *inside the filter being listened to*, taken before the
audio AGC, the noise reduction, the squelch and the volume: it does not follow
the volume control, it does not care how hard the noise reduction is working,
and it still shows a signal you cannot hear because the squelch is shut.
Stopping the receiver drops the needle back to its rest instead of leaving it
standing at the last signal.

Its absolute accuracy is one number the user supplies, **S9 at**: the RSP1
reports a level in dBFS from after the gain stages and has no absolute
calibration of its own, so the dBFS reading that is to count as S9 is asked
for and the whole scale follows from the standard. The default comes from
measurements on a real RSP1 (2 MSps, 6 kHz AM, the default IF gain of 40 dB).
`--selftest` prints the passband level and what the meter makes of it, which
is how the calibration is checked against a station of known strength.

### 1.2.0 — the window remembers everything

Every control — frequency, mode and filter, CW pitch, IF and RF gain, tuner
bandwidth, RF AGC, volume, noise reduction and its strength, audio AGC,
squelch, FFT size, tuning step, zoom, the waterfall's colour map, levels,
detail and tracking, the FT8 band and whether it decodes every slot — is
written to `~/.config/rspsdr/settings.json` as it changes and read back at the
next start, together with the window's size, place, maximised state and the
divider between the spectrum and the waterfall.

The file is written atomically and every value is read back through one table
that says what kind of thing it is and what range or set of values is usable.
Anything that fails is ignored and the built-in default kept, so a hand-edited
or half-written file costs you one setting, not a window that will not open.
The command line still wins, but only where it was typed. A headless run
(`--selftest`, `--wav`, `--ft8`, `--save-memory`) ignores the file altogether,
so a script gets the same result every time whatever the window last saved, and
`--reset-settings` empties it.

FT8 borrows rather than changes: entering the mode still switches off noise
reduction, the audio AGC and the squelch, but each is remembered and put back
on the way out — a session spent decoding no longer costs the listening
settings for the modes you go back to.

### 1.1.0 — FT8, decoded

An FT8 button in the mode row that turns the receiver into an FT8 decoder: USB
with a 3 kHz filter, and a strip along the foot of the window with the standard
WSJT-X dial frequency for every band from 160 m to 2 m, a slot clock, and the
messages decoded — UTC slot, SNR, time offset, audio frequency and text, newest
at the bottom. Clicking a line tunes to the frequency that message was heard
on.

FT8 is *decoded*, not written: WSJT-X's `jt9` is the decoder, and `rspsdr/ft8.py`
is everything around it that is specific to this program — the band presets,
cutting the receiver's 48 kHz audio down to the 12 kHz `jt9` reads, aligning
that cut to the UTC quarter-minute, and turning the output into something a
window can show and click on. Each finished slot goes to a worker thread, so
one slot decodes while the next is already being received. With WSJT-X absent
the mode still works as a receiver and says what is missing instead of failing
every slot.

It also runs headless: `--ft8` decodes from the command line and prints each
message as it arrives, `--mode ft8` opens the window in the mode with the
decoder armed, and `--check` names the decoder (or how to install one)
alongside the band presets.

Fixed along the way: FT8 used to decode the first slot and then stop for ever
with perfectly good audio arriving. A slot's worth of additions of 1/12000 s
drifts by a few microseconds, and a few microseconds past a boundary made the
"next slot" question return the boundary after the one it meant — skipping
fifteen seconds of audio, then the next fifteen, indefinitely, while the strip
went on saying it was listening to every slot. Measured over ten minutes of
synthetic audio: 40 boundaries, 40 slots captured, against one before.

### 1.0.2 — tuning accuracy and device recovery

* **The waterfall was drawn at the wrong horizontal scale**, which made tuning
  feel inaccurate: a row was placed using the width of the image already held,
  so the rectangle was built from the previous column count. The first frame
  was drawn 37.6 MHz wide inside a 2 MHz window, and the ratio came out again
  after any resize or detail change. The axis, the clicks and the drags were
  correct throughout, so a signal appeared at a frequency nothing like the one
  the window reported. The row is handed over first, then placed.
* **A drag moved the window out from under the pointer.** The automatic
  re-centre ran during the gesture, so a drag across 90 % of the half-span
  jumped half a window and then tuned in steps of it. Re-centring is held off
  until the button comes up, and the pointer-to-frequency mapping is frozen for
  the duration of the gesture.
* **Clicks and drags read their frequency from the transform the frequency
  scale is drawn with** instead of being worked back out of the axis range and
  the widget's rectangle; the two disagreed by about a pixel, so clicking a
  carrier could land 500 Hz from the frequency the labels showed.
* **The receiver recovers when the device is busy at startup** instead of
  giving up until the program is restarted. The front end serves one client at
  a time; while another program holds the RSP1, the window says the device is
  busy, keeps asking (after 1 s, then every 5 s) and starts streaming by itself
  the moment the other program closes.
* **The receiver also recovers when the stream dies underneath it** —
  unplugging the radio, restarting the SDRplay service, or a driver wedged
  after a resume: three seconds of silence ends the stream properly, the window
  releases the device, says what happened, and looks for the radio again, so
  plugging it back in is enough.
* `Radio.open()` is now atomic: a failure part-way through unmakes the device
  it had already made.

### Also fixed

* The GUI smoke test wrote the user's own settings file — it drives every
  control in the window, and the window remembers them, so running the test
  reconfigured the person running it. It now keeps its settings in a config
  directory of its own.
* The squelch box showed −140 dB whatever the receiver was actually doing, and
  the RF gain and tuner bandwidth boxes always showed their first entry however
  they were set.
* The mode in use was not the mode lit up: the window opened with no mode
  button pressed at all.
* Restoring a maximised window only wrote it down rather than restoring it, and
  saving the client position walked the window down the screen by the height of
  its own decoration on every restart.

## Requirements

Python 3.10 or later, with `python3-numpy`, `python3-scipy`, `python3-pyqt6`
and `python3-pyqt6.qtmultimedia`, `python3-pyqtgraph` and `python3-soapysdr` —
all available from the Debian/Ubuntu archives (the SoapySDR bindings are apt
only; there is no PyPI wheel).

The radio itself needs the SDRplay module (`soapysdr-module-sdrplay3`) and the
SDRplay API (V3) with its service running; neither is in Debian, so the package
recommends them rather than requiring them. Sound needs a PulseAudio/PipeWire
(`pacat`), `pw-play` or ALSA (`aplay`) command-line player. WSJT-X (`wsjtx`) is
needed for FT8 only — it ships `jt9`, the decoder the mode drives. The `.deb`
builds on Debian or Ubuntu with nothing but `dpkg-dev` added.

## Verified

The release commit passes the whole suite with the RSP1 attached:
**16 checks passed, 0 failed, 0 skipped**, from **706 individual checks** —
137 unit, 80 settings, 63 DSP, 55 device recovery, 36 receive chain, 33 memory
store, and 173 GUI, plus the FT8 suite (119 checks, driven by synthetic audio
and by WSJT-X's own encoder and decoder at both ends) and 9 checks on a real
display. On the `--offline` path (no radio attached) the hardware group skips
itself rather than failing.

The package was rebuilt from the release commit and its contents checked
against the tree: the module it installs is byte-identical to the source, and
the README and changelog it ships match.

## Known limitations

* **Absolute accuracy of the signal meter is one user-supplied number.** An
  RSP1 has no power calibration, so the scale is only as good as the signal you
  set it against, and a calibration holds only at the gain it was made at. The
  needle resolves to about half a mark (1.5 dB below S9, 2.5 dB above), which
  is why the exact figure is printed.
* **The bottom of the meter's scale is the calibration's business, not the
  hardware's.** S1 is −121 dBm, under the RSP1's own noise floor even at its
  lowest gain, where a whole band of HF noise still reads S3.8.
* **FT8 depends on WSJT-X** for decoding; the receiver is still a receiver
  without it, but the decode strip says so rather than decoding.
* The FT8 live check in the suite verifies the pipeline, not propagation: it
  requires slots to be captured, and whether anything is *heard* depends on the
  band and the hour.
* The package is architecture-independent Python, so it needs the runtime
  packages listed above; it does not bundle them.

---

The full history is in [`debian/changelog`](debian/changelog), which is also
installed with the package.
