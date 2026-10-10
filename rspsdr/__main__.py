"""Command line entry point.

    python3 -m rspsdr                 # open the GUI
    python3 -m rspsdr --check         # hardware / audio diagnostics
    python3 -m rspsdr --selftest      # receive and report for N seconds
    python3 -m rspsdr --wav out.wav   # record demodulated audio
"""

from __future__ import annotations

import argparse
import os
import sys
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
                   choices=["am", "sam", "usb", "lsb", "cw", "nfm"])
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


def _make_settings(args):
    from .dsp import Demodulator
    from .radio import Settings

    bw = args.bw
    if bw is None:
        probe = Demodulator(args.mode)
        bw = probe.bw
    return Settings(
        device_args=args.device, sample_rate=int(round(args.rate)),
        center_freq=args.freq, tuned_freq=args.freq, mode=args.mode,
        filter_bw=float(bw), rf_agc=bool(args.agc), if_gain=float(args.gain),
        nr=bool(args.nr), nr_level=float(args.nr_level),
        audio_backend=args.audio_backend)


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
    try:
        while time.time() < t_end:
            time.sleep(0.2)
    finally:
        st = radio.status
        radio.close()

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
    win = MainWindow(_make_settings(args))
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
