"""Command line entry point.

    python3 -m rspsdr                 # open the GUI
    python3 -m rspsdr --check         # hardware / audio diagnostics
    python3 -m rspsdr --selftest      # receive and report for N seconds
    python3 -m rspsdr --wav out.wav   # record demodulated audio
    python3 -m rspsdr --mode ft8      # open the GUI in FT8 mode
    python3 -m rspsdr --reset-settings
                                      # open on the built-in defaults, having
                                      # cleared what the window remembered
    python3 -m rspsdr --ft8 --freq 14074000 --duration 60
                                      # decode FT8 headless, printing it

The window remembers everything between runs (see ``prefs``): the frequency,
mode, filter, gains, volume, noise reduction, waterfall colours and levels,
zoom, FT8 band and the window's own size and place are written when they change
and read back at the next start.  A headless command (``--selftest``, ``--wav``,
``--ft8``, ``--save-memory``) deliberately does *not* read them: a script has to
be able to depend on the same result every time, whatever the window last saved.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

from . import __version__
import wave

import numpy as np


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rspsdr",
        description="HF / shortwave receiver for the SDRplay RSP1.")
    p.add_argument("--device", default="driver=sdrplay",
                   help="SoapySDR device arguments (default: driver=sdrplay)")
    p.add_argument("--rate", type=float, default=2_000_000,
                   help="sample rate in Hz (default 2 MSps)")
    p.add_argument("--freq", type=float, default=7_100_000,
                   help="tuned frequency in Hz")
    p.add_argument("--mode", default="lsb",
                   choices=["am", "sam", "usb", "lsb", "cw", "nfm", "ft8"])
    p.add_argument("--ft8", action="store_true",
                   help="decode FT8 headless (needs WSJT-X's jt9) and print "
                        "what is heard; --freq is the dial frequency")
    p.add_argument("--bw", type=float, default=None,
                   help="filter bandwidth in Hz (default per mode)")
    p.add_argument("--audio-backend", default="auto",
                   choices=["auto", "pipe", "qt"])
    p.add_argument("--gain", type=float, default=40.0, help="IF gain in dB")
    p.add_argument("--agc", action="store_true", help="enable RF AGC")
    p.add_argument("--nr", action="store_true",
                   help="enable noise reduction on the demodulated audio")
    p.add_argument("--nr-level", type=float, default=50.0,
                   help="noise reduction strength, 0..100 (default 50)")
    p.add_argument("--memories", action="store_true",
                   help="list saved memory channels, then exit")
    p.add_argument("--save-memory", metavar="NAME",
                   help="save the given frequency/mode as a memory channel")
    p.add_argument("--check", action="store_true",
                   help="report devices, audio backend and DSP plan, then exit")
    p.add_argument("--selftest", action="store_true",
                   help="receive for --duration seconds and print a report")
    p.add_argument("--duration", type=float, default=5.0,
                   help="seconds for --selftest / --wav")
    p.add_argument("--wav", metavar="FILE",
                   help="record demodulated audio to a mono 48 kHz WAV file")
    p.add_argument("--list", action="store_true", help="list SoapySDR devices")
    p.add_argument("--reset-settings", action="store_true",
                   help="forget everything the window remembered about the last "
                        "run and open on the built-in defaults")
    p.add_argument("--screenshot", metavar="FILE",
                   help="start the GUI, save a picture of its own window, exit")
    p.add_argument("--screenshot-delay", type=float, default=10.0,
                   help="seconds to run before --screenshot (default 10)")
    # A packaged build has to be able to say which build it is: the Debian
    # changelog, the module version and a bug report all have to agree.
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    return p


def cmd_check(args) -> int:
    from .audio import find_pipe_command
    from .dsp import AUDIO_RATE, plan_chain
    from .radio import list_devices

    print("RSPSDR -- diagnostics")
    print("=" * 62)

    try:
        import SoapySDR  # noqa: F401
        print("SoapySDR bindings      : OK")
    except Exception as exc:
        print(f"SoapySDR bindings      : MISSING ({exc})")
        print("                         install: sudo apt install python3-soapysdr")

    try:
        import pyqtgraph
        print(f"pyqtgraph              : {pyqtgraph.__version__} "
              f"(Qt {os.environ.get('PYQTGRAPH_QT_LIB', 'auto')})")
    except Exception as exc:
        print(f"pyqtgraph              : MISSING ({exc})")

    try:
        from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR
        print(f"PyQt6                  : {PYQT_VERSION_STR} (Qt {QT_VERSION_STR})")
    except Exception as exc:
        print(f"PyQt6                  : MISSING ({exc})")

    print()
    print("SoapySDR devices:")
    devs = list_devices()
    if not devs:
        print("  (none found -- is the RSP1 plugged in and the sdrplay "
              "service running?)")
    for d in devs:
        print(f"  [{d['index']}] {d['label']}")
        print(f"      args: {d['args']}")

    try:
        from .memory import MemoryStore
        store = MemoryStore()
        print(f"memory channels        : {len(store)} ({store.path})")
    except Exception as exc:
        print(f"memory channels        : unavailable ({exc})")

    try:
        from . import ft8
        print(f"FT8 decoder            : {ft8.describe()}")
        presets = ", ".join(f"{b.name} {b.dial/1e6:.3f}" for b in ft8.FT8_BANDS)
        print(f"FT8 band presets       : {presets}")
    except Exception as exc:
        print(f"FT8 decoder            : unavailable ({exc})")

    try:
        from . import prefs as prefsmod
        store = prefsmod.PrefsStore()
        known = sum(
            1 for name, (kind, spec) in prefsmod.RADIO_FIELDS.items()
            if prefsmod.read_value(kind, store.data.get("radio", {}).get(name),
                                   spec) is not None)
        print(f"remembered settings    : {known}/{len(prefsmod.RADIO_FIELDS)} "
              f"receiver fields ({store.path})")
    except Exception as exc:
        print(f"remembered settings    : unavailable ({exc})")

    print()
    cmd = find_pipe_command(AUDIO_RATE)
    print(f"audio pipe player      : {' '.join(cmd) if cmd else 'none found'}")
    try:
        from PyQt6.QtMultimedia import QMediaDevices
        devs = QMediaDevices.audioOutputs()
        print(f"Qt audio outputs       : {len(devs)}")
        for d in devs:
            print(f"    {d.description()}{'  [default]' if d.isDefault() else ''}")
    except Exception as exc:
        print(f"Qt audio outputs       : unavailable ({exc})")

    print()
    print("DSP decimation plans:")
    for rate in (96000, 250000, 1000000, 1536000, 2000000, 4000000, 10_000_000):
        stages, hop_in, hop_out = plan_chain(rate)
        desc = " ".join(
            (f"/{dn}" if up == 1 else f"x{up}/{dn}") for up, dn in stages)
        print(f"  {rate/1e6:>8g} MSps -> 48 kHz : {desc:<18} "
              f"block {hop_in:>7d} -> {hop_out} samples")
    return 0 if devs else 1


# The command-line options that stand for a remembered receiver setting.  An
# option the user did not type leaves the remembered value alone; one that was
# typed wins over it, which is what makes "the file is the base and the command
# line overrides it" true rather than nearly true -- ``--agc`` and ``--nr`` are
# the awkward ones, since not typing a store_true flag looks exactly like saying
# no to it.
CLI_OVERRIDES = {
    "device": "device_args",
    "rate": "sample_rate",
    "freq": "tuned_freq",
    "mode": "mode",
    "bw": "filter_bw",
    "audio_backend": "audio_backend",
    "gain": "if_gain",
    "agc": "rf_agc",
    "nr": "nr",
    "nr_level": "nr_level",
}


# Stands in for "nobody typed this option" while the command line is read a
# second time (see _typed_options).  A distinct object rather than None: None
# is a value some of these options could legitimately have.
_UNSET = object()


def _typed_options(argv) -> set:
    """Which of the parser's options the command line actually carried.

    ``argparse`` records a value for every option whether it was given or not,
    so the only way to tell "the user asked for 1 MSps" from "1 MSps is the
    default" is to ask a second time with the defaults replaced by a marker: a
    value that is no longer the marker was typed.  ``default=None`` cannot
    stand in for that, because the options have real defaults which have to go
    on working.
    """
    probe = build_parser()
    probe.set_defaults(**{a.dest: _UNSET for a in probe._actions
                          if a.dest != argparse.SUPPRESS})
    return {dest for dest, value in vars(probe.parse_args(argv)).items()
            if value is not _UNSET}


def open_prefs(args):
    """The store this run remembers into, or ``None`` when it remembers nothing.

    Two runs get no store at all.  A ``--screenshot`` run is a scripted look at
    the window rather than a session of listening, so it must be neither
    configured by what was remembered nor able to leave anything behind for the
    next real start.  ``--reset-settings`` gets a store that has been written
    empty *before* the window opens: a crash on the way up cannot then leave the
    values the user asked to be rid of still in place.
    """
    if args.screenshot:
        return None
    from . import prefs as prefsmod

    store = prefsmod.PrefsStore(read=not args.reset_settings)
    if args.reset_settings:
        store.save()
    return store


def _make_settings(args, prefs_store=None, given=()):
    from . import ft8
    from . import prefs as prefsmod
    from .dsp import Demodulator
    from .radio import Settings

    # "ft8" is not a demodulator: the radio is asked for the mode FT8 is heard
    # through, and the mode kept in the settings is the one the user asked for
    # (so the window opens on the FT8 button, with the decoder armed).
    bw = args.bw
    if bw is None:
        bw = (ft8.FILTER_BW if args.mode == "ft8"
              else Demodulator(ft8.receiver_mode(args.mode)).bw)
    typed = Settings(
        device_args=args.device, sample_rate=int(round(args.rate)),
        center_freq=args.freq, tuned_freq=args.freq, mode=args.mode,
        filter_bw=float(bw), rf_agc=bool(args.agc), if_gain=float(args.gain),
        nr=bool(args.nr), nr_level=float(args.nr_level),
        audio_backend=args.audio_backend)
    if prefs_store is None:
        return typed
    # The remembered settings, with the arguments that were really given laid
    # back over the top of them.
    remembered = prefsmod.settings_from(prefs_store, typed)
    over = {field: getattr(typed, field)
            for dest, field in CLI_OVERRIDES.items() if dest in given}
    if "freq" in given:
        over["center_freq"] = typed.center_freq     # the dial and the window together
    if "bw" in given:
        over["filter_bw"] = typed.filter_bw
    elif "mode" in given and typed.mode != remembered.mode:
        # A filter is not an independent choice, it is the one that suits the
        # mode -- so asking for a mode without asking for a filter gets that
        # mode's width, and not the width that belonged to the mode being left
        # behind.  The window's own table, so that --mode am opens with the 6
        # kHz the AM button would give rather than the demodulator's generic
        # default.  A mode that was *not* changed keeps the filter that was
        # chosen for it, which may well not be the table's.
        from .ui import MODE_BW
        over["filter_bw"] = MODE_BW.get(typed.mode, typed.filter_bw)
    return remembered.copy_with(**over)


def cmd_memories(args) -> int:
    from .memory import MemoryStore

    store = MemoryStore()
    if not len(store):
        print(f"no memories saved yet ({store.path})")
        return 0
    print(f"{len(store)} memory channel(s) in {store.path}")
    for i, ch in enumerate(store):
        print(f"  [{i}] {ch.name:<24} {ch.freq/1e6:>12,.6f} MHz  "
              f"{ch.mode.upper():<4} {ch.filter_bw:>7.0f} Hz")
    return 0


def cmd_save_memory(args) -> int:
    from .memory import Channel, MemoryStore

    s = _make_settings(args)
    ch = Channel(name=args.save_memory, freq=s.tuned_freq, mode=s.mode,
                 filter_bw=s.filter_bw, cw_pitch=s.cw_pitch)
    store = MemoryStore()
    store.save_channel(ch)
    print(f"saved “{ch.name}” {ch.freq/1e6:.6f} MHz {ch.mode.upper()} "
          f"{ch.filter_bw:.0f} Hz  ->  {store.path}")
    return 0


def cmd_ft8(args) -> int:
    """--ft8: decode FT8 headless, printing each slot's messages as they land.

    The same spotter the window uses, driven from the command line -- which is
    what makes the decoder testable without a display, and what a station with
    no screen would run.
    """
    from . import ft8
    from .radio import Radio

    settings = _make_settings(args)
    # What the decoder wants, applied to the trial settings rather than to the
    # user's: no noise reduction, no audio AGC, no squelch, USB at 3 kHz.
    settings = settings.copy_with(mode=ft8.RECEIVER_MODE, filter_bw=ft8.FILTER_BW,
                                  nr=False, audio_agc=False, squelch_db=-200.0)
    decodes: list = []
    errors: list[str] = []
    printing = threading.Lock()

    def heard(found, when):
        decodes.extend(found)
        with printing:                                 # one line at a time
            for d in sorted(found, key=lambda x: x.audio_hz):
                print(f"  {d.line()}   {d.freq/1e6:.6f} MHz")
            sys.stdout.flush()

    spotter = ft8.FT8Spotter(dial_hz=settings.tuned_freq, on_decodes=heard,
                             on_error=errors.append)
    radio = Radio(settings, on_error=errors.append,
                  on_audio=lambda a: spotter.feed(a))

    seconds = float(args.duration)
    if seconds < ft8.SLOT_SECONDS + 1.0:
        # A slot is 15 s and a decode needs the whole of one, so a duration
        # shorter than that cannot produce anything at all.  Say so rather
        # than silently listening for a length of time that cannot work.
        seconds = 60.0
        print(f"--duration {args.duration:g} s is shorter than one FT8 slot; "
              f"listening for {seconds:.0f} s instead")
    print(f"FT8 on {settings.tuned_freq/1e6:.6f} MHz dial "
          f"(signals {ft8.LOW_HZ:.0f}-{ft8.HIGH_HZ:.0f} Hz above it), "
          f"{seconds:.0f} s")
    if not spotter.decoder.available:
        print(f"  no decoder: {spotter.decoder.describe()}")
    try:
        radio.open()
    except Exception as exc:
        print(f"  could not open the radio : {exc}")
        radio.close()
        print("\nFT8 FAILED")
        return 1
    spotter.arm()
    try:
        time.sleep(seconds)
    finally:
        spotter.stop()
        radio.close()
    st = spotter.stats()
    for e in errors[:5]:
        print(f"  error                 : {e}")
    print(f"  slots decoded         : {st['slots']}")
    if st["dropped"]:
        print(f"  slots dropped         : {st['dropped']}")
    print(f"  messages decoded      : {st['decodes']}")
    if decodes:
        print(f"  strongest             : "
              f"{max(decodes, key=lambda d: d.snr).line()}")
        print(f"  distinct messages     : {len({d.message for d in decodes})}")
        print(f"  slots heard in        : {len({d.utc for d in decodes})}")
    ok = st["slots"] > 0 and not errors
    print("\nFT8 " + ("RECEIVED" if ok else "FAILED"))
    return 0 if ok else 1


def cmd_receive(args) -> int:
    """--selftest and/or --wav: run the real receive chain headless."""
    from .dsp import AUDIO_RATE
    from .radio import Radio

    settings = _make_settings(args)
    lines: list[tuple[float, np.ndarray]] = []
    audio_sink: list[np.ndarray] = []
    errors: list[str] = []

    radio = Radio(settings,
                  on_spectrum=lambda c, db, sr: lines.append((c, db.copy())),
                  on_error=errors.append)

    print(f"opening {settings.device_args} @ {settings.sample_rate/1e6:g} MSps, "
          f"{settings.tuned_freq/1e6:.6f} MHz, {settings.mode.upper()}"
          + (f", NR {settings.nr_level:.0f}%" if settings.nr else ""))
    try:
        radio.open()
    except Exception as exc:
        # A missing device or a missing sound server is a reportable condition,
        # not a traceback.
        print(f"  could not open the radio : {exc}")
        radio.close()
        print("\nSELFTEST FAILED")
        return 1
    if args.wav:
        # Stop the output properly before standing in for it: setting the
        # running flag by hand left the writer thread mid-write on a closed
        # backend.
        radio.audio.stop()
        radio.audio.backend.close()               # silence, we record instead
        radio.audio = _NullAudio(audio_sink)

    t_end = time.time() + args.duration
    levels: list[float] = []
    try:
        while time.time() < t_end:
            time.sleep(0.2)
            if radio.demod is not None:
                levels.append(radio.demod.passband_db)
    finally:
        st = radio.status
        radio.close()
    # The passband level and what the signal meter makes of it: the one place a
    # headless run can be asked what an operator would see on the needle, and
    # how to check the calibration ("--selftest --freq <a station you know>").
    levels = [v for v in levels if v > -190.0]
    if levels:
        from . import smeter

        mid = sorted(levels)[len(levels) // 2]
        dbm = smeter.dbm_for_dbfs(mid)
        print(f"  passband level        : {mid:.1f} dBFS (median), "
              f"{max(levels):.1f} dBFS (peak)")
        print(f"  signal meter          : {smeter.reading(dbm)} "
              f"({dbm:.0f} dBm, with S9 at "
              f"{smeter.DEFAULT_REF_DBFS:.1f} dBFS)")

    print(f"  sample rate requested : {settings.sample_rate/1e6:g} MSps")
    print(f"  sample rate actual    : {st.actual_rate/1e6:g} MSps")
    print(f"  spectrum frames       : {len(lines)}")
    print(f"  dropped RF blocks     : {st.dropped_chunks}")
    print(f"  dsp cpu load          : {st.cpu_load*100:.1f}%")
    if lines:
        c, db = lines[-1]
        print(f"  spectrum bins         : {db.size}")
        print(f"  spectrum range        : {db.min():.1f} .. {db.max():.1f} dB")
        floor = float(np.median(db))
        peak = float(db.max())
        print(f"  noise floor / peak    : {floor:.1f} / {peak:.1f} dB "
              f"(headroom {peak-floor:.1f} dB)")
    for e in errors[:5]:
        print(f"  error                 : {e}")

    if audio_sink:
        a = np.concatenate(audio_sink)
        rms = float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0
        peak = float(np.max(np.abs(a))) if a.size else 0.0
        print(f"  audio samples         : {a.size} ({a.size/AUDIO_RATE:.2f} s)")
        print(f"  audio rms / peak      : {rms:.5f} / {peak:.5f}")
        if args.wav:
            path = args.wav
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(AUDIO_RATE)
                np.clip(a, -1.0, 1.0, out=a)
                w.writeframes((a * 32767.0).astype("<i2").tobytes())
            print(f"  wrote                 : {path}")

    ok = bool(lines) and st.dropped_chunks == 0 and not errors
    print("\nSELFTEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


class _NullAudio:
    """Stands in for AudioOutput while recording to a WAV file."""

    volume = 1.0
    muted = False
    status = "recording"

    def __init__(self, sink):
        self.sink = sink

    def write(self, audio):
        self.sink.append(np.asarray(audio, dtype=np.float32).copy())

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


def main(argv=None) -> int:
    # Set before anything can import pyqtgraph, so it never picks up the PyQt5
    # that Debian installs alongside it.
    os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")
    args = build_parser().parse_args(argv)

    if args.list:
        from .radio import list_devices
        devs = list_devices()
        if not devs:
            print("no SoapySDR devices found")
            return 1
        for d in devs:
            print(f"[{d['index']}] {d['label']}\n    {d['args']}")
        return 0

    if args.check:
        return cmd_check(args)

    if args.save_memory:
        rc = cmd_save_memory(args)
        if not args.memories:
            return rc

    if args.memories:
        return cmd_memories(args)

    if args.ft8:
        return cmd_ft8(args)

    if args.selftest or args.wav:
        return cmd_receive(args)

    # GUI
    os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from .ui import MainWindow

    from . import logo

    app = QApplication(sys.argv[:1])
    app.setApplicationName(logo.APP_NAME)
    app.setApplicationDisplayName(logo.APP_NAME)
    # Makes the running window match the installed menu entry (and so its
    # icon) instead of being an anonymous process to the task bar.
    app.setDesktopFileName("rspsdr")
    app.setWindowIcon(logo.logo_icon())
    store = open_prefs(args)
    if store is not None and args.reset_settings:
        print(f"remembered settings cleared: {store.path}")
    win = MainWindow(_make_settings(args, store, _typed_options(argv)),
                     prefs_store=store)
    win.show()
    # An app whose only job is to listen should already be listening.
    QTimer.singleShot(0, win.autostart)

    if args.screenshot:
        def shoot():
            # Grab only our own widget tree -- never the surrounding desktop.
            pix = win.grab()
            ok = pix.save(args.screenshot)
            print(f"screenshot {'saved' if ok else 'FAILED'}: {args.screenshot} "
                  f"({pix.width()}x{pix.height()})")
            win.close()
            app.quit()

        QTimer.singleShot(int(args.screenshot_delay * 1000), shoot)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
