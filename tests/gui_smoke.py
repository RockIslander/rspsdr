"""GUI smoke test: build the real window, drive it with a live RSP1 and
confirm the spectrum/waterfall actually receive data.

    QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py [--shot out.png]

The phases run from QTimer callbacks inside a real ``app.exec()`` loop, which
is how the application actually runs.  (Driving the GUI from a
``processEvents()`` polling loop instead keeps the GUI thread runnable at 100%
duty, thrashes the GIL and makes the DSP thread look far slower than it is.)
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The window remembers its settings, so this test -- which drives every control
# in it -- would otherwise leave the person running it with the test's own
# frequency, volume and waterfall colours.  A config directory of its own, the
# same way the channel store is given one below.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="rsp1-gui-cfg-")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pyqtgraph as pg  # noqa: E402
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer  # noqa: E402
from PyQt6.QtGui import QImage, QMouseEvent, QWheelEvent  # noqa: E402
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel,  # noqa: E402
                             QSplitter)

from rspsdr import logo, ui  # noqa: E402
from rspsdr.memory import MemoryStore  # noqa: E402
from rspsdr.radio import Settings  # noqa: E402
from rspsdr.ui import MainWindow  # noqa: E402

FAILURES: list[str] = []
# Keep the test away from the user's real ~/.config file.
MEM_TMP = tempfile.mkdtemp(prefix="rsp1-gui-mem-")
MEM_PATH = os.path.join(MEM_TMP, "memories.json")


def check(name, ok, detail=""):
    # flush=True because the suite's output goes through a pipe, where Python
    # block-buffers it: a phase that dies mid-run would otherwise take every
    # result so far away with it and report nothing.
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" :: {detail}" if detail else ""),
          flush=True)
    if not ok:
        FAILURES.append(name)


def cpu_jiffies() -> tuple[int, int] | None:
    """(total, idle) jiffies from /proc/stat -- idle counts iowait too."""
    try:
        with open("/proc/stat", "r", encoding="ascii") as fh:
            fields = [int(v) for v in fh.readline().split()[1:]]
    except (OSError, ValueError):       # pragma: no cover
        return None
    if len(fields) < 5:
        return None
    return sum(fields[:9]), fields[3] + fields[4]


CPU0: tuple[int, int] | None = None


def idle_cores() -> float | None:
    """Average idle cores since the test started.

    The 1-minute load average is far too stale to say anything about a twelve
    second run, so this measures the real thing: how much CPU was idle across
    exactly the window under test.
    """
    now = cpu_jiffies()
    if CPU0 is None or now is None:
        return None
    total, idle = now[0] - CPU0[0], now[1] - CPU0[1]
    if total <= 0:
        return None
    return idle / total * (os.cpu_count() or 1)


def check_no_drops(label: str, status, baseline: int = 0):
    """Dropping the oldest RF is the *designed* response to a CPU stall.

    A one-off stall -- another process bursting, a page of swap -- costs a few
    blocks and then the receiver is back in the present, which is the entire
    point of the bounded queue.  What would be a real defect is *failing to
    catch up*: a DSP cost near realtime, or a backlog that stays large.  So the
    criterion is that state, and the counts are reported alongside it.  An
    absolute "must be zero" here just measures how busy the machine happened to
    be for twelve seconds.
    """
    new = status.dropped_chunks - baseline
    spare = idle_cores()
    detail = (f"{new} dropped" +
              (f" of {status.dropped_chunks} total" if baseline else "") +
              f", backlog {status.lag * 1000:.0f} ms, dsp {status.cpu_load * 100:.1f}%" +
              (f", {spare:.1f} cpus idle" if spare is not None else ""))
    if new == 0:
        check(label, True, detail)
        return
    recovered = status.cpu_load < 0.6 and status.lag < 0.25
    check(label, recovered, detail + (" -- caught up" if recovered else
                                      " -- still behind"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", default="artifacts/gui.png")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--freq", type=float, default=9_805_000)
    args = ap.parse_args()

    app = QApplication(sys.argv[:1])
    settings = Settings(center_freq=args.freq, tuned_freq=args.freq, mode="am",
                        filter_bw=6000.0, if_gain=50.0, audio_backend="pipe")
    win = MainWindow(settings, memory_path=MEM_PATH)
    win.show()

    def guard(phase):
        """Run a phase; a bug inside it must fail the run, not hang it.

        An exception raised in a Qt slot is swallowed by the event loop, so
        without this the test would sit there until the timeout with its
        output still in a pipe buffer -- no result at all, which is the worst
        possible outcome for a test.
        """
        def run():
            try:
                phase()
            except Exception as exc:                  # noqa: BLE001
                import traceback
                traceback.print_exc()
                print(f"  FAIL  {phase.__name__} raised {exc!r}", flush=True)
                FAILURES.append(f"{phase.__name__} raised {exc!r}")
                try:
                    win.radio.close()
                except Exception:
                    pass
                app.quit()
        run.__name__ = phase.__name__
        return run

    print("== branding ==")
    check("the window is titled with the application's name",
          win.windowTitle().startswith(logo.APP_NAME), win.windowTitle())
    check("the header states the name",
          win.lbl_app.text() == logo.APP_NAME, repr(win.lbl_app.text()))
    check("the header states what the receiver is for",
          win.lbl_tag.text() == logo.APP_TAGLINE, repr(win.lbl_tag.text()))

    def mark_stats(pix):
        """Opaque fraction, distinct colours and accent pixels of a rendered mark."""
        img = pix.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        w, h = img.width(), img.height()
        ptr = img.constBits()
        ptr.setsize(img.sizeInBytes())
        a = np.frombuffer(ptr, np.uint8).reshape(h, img.bytesPerLine() // 4, 4)
        a = a[:, :w, :]
        opaque = float((a[:, :, 3] > 200).mean())
        flat = a.reshape(-1, 4)
        colours = len({tuple(px) for px in flat[::5]})
        accent = int(((a[:, :, 3] > 200) & (a[:, :, 2] > 150)
                      & (a[:, :, 1] > 140) & (a[:, :, 0] < 170)).sum())
        return opaque, colours, accent

    def face_pixels(widget):
        """The widget as an (h, w, 4) array of bytes, for pixel questions."""
        img = widget.grab().toImage().convertToFormat(
            QImage.Format.Format_RGBA8888)
        ptr = img.constBits()
        ptr.setsize(img.sizeInBytes())
        return np.frombuffer(ptr, np.uint8).reshape(
            img.height(), img.bytesPerLine() // 4, 4)[:, :img.width(), :].astype(int)

    def needle_mask(a):
        """The red needle's pixels -- the only red thing on either face."""
        r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
        return (r > 140) & (r > g + 60) & (r > b + 60)

    def peak_mask(a):
        """The peak marker's pixels -- the only yellow thing on the dark face."""
        r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
        return (r > 200) & (g > 150) & (b < 170) & (g > b + 40)

    # The mark is painted, not loaded, so a blank widget would be invisible to
    # every other check here: the widget is still visible, still the right size
    # and still screenshots as an empty rectangle.
    mark, colours, accent = mark_stats(win.logo.grab())
    check("the header mark is really painted",
          mark > 0.80 and colours >= 20 and accent > 20,
          f"{mark*100:.0f}% opaque, {colours} colours at a glance, {accent} accent px")
    mark_small, colours_small, accent_small = mark_stats(logo.logo_pixmap(16))
    check("the mark still reads at task-bar size",
          mark_small > 0.70 and accent_small > 4,
          f"16 px: {mark_small*100:.0f}% opaque, {accent_small} accent px")
    mark_icon, _, accent_icon = mark_stats(logo.logo_pixmap(48))
    check("the mark renders at icon size too",
          mark_icon > 0.80 and accent_icon > 15,
          f"48 px: {mark_icon*100:.0f}% opaque, {accent_icon} accent px")
    check("the window icon carries several sizes",
          not logo.logo_icon().isNull()
          and len(logo.logo_icon().availableSizes()) >= 4,
          f"{len(logo.logo_icon().availableSizes())} sizes")
    check("the window uses that icon",
          not win.windowIcon().isNull()
          and not win.windowIcon().pixmap(32, 32).isNull())

    # Every rate the window offers must survive the DSP chain plan.
    from rspsdr.dsp import AUDIO_RATE, plan_chain

    unplannable = []
    for label, rate in ui.RATES:
        stages, hop_in, hop_out = plan_chain(rate)
        r = float(rate)
        for up, down in stages:
            r = r * up / down
        if abs(r - AUDIO_RATE) > 1e-6 or hop_in <= 0 or hop_out <= 0:
            unplannable.append(f"{label} -> {r:g}")
    check("every offered sample rate plans to 48 kHz audio",
          not unplannable, ", ".join(unplannable) or
          f"{len(ui.RATES)} rates, {ui.RATES[0][0]} .. {ui.RATES[-1][0]}")

    # A rate that is not in the list used to raise ValueError while building
    # the window, which meant "--rate 7000000" could not start the GUI at all.
    odd_rate = 1_234_567
    odd = MainWindow(Settings(center_freq=settings.center_freq,
                              tuned_freq=settings.tuned_freq,
                              sample_rate=odd_rate),
                     memory_path=MEM_PATH)
    check("a sample rate outside the offered list opens the window",
          odd.cmb_rate.currentData() == odd_rate
          and odd.cmb_rate.findData(odd_rate) == odd.cmb_rate.count() - 1,
          f"combo shows {odd.cmb_rate.currentText()!r}, "
          f"{odd.cmb_rate.count()} entries")
    odd.close()

    print("== GUI smoke test ==")

    # The reported bug: a control row whose minimum width was wider than the
    # screen (the tuning row alone demanded 1469 px) made the window
    # impossible to fit or maximise.  Guard both halves of that.
    scr = QApplication.primaryScreen().availableGeometry()
    win.showMaximized()
    check("showMaximized actually maximises",
          win.isMaximized(), f"platform {QApplication.platformName()}")
    if QApplication.platformName() == "offscreen":
        print("  SKIP  maximised window fills the screen :: the offscreen "
              "platform has no window manager to honour the request")
    else:
        check("maximised window fills the screen",
              win.width() >= scr.width() - 4 and win.height() >= scr.height() - 60,
              f"client {win.width()}x{win.height()} vs screen "
              f"{scr.width()}x{scr.height()}")
    shown = win.frameGeometry()
    check("maximised window stays on the screen",
          shown.x() >= scr.x() - 1 and shown.y() >= scr.y() - 1
          and shown.width() <= scr.width() + 1,
          f"frame at +{shown.x()}+{shown.y()} {shown.width()}x{shown.height()}")
    check("window can be much smaller than the screen",
          win.minimumSizeHint().width() <= 600,
          f"content minimum width {win.minimumSizeHint().width()} px, "
          f"explicit minimum {win.minimumWidth()} px")

    # The rows wrap between *items*, so a caption added separately from the
    # control it names can be left dangling at the end of one line while the
    # control starts the next.  Every labelled group has to stay whole, at any
    # width the window can take.
    split = []
    groups = 0
    for width in (1360, 900, 780):
        win.resize(width, 739)
        QApplication.processEvents()
        for lay in win.findChildren(QHBoxLayout):
            kids = [lay.itemAt(i).widget() for i in range(lay.count())]
            kids = [k for k in kids if k is not None and k.isVisible()]
            if len(kids) < 2 or not isinstance(kids[0], QLabel):
                continue
            if width == 1360:
                groups += 1
            ys = [k.y() for k in kids]
            # >2 px means a real line break; 1 px is just baseline alignment
            # between a label and a taller spin box or slider.
            if max(ys) - min(ys) > 2:
                split.append(f"{kids[0].text()}@{width}px")
    check("no control row splits a label from its control",
          not split,
          ", ".join(split[:4]) or
          f"{groups} labelled groups whole at 1360/900/780 px")

    win.showNormal()
    win.resize(1400, 950)

    ok_open = win.start_radio()
    check("radio opened from the GUI", ok_open and win.radio.opened,
          win.lbl_device.text() if ok_open else "open failed")

    global CPU0
    CPU0 = cpu_jiffies()
    drops = {"before_nr": 0}

    def phase_measure():
        curve = win.spectrum.curve.getData()
        wf = win.waterfall._wf
        st = win.radio.status

        check("spectrum curve has data",
              curve[0] is not None and len(curve[0]) > 1000,
              f"{0 if curve[0] is None else len(curve[0])} points")
        if curve[1] is not None:
            check("spectrum values are finite dB",
                  bool(np.all(np.isfinite(curve[1]))) and float(curve[1].max()) < 20,
                  f"range {curve[1].min():.1f}..{curve[1].max():.1f} dB")
        check("waterfall holds real signal (not all floor)",
              float(wf.max()) > -100.0, f"max {wf.max():.1f} dB")
        # The picture is sized to the plot it is drawn in -- one row per pixel
        # of height, one column per pixel of width -- rather than to a fixed
        # 240x800, which is both what SDR++ does and the only shape at which
        # nothing has to be stretched by the rasteriser.
        check("waterfall is sized to its own plot",
              wf.shape[0] == win.waterfall.rows()
              and wf.shape[1] == win.waterfall.cols()
              and ui.WF_MIN_ROWS <= wf.shape[0] <= ui.WF_MAX_ROWS,
              f"{wf.shape[0]} rows x {wf.shape[1]} columns, "
              f"{win.waterfall.bins()} bins on offer")
        check("marker sits on the tuned frequency",
              abs(win.spectrum.marker.value() - settings.tuned_freq / 1e6) < 1e-6)
        check("spectrum frames rendered", win.frames_drawn > 20,
              f"{win.frames_drawn} repaints, {win.render_seconds*1000:.1f} ms each")
        print(f"        dsp {st.cpu_load*100:.1f}% of one core, "
              f"{st.sample_rate/1e6:g} MSps, dropped {st.dropped_chunks}")
        check("DSP fits in realtime with headroom", st.cpu_load < 0.6,
              f"{st.cpu_load*100:.1f}% of one core")
        check_no_drops("no dropped RF blocks", st)

        # Exercise the controls the way a user would.
        win.spn_freq.setValue(9_950_000 / 1e6)
        check("tuning updates the radio",
              abs(win.radio.settings.tuned_freq - 9_950_000) < 1.0,
              f"{win.radio.settings.tuned_freq/1e6:.6f} MHz")

        win._select_mode("usb")
        check("mode change reaches the demodulator",
              win.radio.demod.mode == "usb" and win.radio.settings.mode == "usb",
              win.radio.demod.mode)

        win.cmb_bw.setCurrentIndex(2)
        check("filter width change applied",
              abs(win.radio.demod.bw - float(win.cmb_bw.currentData())) < 1.0,
              f"{win.radio.demod.bw:.0f} Hz")

        # The passband marker has to show the band the demodulator really uses.
        # Drawn symmetrically for every mode it is the *AM* shape, which made
        # USB and LSB look like AM on screen.
        def band_khz(mode, bw):
            win.spectrum.set_marker(win.settings.tuned_freq, bw, mode,
                                    win.settings.cw_pitch)
            lo, hi = win.spectrum.band.getRegion()
            dial = win.settings.tuned_freq / 1e6
            return round((lo - dial) * 1000.0, 3), round((hi - dial) * 1000.0, 3)

        def near(pair, want):
            return (abs(pair[0] - want[0]) < 0.01 and abs(pair[1] - want[1]) < 0.01)

        got_am = band_khz("am", 6000.0)
        check("AM's passband is drawn either side of the dial",
              near(got_am, (-3.0, 3.0)), str(got_am))
        got_usb = band_khz("usb", 2400.0)
        check("USB's passband is drawn above the dial, not as AM",
              near(got_usb, (0.0, 2.4)),
              f"{got_usb} kHz around the dial, AM would be (-1.2, 1.2)")
        got_lsb = band_khz("lsb", 2400.0)
        check("LSB's passband is drawn below the dial",
              near(got_lsb, (-2.4, 0.0)), f"{got_lsb} kHz")
        # Derived, not hard-coded: a 500 Hz CW filter spans +/-250 Hz about the
        # dial *plus the pitch*, and the dial itself would give (-0.25, +0.25).
        pitch_hz = win.settings.cw_pitch
        got_cw = band_khz("cw", 500.0)
        check("CW's passband is drawn on its pitch offset, not on the dial",
              near(got_cw, ((pitch_hz - 250.0) / 1000.0,
                            (pitch_hz + 250.0) / 1000.0)),
              f"{got_cw} kHz = the 500 Hz filter centred on the "
              f"{pitch_hz:.0f} Hz pitch; on the dial it would be (-0.25, 0.25)")

        win.sld_if.setValue(55)
        check("IF gain slider applied",
              abs(win.radio.settings.if_gain - 55.0) < 0.01,
              f"{win.radio.settings.if_gain:.0f} dB")

        win.chk_agc.setChecked(True)
        check("RF AGC toggles", win.radio.settings.rf_agc is True)

        win.cmb_fft.setCurrentIndex(2)
        check("FFT size change applied", win.radio.spectrum.n_fft == 4096,
              str(win.radio.spectrum.n_fft))

        # Pick the 20m entry out of the combo the way the user would click it.
        idx = next(i for i in range(win.cmb_band.count())
                   if getattr(win.cmb_band.itemData(i), "name", None) == "20m")
        win.cmb_band.setCurrentIndex(idx)
        check("band jump retunes",
              abs(win.radio.settings.tuned_freq - win.settings.tuned_freq) < 1.0
              and abs(win.settings.tuned_freq - 14_175_000) < 500_000,
              f"{win.settings.tuned_freq/1e6:.6f} MHz, mode {win.radio.demod.mode}")

        win.spn_center.setValue(14_200_000 / 1e6)
        check("centre control retunes the hardware",
              abs(win.radio.settings.center_freq - 14_200_000) < 1.0,
              f"{win.radio.settings.center_freq/1e6:.6f} MHz")

        win.spectrum.tuned.emit(14_070_000.0)   # exactly what a click emits
        check("click-to-tune path works",
              abs(win.radio.settings.tuned_freq - 14_070_000) < 1.0,
              f"{win.radio.settings.tuned_freq/1e6:.6f} MHz")

        # ---- noise reduction -----------------------------------------
        check("NR starts switched off",
              win.radio.demod.nr.enabled is False
              and win.sld_nr.isEnabled() is False)

        drops["before_nr"] = win.radio.status.dropped_chunks
        win.chk_nr.setChecked(True)
        check("enabling NR reaches the demodulator",
              win.radio.demod.nr.enabled is True
              and win.radio.settings.nr is True)
        check("NR strength slider enables with it", win.sld_nr.isEnabled())

        win.sld_nr.setValue(75)
        check("NR strength reaches the demodulator",
              abs(win.radio.demod.nr.strength - 0.75) < 1e-9,
              f"{win.radio.demod.nr.strength:.2f}")

        # ---- memory channels -----------------------------------------
        check("memory list starts empty and disabled",
              win.cmb_mem.count() == 0 and not win.btn_mem_recall.isEnabled(),
              win.lbl_mem.text())

        win.spn_freq.setValue(14_074_000 / 1e6)
        win._select_mode("usb")
        win.spn_pitch.setValue(640.0)
        ch = win._current_channel()
        ch.name = "20m FT8"
        win._mem_write(ch)
        check("saving a memory lists it",
              len(win.memory) == 1 and win.cmb_mem.count() == 1
              and "20m FT8" in win.cmb_mem.itemText(0), win.cmb_mem.itemText(0))
        check("memory controls become usable",
              win.btn_mem_recall.isEnabled() and win.btn_mem_update.isEnabled())

        on_disk = MemoryStore(MEM_PATH)
        check("memory is persisted between runs",
              len(on_disk) == 1 and on_disk[0].name == "20m FT8"
              and abs(on_disk[0].freq - 14_074_000) < 1e-6
              and on_disk[0].mode == "usb" and on_disk[0].cw_pitch == 640.0,
              repr(on_disk[0]) if len(on_disk) else "empty")

        # Move somewhere else entirely, then recall.
        win.spn_freq.setValue(7_150_000 / 1e6)
        win._select_mode("lsb")
        win.spn_pitch.setValue(900.0)
        win._mem_recall()
        check("recall restores frequency, mode, filter and pitch",
              abs(win.radio.settings.tuned_freq - 14_074_000) < 1.0
              and win.radio.demod.mode == "usb"
              and abs(win.radio.demod.bw - 2400.0) < 1.0
              and abs(win.radio.demod.pitch - 640.0) < 1e-6,
              f"{win.radio.settings.tuned_freq/1e6:.6f} MHz "
              f"{win.radio.demod.mode} {win.radio.demod.bw:.0f} Hz "
              f"pitch {win.radio.demod.pitch:.0f}")
        check("mode buttons follow the recall",
              win.mode_buttons["usb"].isChecked()
              and not win.mode_buttons["lsb"].isChecked())

        # Overwrite it from the current settings, as the Update button does.
        win.spn_freq.setValue(14_080_000 / 1e6)
        win._mem_update()
        check("update rewrites the selected memory in place",
              len(win.memory) == 1
              and abs(win.memory[0].freq - 14_080_000) < 1.0,
              f"{win.memory[0].freq/1e6:.6f} MHz")

        from PyQt6.QtWidgets import QMessageBox   # noqa: E402
        saved_question = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Yes)
        try:
            win._mem_delete()
        finally:
            QMessageBox.question = saved_question
        check("delete removes the memory and persists",
              len(win.memory) == 0 and len(MemoryStore(MEM_PATH)) == 0
              and win.cmb_mem.count() == 0)

        # Leave one in place so the screenshot shows a populated list.
        for name, freq, mode in (("40m CW", 7_030_000, "cw"),
                                 ("20m SSB", 14_250_000, "usb"),
                                 ("WWV 10", 10_000_000, "am")):
            win.spn_freq.setValue(freq / 1e6)
            win._select_mode(mode)
            c = win._current_channel()
            c.name = name
            win._mem_write(c)
        check("several memories list together",
              win.cmb_mem.count() == 3 and "3 saved" in win.lbl_mem.text(),
              win.lbl_mem.text())

        # ---- waterfall levels, colour and zoom ------------------------
        span = float(win.settings.sample_rate)
        centre = win.radio.status.center
        lo, hi = win._view(centre, span)
        check("zoom 1x shows the whole receiver span",
              abs((hi - lo) - span) < 1.0 and win._zoom == 1,
              f"{(hi - lo)/1e6:.4f} MHz of {span/1e6:g} MSps")

        # The zoom has to be usable without opening a list, and the user must
        # be able to see what the controls did.
        # "Visible" in Qt does not mean "inside the window": a row clipped off
        # the bottom of a too-short window is still isVisible().  Check the
        # widgets are actually within the client area, at the smallest size the
        # window can take as well as the current one.
        was = win.size()
        clipped = []
        for w_px, h_px in ((was.width(), was.height()), (780, 560)):
            win.resize(w_px, h_px)
            QApplication.processEvents()
            for name, wdg in (("+", win.btn_zoom_in), ("-", win.btn_zoom_out),
                              ("Fit", win.btn_zoom_fit), ("readout", win.lbl_zoom),
                              ("Detail", win.cmb_wfdetail)):
                tl = wdg.mapTo(win, QPoint(0, 0))
                if not (0 <= tl.x() and 0 <= tl.y()
                        and tl.x() + wdg.width() <= win.width()
                        and tl.y() + wdg.height() <= win.height()):
                    clipped.append(f"{name} at +{tl.x()}+{tl.y()} in "
                                   f"{win.width()}x{win.height()}")
        win.resize(was)
        QApplication.processEvents()
        check("the zoom controls are on screen and enabled",
              not clipped and win.btn_zoom_in.isEnabled()
              and win.btn_zoom_fit.isEnabled(),
              ", ".join(clipped) or
              f"+ / - / Fit inside the window at {was.width()}x{was.height()} "
              f"and at 780x560, readout “{win.lbl_zoom.text()}”")
        check("the zoom readout states the visible width",
              win.lbl_zoom.text() == f"{span/1e6:g} MHz", win.lbl_zoom.text())

        win.btn_zoom_in.click()
        check("the Zoom + button magnifies",
              win._zoom == 2 and int(win.cmb_zoom.currentData()) == 2,
              f"zoom {win._zoom}x, readout “{win.lbl_zoom.text()}”")
        check("the readout follows the buttons",
              win.lbl_zoom.text() == f"{span/2e6:g} MHz", win.lbl_zoom.text())
        win.btn_zoom_out.click()
        check("the Zoom - button widens again", win._zoom == 1,
              f"zoom {win._zoom}x, readout “{win.lbl_zoom.text()}”")
        win.btn_zoom_out.click()                        # already at 1x
        check("zooming out at the limit is harmless",
              win._zoom == 1 and int(win.cmb_zoom.currentData()) == 1,
              f"zoom {win._zoom}x")

        # A wheel over the waterfall, delivered as a real event rather than by
        # emitting the signal, so the handler itself is exercised.  It has to
        # go to the *viewport*: that is where Qt sends a wheel event over a
        # QGraphicsView, and sending it to the plot widget instead silently
        # reaches nothing (which is how this check first failed).
        app.sendEvent(win.waterfall.viewport(), QWheelEvent(
            QPointF(20.0, 20.0), QPointF(20.0, 20.0), QPoint(0, 0),
            QPoint(0, 120), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))
        check("the wheel over the waterfall zooms it", win._zoom == 2,
              f"zoom {win._zoom}x, readout “{win.lbl_zoom.text()}”")
        app.sendEvent(win.waterfall.viewport(), QWheelEvent(
            QPointF(20.0, 20.0), QPointF(20.0, 20.0), QPoint(0, 0),
            QPoint(0, -120), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))
        check("the wheel zooms back out too", win._zoom == 1,
              f"zoom {win._zoom}x, readout “{win.lbl_zoom.text()}”")
        win.btn_zoom_fit.click()
        check("Fit puts the whole span back",
              win._zoom == 1 and win.lbl_zoom.text() == f"{span/1e6:g} MHz",
              f"zoom {win._zoom}x, readout “{win.lbl_zoom.text()}”")

        # ---- the two plots must share one frequency axis ---------------
        # They are separate widgets and the spectrum labels its left axis in dB
        # while the waterfall draws no values on its, so their plot areas used
        # to start 57 px apart.  Then a carrier sat at a different x in the two
        # pictures and a click landed tens of kHz from the frequency the screen
        # appeared to point at.
        areas = {}
        for name, plot in (("spectrum", win.spectrum),
                           ("waterfall", win.waterfall)):
            r = plot.plotItem.vb.sceneBoundingRect()
            areas[name] = (r.x(), r.x() + r.width())
        check("the spectrum and waterfall share the same plot area",
              abs(areas["spectrum"][0] - areas["waterfall"][0]) < 1.0
              and abs(areas["spectrum"][1] - areas["waterfall"][1]) < 1.0,
              f"spectrum {areas['spectrum'][0]:.1f}..{areas['spectrum'][1]:.1f} px, "
              f"waterfall {areas['waterfall'][0]:.1f}..{areas['waterfall'][1]:.1f} px")

        worst = 0.0
        for x in (200, 700, 1200):
            here = [plot.plotItem.vb.mapSceneToView(
                QPointF(float(x), plot.height() / 2)).x()
                for plot in (win.spectrum, win.waterfall)]
            worst = max(worst, abs(here[1] - here[0]) * 1e6)
        check("a pixel means the same frequency in both plots", worst < 500.0,
              f"worst disagreement {worst:.0f} Hz across the span")

        # A real click on the waterfall must land where the screen says it will.
        was_tuned = win.settings.tuned_freq
        clicks = []
        win.waterfall.tuned.connect(lambda f: clicks.append(f))
        click_x = 900
        where = QPointF(float(click_x), win.waterfall.height() / 2)
        for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            app.sendEvent(win.waterfall.viewport(), QMouseEvent(
                kind, where, Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        want = win.waterfall.plotItem.vb.mapSceneToView(where).x() * 1e6
        check("clicking the waterfall tunes to the frequency under the cursor",
              bool(clicks) and abs(clicks[0] - want) < 1.0,
              f"x={click_x} px tuned "
              f"{(clicks[0] if clicks else float('nan'))/1e6:.6f} MHz, "
              f"axis says {want/1e6:.6f} MHz")
        win.spn_freq.setValue(was_tuned / 1e6)      # leave the state as it was

        win.cmb_zoom.setCurrentIndex(2)                 # 4x
        lo4, hi4 = win._view(centre, span)
        check("zoom narrows the window to span/N",
              win._zoom == 4 and abs((hi4 - lo4) - span / 4.0) < 1.0,
              f"zoom {win._zoom}x gives {(hi4 - lo4)/1e3:.1f} kHz")
        # Strictly *below* the floor, not merely at it: rows filled with the
        # previous window's floor lit up at full brightness as soon as the
        # tracked floor moved down past them, drawing a bright band across the
        # foot of the waterfall.
        check("changing zoom clears the stale waterfall history",
              float(win.waterfall._wf.max()) < win.waterfall.levels()[0],
              f"max {win.waterfall._wf.max():.1f} dB vs floor "
              f"{win.waterfall.levels()[0]:.1f} dB")
        # The guard for the shadowed-method trap: PlotWidget delegates a
        # 'clear' call to PlotItem, which destroys every item in the plot.
        # Reading the data array would not reveal that; the scene would.
        check("the waterfall image is still attached after a zoom change",
              win.waterfall.img.scene() is not None,
              "images in scene: "
              f"{0 if win.waterfall.img.scene() is None else 1}")

        win.chk_wfauto.setChecked(False)
        win.spn_wflo.setValue(-95.0)
        win.spn_wfhi.setValue(-35.0)
        check("manual levels enable Floor/Ceiling and disable Contrast",
              win.spn_wflo.isEnabled() and win.spn_wfhi.isEnabled()
              and not win.spn_contrast.isEnabled())
        check("manual levels reach the waterfall",
              tuple(round(v, 3) for v in win.waterfall.levels()) == (-95.0, -35.0),
              str(win.waterfall.levels()))

        win.spn_wflo.setValue(-30.0)
        win.spn_wfhi.setValue(-80.0)                    # ceiling below floor
        deg_lo, deg_hi = win.waterfall.levels()
        check("a ceiling below the floor cannot make a degenerate image",
              deg_hi > deg_lo, f"{deg_lo:.1f}..{deg_hi:.1f} dB")

        win.chk_wfauto.setChecked(True)
        check("auto levels hand back to the Contrast control",
              win.spn_contrast.isEnabled() and not win.spn_wflo.isEnabled())
        tracked = win.waterfall.levels()
        check("the level spin ranges can display the tracked values",
              win.spn_wflo.minimum() <= tracked[0] <= win.spn_wflo.maximum()
              and win.spn_wfhi.minimum() <= tracked[1] <= win.spn_wfhi.maximum(),
              f"tracked {tracked[0]:.1f}/{tracked[1]:.1f} dB, floor spin "
              f"{win.spn_wflo.minimum()}..{win.spn_wflo.maximum()}")

        win.cmb_cmap.setCurrentIndex(1)                 # Viridis
        win.chk_cmrev.setChecked(True)
        check("colour map selection is stored",
              win.waterfall._cmap_key == "viridis"
              and win.waterfall._cmap_rev is True,
              f"{win.waterfall._cmap_key} reverse={win.waterfall._cmap_rev}")

        # ---- waterfall frequency detail -------------------------------
        # The picture used to be built from at most 800 columns whatever the
        # plot was, so an 8192- or 16384-point FFT measured far more detail
        # than the waterfall could show and the 800 columns were stretched back
        # across the window.  "Screen" -- the default -- is SDR++'s answer: the
        # row is as wide as the plot has pixels.  The widget is driven with
        # frames of a known bin count so the column arithmetic is checked
        # exactly, rather than through whatever the live radio is delivering.
        def detail_index(cols: int) -> int:
            return next(i for i in range(win.cmb_wfdetail.count())
                        if win.cmb_wfdetail.itemData(i) == cols)

        def fake_frame(bins: int, value: float, peak_at: int | None = None):
            db = np.full(bins, value, dtype=np.float32)
            if peak_at is not None:
                db[peak_at] = value + 80.0          # one narrow carrier
            win.waterfall.push(centre, db, span,
                               centre - span, centre + span)
            return db

        def plot_pixels() -> int:
            """The plot area's own width, which is what Screen has to match."""
            wf = win.waterfall
            return int(round(wf.plotItem.vb.width() * wf.devicePixelRatioF()))

        offered = [c for _, c in ui.WF_DETAILS if c]
        check("the detail list starts at Screen and keeps the fixed sizes",
              win.cmb_wfdetail.itemData(0) == ui.SCREEN_DETAIL
              and ui.WF_MAX_COLS == max(offered)
              and offered == sorted(set(offered)),
              f"Screen, then {offered[0]}..{ui.WF_MAX_COLS} columns")

        # Screen is not a column count, it is the plot's own width, so it has to
        # answer with the width the plot actually has.
        win.cmb_wfdetail.setCurrentIndex(detail_index(ui.SCREEN_DETAIL))
        fake_frame(16384, -80.0)
        check("Screen matches a row to the plot's own pixels",
              win.waterfall.cols() == min(16384, plot_pixels()),
              f"{win.waterfall.cols()} columns for {plot_pixels()} px of plot")
        check("and the history is one row per pixel of height",
              win.waterfall.rows() == win.waterfall._wf.shape[0]
              and ui.WF_MIN_ROWS <= win.waterfall.rows() <= ui.WF_MAX_ROWS,
              f"{win.waterfall.rows()} rows, bounds "
              f"{ui.WF_MIN_ROWS}..{ui.WF_MAX_ROWS}")

        win.cmb_wfdetail.setCurrentIndex(detail_index(ui.WF_MAX_COLS))
        fake_frame(16384, -80.0)
        check("a fixed detail setting uses every column it was asked for",
              win.waterfall.cols() == ui.WF_MAX_COLS,
              f"{win.waterfall.cols()} columns from 16384 bins")

        win.cmb_wfdetail.setCurrentIndex(detail_index(800))
        fake_frame(16384, -80.0)
        check("and a smaller one pools the row down",
              win.waterfall.cols() == 800, f"{win.waterfall.cols()} columns")

        fake_frame(512, -80.0)
        check("a row is never stretched beyond the bins behind it",
              win.waterfall.cols() == 512,
              f"{win.waterfall.cols()} columns from 512 bins")

        # Peak, not mean, when the bins outnumber the columns.  This is what
        # SDR++'s doZoom does, and it is the whole difference between a weak
        # carrier drawn as a line and the same carrier averaged into the noise:
        # ten bins at -90 dB with one at -10 read -10 under a maximum and
        # -82 under a mean.
        win.cmb_wfdetail.setCurrentIndex(detail_index(1600))
        fake_frame(16384, -90.0, peak_at=8192)
        win.waterfall._wf[:] = ui.WF_BLANK_DB           # only the newest row real
        fake_frame(16384, -90.0, peak_at=8192)
        row = win.waterfall._wf[0]
        marked = float(row.max())
        mean_rounded = float(row.mean())
        check("a lone carrier keeps its strength when the row is pooled",
              marked > -20.0 and mean_rounded < -60.0,
              f"carrier {marked:.1f} dB in a row averaging {mean_rounded:.1f} dB "
              f"over {win.waterfall.cols()} columns")

        # Only the resolution changes here, never the span the rows were taken
        # over, so the history is still meaningful and has to survive the move
        # instead of blanking the picture every time the control is touched.
        win.cmb_wfdetail.setCurrentIndex(detail_index(4800))
        wf = win.waterfall._wf
        check("a detail change resamples the history instead of discarding it",
              wf.shape[0] == win.waterfall.rows() and wf.shape[1] == 4800
              and abs(float(wf[0].max()) - marked) < 1e-3,
              f"carrier {marked:.1f} dB -> {float(wf[0].max()):.1f} dB over "
              f"{wf.shape[1]} columns")
        check("and the bright row did not merely come back blank",
              float(wf.max()) > -100.0, f"max {float(wf.max()):.1f} dB")

        # The picture is pixels, so the levels are baked in rather than applied
        # when the picture is drawn.  The pixels have to be the ones the colour
        # map gives for the dB value, and a level change has to recolour the
        # rows already on screen -- otherwise the picture would keep the old
        # window until it scrolled away.
        lut = win.waterfall._lut
        lo, hi = win.waterfall.levels()
        newest = win.waterfall._wf[0]               # after the resample
        col = int(np.argmax(newest))
        value = float(newest[col])
        idx = max(0, min(255, int(round((value - lo) * 255.0 / (hi - lo)))))
        px = tuple(int(v) for v in win.waterfall._rgba[0, col])
        check("the picture's pixels are the colour map at those levels",
              px == tuple(int(v) for v in lut[idx]),
              f"rgba {px} for {value:.1f} dB, colour index {idx}")

        # A history of real rows, so this is about the levels and not about the
        # blank rows, which stay black whatever the levels say.
        win.waterfall._wf[:] = -60.0
        win.waterfall._repaint_history()
        oldest = tuple(int(v) for v in win.waterfall._rgba[-1, 0])
        win.chk_wfauto.setChecked(False)                # hand the levels over
        win.spn_wflo.setValue(-100.0)
        win.spn_wfhi.setValue(-30.0)
        recoloured = tuple(int(v) for v in win.waterfall._rgba[-1, 0])
        check("a level change recolours the rows already drawn",
              recoloured != oldest and oldest != (0, 0, 0, 255)
              and win.waterfall.levels() == (-100.0, -30.0),
              f"oldest row {oldest} -> {recoloured} at levels "
              f"{win.waterfall.levels()}")
        win.chk_wfauto.setChecked(True)

        # A row with no data in it yet must be black, not whatever colour sits
        # at the bottom of the scale: the brightest entry of a reversed map drew
        # a band of full-brightness pixels across a history that had not filled
        # in yet.
        win.waterfall.set_colormap("viridis", True)
        win.waterfall.clear_history()
        bright = int(win.waterfall._rgba[..., :3].max())
        check("a cleared history is black under a reversed colour map",
              bright == 0, f"brightest cleared pixel {bright}")
        win.waterfall.set_colormap("spectrum", False)

        # What is *drawn* has to follow the data, not merely the array behind
        # it.  Handing an ImageItem the same buffer every frame is the classic
        # way to end up with an image that is perfectly up to date and never
        # re-rendered, so this reads the pixels the plot produced.
        win.cmb_wfdetail.setCurrentIndex(detail_index(ui.SCREEN_DETAIL))
        win.waterfall.set_levels(False, 30.0, -100.0, -40.0)
        # The view is pinned to the window the row is taken over, so where the
        # carrier should land follows from the axis mapping alone.  (The timer
        # keeps re-centring the view on the live receiver otherwise.)
        live = win.radio.status.center
        win.waterfall.set_view(live - span, live + span)
        flat = np.full(16384, -110.0, dtype=np.float32)
        flat[8192] = -20.0                              # one carrier, on centre
        win.waterfall.push(live, flat, span, live - span, live + span)
        shot = win.waterfall.grab().toImage()
        sw, sh = shot.width(), shot.height()
        pix = np.frombuffer(shot.constBits().asstring(shot.sizeInBytes()),
                            np.uint8).reshape(sh, shot.bytesPerLine() // 4, 4)[:, :sw, :]
        # The newest line is the first one drawn, at the top of the plot; the
        # leftmost columns are the frequency scale, not the picture.
        band = pix[0:14, ui.PLOT_AXIS_WIDTH:, :3].sum(axis=2).max(axis=0)
        found = ui.PLOT_AXIS_WIDTH + int(np.argmax(band))
        scene = win.waterfall.plotItem.vb.mapViewToScene(QPointF(live / 1e6, 1.0))
        want = win.waterfall.mapFromScene(scene).x()
        check("the drawn picture puts a carrier where the axis says it is",
              abs(found - want) <= 3
              and float(band.max()) > 1.5 * float(np.median(band)),
              f"brightest column {found} px, carrier at {want:.0f} px "
              f"of {sw} px")
        win.waterfall.set_levels(True, 30.0, -110.0, -40.0)
        win.waterfall.set_view(*win._view(live, span))

        # ---- the picture is drawn over the span the axis shows -----------
        # The regression guard for the trap behind "tuning is not accurate".
        # An ImageItem scales by the image it already holds, so a rectangle
        # applied before the new array was handed over was built from the
        # *previous* column count: the first frame was drawn 37.6 MHz wide in a
        # 2 MHz window, and the width ratio was out again after any resize or
        # detail change.  The axis, and every click and drag, stayed correct
        # throughout -- so the picture showed a signal at a frequency that was
        # nothing like the one the window said, and tuning to the carrier you
        # could see landed you somewhere else.
        def scene_x(freq: float) -> float:
            """Where the axis and the plot's own rectangle put this frequency.

            Worked out from the axis range and the plot rectangle, not from the
            plot's scene transform, so this is an independent reference: a plot
            whose transform says one thing and whose axis says another fails.
            """
            lo, hi = win.waterfall.plotItem.vb.viewRange()[0]
            r = win.waterfall.plotItem.vb.sceneBoundingRect()
            return r.x() + (freq / 1e6 - lo) / (hi - lo) * r.width()

        for detail in (ui.SCREEN_DETAIL, 2400, ui.WF_MAX_COLS):
            win.cmb_wfdetail.setCurrentIndex(detail_index(detail))
            win.waterfall.set_view(centre - span, centre + span)
            fake_frame(16384, -90.0, peak_at=8192)
            win.waterfall.grab()            # paint, so the plot's mapping is current
            img = win.waterfall.img
            left = img.mapToScene(QPointF(0.0, 0.0)).x()
            right = img.mapToScene(QPointF(float(win.waterfall.cols()), 0.0)).x()
            want_l, want_r = scene_x(centre - span), scene_x(centre + span)
            check(f"the picture is drawn over the span the axis shows "
                  f"({detail or 'Screen'})",
                  abs(left - want_l) <= 1.5 and abs(right - want_r) <= 1.5,
                  f"{win.waterfall.cols()} columns drawn over {left:.0f}..{right:.0f} px, "
                  f"the axis puts that window at {want_l:.0f}..{want_r:.0f} px")

        # ---- a drag follows the pointer, and the window stays put ---------
        # The other half of the same complaint.  A recentre mid-gesture moved
        # the window out from under the pointer, so a drag across 90 % of the
        # half-span jumped half a window and then tuned in steps of it, with
        # the frequency under the cursor changing as the user dragged.  The
        # window may follow the dial -- once the button is up.
        live = win.radio.settings.center_freq
        half = win.radio.settings.sample_rate / (2.0 * max(1, win._zoom))
        win.waterfall.set_view(live - half, live + half)
        win.waterfall.push(live, np.full(16384, -110.0, np.float32),
                           win.radio.settings.sample_rate, live - half, live + half)
        app.processEvents()

        # Where a frequency sits, worked out from the axis range and the plot
        # rectangle rather than from the transform the widget itself uses to
        # answer a click -- otherwise this would be checking the mapping against
        # itself and could not fail.
        def px_of(freq: float) -> int:
            lo, hi = win.waterfall.plotItem.vb.viewRange()[0]
            r = win.waterfall.plotItem.vb.sceneBoundingRect()
            frac = (freq / 1e6 - lo) / (hi - lo)
            return int(round(r.x() + frac * r.width()))

        def freq_at(x: float) -> float:
            """What the axis and the plot rectangle put under this pixel."""
            lo, hi = win.waterfall.plotItem.vb.viewRange()[0]
            r = win.waterfall.plotItem.vb.sceneBoundingRect()
            return (lo + (x - r.x()) / r.width() * (hi - lo)) * 1e6

        def drag_to(fracs):
            """Press, then drag through the fractions of the half-span given."""
            y = float(win.waterfall.height() // 3)
            x0 = px_of(live + fracs[0] * half)
            app.sendEvent(win.waterfall.viewport(), QMouseEvent(
                QEvent.Type.MouseButtonPress, QPointF(float(x0), y),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier))
            app.processEvents()
            ends = {}
            for frac in fracs[1:]:
                x = px_of(live + frac * half)
                ends[frac] = freq_at(x)         # measured before the move
                app.sendEvent(win.waterfall.viewport(), QMouseEvent(
                    QEvent.Type.MouseMove, QPointF(float(x), y),
                    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier))
                app.processEvents()
            return ends

        ends = drag_to((0.4, 0.7, 0.98))        # 0.98 is past the recentre point
        check("a drag on the waterfall leaves the window where it was",
              abs(win.spn_center.value() * 1e6 - live) < 1.0
              and abs((win.waterfall._hi_freq - win.waterfall._lo_freq)
                      - 2.0 * half) < 1.0,
              f"centre spin {win.spn_center.value()*1e6/1e6:.6f} MHz (receiver at "
              f"{live/1e6:.6f}), window "
              f"{(win.waterfall._hi_freq - win.waterfall._lo_freq)/1e3:.0f} kHz "
              f"wide")
        held = win.spn_freq.value() * 1e6
        check("and tunes to the frequency the pointer is over",
              abs(held - ends[0.98]) < half * 0.02,
              f"{(held - live)/1e3:+.1f} kHz from the receiver centre, the "
              f"window says {(ends[0.98] - live)/1e3:+.1f} kHz")
        app.sendEvent(win.waterfall.viewport(), QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(float(px_of(live + 0.98 * half)),
                    float(win.waterfall.height() // 3)),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        check("and lets the window follow the dial once the button is up",
              abs(win.spn_center.value() * 1e6 - ends[0.98]) < half * 0.05,
              f"receiver centre {win.spn_center.value():.6f} MHz for a dial at "
              f"{ends[0.98]/1e6:.6f} MHz")
        win.spn_freq.setValue(was_tuned / 1e6)
        win.spn_center.setValue(live / 1e6)     # put the receiver back
        win.waterfall.set_view(*win._view(live, span))

        # The readout is how anyone can tell whether the FFT or the picture is
        # the limit, which the column count on its own does not say.
        win._wf_detail_readout()
        check("the readout says what a column is worth in Hz",
              "kHz per column" in win.lbl_status.text(),
              win.lbl_status.text())

        win.cmb_wfdetail.setCurrentIndex(detail_index(ui.SCREEN_DETAIL))
        fake_frame(16384, -80.0)
        check("Screen follows the plot again",
              win.waterfall.cols() == min(16384, plot_pixels()),
              f"{win.waterfall.cols()} columns, "
              f"{win.waterfall.bins()} bins on offer")

        # ---- the picture follows the plot it is in ----------------------
        # Dragging the splitter is how a user changes the waterfall's height,
        # and both dimensions are supposed to follow it.
        split = win.waterfall.parentWidget()
        check("the waterfall lives in a splitter", isinstance(split, QSplitter))
        if isinstance(split, QSplitter):
            rows_before, cols_before = win.waterfall.rows(), win.waterfall.cols()
            px_before = plot_pixels()
            above, below = split.sizes()          # [spectrum, waterfall]
            split.setSizes([max(140, above - 90), below + 90])
            # Width decides the columns, so widen the window too -- unless it is
            # already as wide as the screen, in which case the invariant is all
            # there is to check.
            win.resize(min(win.width() + 120, 1900), win.height())
            QApplication.processEvents()
            check("a taller plot means more rows of history",
                  win.waterfall.rows() > rows_before,
                  f"{rows_before} -> {win.waterfall.rows()} rows")
            fake_frame(16384, -80.0)              # bins to spare: width decides
            check("and Screen tracks whatever width the plot ended up with",
                  win.waterfall.cols() == min(16384, plot_pixels())
                  and (plot_pixels() == px_before
                       or win.waterfall.cols() > cols_before),
                  f"{cols_before} -> {win.waterfall.cols()} columns for "
                  f"{px_before} -> {plot_pixels()} px of plot")
            split.setSizes([above, below])
            QApplication.processEvents()

        # ---- the FFT sizes the resolution actually comes from -----------
        # A bin is span/n_fft wide and the waterfall draws the visible slice of
        # them, so a bigger FFT is a finer waterfall.  The two large sizes only
        # became affordable when the transform moved to scipy.fft.
        sizes = [win.cmb_fft.itemData(i) for i in range(win.cmb_fft.count())]
        check("the FFT list reaches the sizes a deep zoom needs",
              sizes == sorted(sizes) and max(sizes) >= 32768
              and min(sizes) >= 1024,
              f"{min(sizes)}..{max(sizes)} points, {len(sizes)} choices")
        check("and keeps the sample rate the span expects by default",
              int(win.cmb_fft.currentData()) >= 4096,
              f"{win.cmb_fft.currentData()} points")

        # ---- one repaint has to fit in the repaint interval -------------
        # The GUI and the DSP share four cores, so what the picture costs per
        # frame is not a private matter: a repaint that overruns REPAINT_MS
        # stutters the display *and* starves the DSP thread of CPU.  This is the
        # whole path a frame takes -- row building, the level tracking, the
        # colour mapping and both plots' real rasterised paints.
        bench_db = np.random.randn(16384).astype(np.float32) - 90.0

        def repaint_once():
            win.waterfall.push(centre, bench_db, span, centre - span, centre + span)
            win.spectrum.update_spectrum(centre, bench_db, span,
                                         centre - span, centre + span)
            win.grab()

        repaint_once()                                  # warm up the caches
        t0 = time.perf_counter()
        for _ in range(5):
            repaint_once()
        per_frame = (time.perf_counter() - t0) / 5.0 * 1e3
        check("a repaint of the whole window fits in the repaint interval",
              per_frame < ui.REPAINT_MS,
              f"{per_frame:.1f} ms of the {ui.REPAINT_MS} ms budget, "
              f"{win.waterfall.cols()} columns")

        # ---- the helper that decides what is on screen ------------------
        # visible_bins picks which slice of the full-span FFT the plots show,
        # and a zoomed view has to show real bins at full resolution rather
        # than a stretched copy of the whole span.
        probe = np.linspace(-120.0, -20.0, 4096)
        centre, span = 10_000_000.0, 2_000_000.0
        bin_hz = span / probe.size
        vis_f, vis_db = ui.visible_bins(centre, probe, span,
                                        centre - 250_000, centre + 250_000)
        check("visible_bins slices the spectrum at full resolution",
              vis_f.size == vis_db.size and 1023 <= vis_f.size <= 1025
              and abs(vis_f[0] - (centre - 250_000)) < 1.0,
              f"{vis_f.size} bins over "
              f"{(vis_f[-1] - vis_f[0]) / 1e3:.1f} kHz, first at "
              f"{(vis_f[0] - centre) / 1e3:+.3f} kHz")
        check("a zoomed bin is the same width as a full-span bin",
              abs((vis_f[1] - vis_f[0]) - bin_hz) < 1e-6,
              f"{vis_f[1] - vis_f[0]:.4f} Hz vs {bin_hz:.4f} Hz")
        off_f, off_db = ui.visible_bins(centre, probe, span,
                                        centre - 50 * span, centre + 50 * span)
        check("visible_bins clamps a window wider than the data",
              off_f.size == probe.size and off_db.size == probe.size,
              f"{off_f.size} bins asked for 100 spans")

        # pyqtgraph hands back a shared cached colour map and reverse()
        # mutates it in place, so a reversed copy must not reverse the map for
        # the next caller.
        def ramp(cm, n=9):
            # nPts is the *third* argument of getLookupTable; passing it
            # positionally sets the start of the range instead.
            return [tuple(int(v) for v in row) for row in cm.getLookupTable(nPts=n)]

        normal = ui.build_colormap("viridis")
        flipped = ui.build_colormap("viridis", True)
        again = ui.build_colormap("viridis")
        check("a colour map starts dark and ends bright",
              sum(ramp(normal)[0]) < sum(ramp(normal)[-1]),
              f"{ramp(normal)[0]} -> {ramp(normal)[-1]}")
        check("reversing a colour map returns the same ramp backwards",
              ramp(flipped) == ramp(normal)[::-1], f"{ramp(flipped)[0]} .. first")
        check("and does not leak into the next caller",
              ramp(again) == ramp(normal),
              "a forward map taken after one reversed copy is still forward")
        builtin = {"spectrum", "grey", "hot"}
        broken = []
        for name, key in ui.COLORMAPS:
            if key in builtin:          # built in-process, no data file involved
                continue
            try:
                pg.colormap.get(key, skipCache=True)
            except Exception as exc:
                broken.append(f"{name} ({exc.__class__.__name__})")
        check("every named colour map exists in this pyqtgraph",
              not broken, ", ".join(broken) or
              f"{len(ui.COLORMAPS) - len(builtin)} from pyqtgraph plus "
              f"{len(builtin)} drawn here")

        # ---- the lowest sample rate, on the live radio ------------------
        # A hop there is shorter than one FFT, which used to leave both
        # displays permanently blank.  Test the real radio, not just the DSP.
        drops["low_rate_frames"] = win.frames_drawn
        low = next(i for i in range(win.cmb_rate.count())
                   if win.cmb_rate.itemData(i) == 62500)
        win.cmb_rate.setCurrentIndex(low)
        QTimer.singleShot(3000, guard(phase_low_rate))

    def phase_finish():
        wf = win.waterfall._wf
        check("waterfall still updating after retunes",
              float(wf.max()) > -100.0, f"max {wf.max():.1f} dB")
        st = win.radio.status

        # ---- the zoomed view really is zoomed, on live data -----------
        span = float(win.settings.sample_rate)
        want = span / 4.0 / 1e6
        xr = win.spectrum.viewRange()[0]
        check("zoomed spectrum view shows a quarter of the span",
              abs((xr[1] - xr[0]) - want) < want * 0.35,
              f"view {xr[0]:.4f}..{xr[1]:.4f} MHz, want {want:.4f} MHz")
        curves = win.spectrum.curve.getData()
        drawn = 0.0 if curves[0] is None else float(curves[0].max() - curves[0].min())
        check("spectrum draws zoomed data, not the whole span stretched",
              curves[0] is not None and len(curves[0]) > 100 and drawn < want * 1.6,
              f"{0 if curves[0] is None else len(curves[0])} points over "
              f"{drawn:.4f} MHz")
        check("the tuned frequency is inside the zoomed window",
              xr[0] - 1e-6 <= win.settings.tuned_freq / 1e6 <= xr[1] + 1e-6,
              f"tuned {win.settings.tuned_freq/1e6:.4f} in "
              f"{xr[0]:.3f}..{xr[1]:.3f} MHz  [centre spinner "
              f"{win.spn_center.value():.4f}, radio "
              f"{win.radio.settings.center_freq/1e6:.4f}, reported "
              f"{win.radio.status.center/1e6:.4f}]")
        wf_span = win.waterfall._hi_freq - win.waterfall._lo_freq
        check("waterfall follows the zoom",
              abs(wf_span - span / 4.0) < 2000.0,
              f"waterfall span {wf_span/1e3:.1f} kHz")
        levels = win.waterfall.levels()
        check("auto levels produce a sane dB window",
              -160.0 < levels[0] < levels[1] < 40.0,
              f"{levels[0]:.1f}..{levels[1]:.1f} dB")
        check("the tracked levels are shown back to the user",
              abs(win.spn_wflo.value() - levels[0]) < 1.0
              and abs(win.spn_wfhi.value() - levels[1]) < 1.0,
              f"spins {win.spn_wflo.value():.1f}/{win.spn_wfhi.value():.1f} "
              f"vs {levels[0]:.1f}/{levels[1]:.1f}")

        # Retune while zoomed right in.  This is the case a stale centre used
        # to break: the recentre decision looked at an acknowledged centre
        # several retunes out of date, concluded the new frequency was already
        # in view, and left it entirely outside the window.
        drops["far_freq"] = win.settings.tuned_freq + 1_000_000.0
        win.spn_freq.setValue(drops["far_freq"] / 1e6)
        QTimer.singleShot(700, guard(phase_zoomed_retune))

    def phase_zoomed_retune():
        xr = win.spectrum.viewRange()[0]
        f = drops["far_freq"]
        check("a retune while zoomed brings the signal into the window",
              xr[0] - 1e-6 <= f / 1e6 <= xr[1] + 1e-6,
              f"tuned {f/1e6:.4f} in {xr[0]:.4f}..{xr[1]:.4f} MHz, "
              f"recentre target {win.spn_center.value():.4f} MHz")
        check("the receiver followed the retune while zoomed",
              abs(win.radio.settings.center_freq - f) < 1.0,
              f"{win.radio.settings.center_freq/1e6:.6f} MHz")
        span = float(win.settings.sample_rate)
        wf_span = win.waterfall._hi_freq - win.waterfall._lo_freq
        check("the waterfall follows the zoomed retune",
              abs(wf_span - span / 4.0) < 2000.0,
              f"waterfall span {wf_span/1e3:.1f} kHz")

        # Back to 1x, and confirm the picture widens again.
        win.cmb_zoom.setCurrentIndex(0)
        QTimer.singleShot(800, guard(phase_wide))

    def phase_wide():
        span = float(win.settings.sample_rate)
        xr = win.spectrum.viewRange()[0]
        check("zoom 1x restores the full-span view",
              abs((xr[1] - xr[0]) - span / 1e6) < span / 1e6 * 0.2,
              f"view {xr[0]:.4f}..{xr[1]:.4f} MHz")
        check("waterfall updates again after zooming back out",
              float(win.waterfall._wf.max()) > -100.0,
              f"max {win.waterfall._wf.max():.1f} dB")
        st = win.radio.status
        # Targeted check for the new feature: whatever the machine was doing
        # before, switching noise reduction on must not itself cost us RF.
        check_no_drops("no drops while noise reduction runs", st,
                       baseline=drops["before_nr"])
        check_no_drops("no drops after retuning", st)

        QTimer.singleShot(300, guard(phase_controls))

    def phase_controls():
        """Every remaining control, driven the way a user drives it.

        These are the paths where a widget can be wired to the wrong slot or
        not wired at all: calling the method behind a control (as the rest of
        this file does) proves the logic works, not that the control reaches
        it.  Each check clicks, drags, wheels or spins the real widget and
        looks at the radio and the window afterwards.
        """
        # ---- the Qt audio backend, which no other test touches ----------
        from rspsdr.audio import AUDIO_RATE, _QtBackend
        try:
            backend = _QtBackend(AUDIO_RATE)
        except Exception as exc:                      # noqa: BLE001
            print(f"  SKIP  the Qt audio backend :: no Qt audio device ({exc})")
        else:
            check("the Qt backend names the device it plays to",
                  "QAudioSink" in backend.describe(), backend.describe())
            tone = (8000 * np.sin(2 * np.pi * 1000 * np.arange(4800) / AUDIO_RATE)
                    ).astype("<i2").tobytes()
            backend.write(tone)
            check("the Qt backend hands over exactly what it was given",
                  backend.take(16) == tone[:16], f"{len(tone)} bytes written")
            check("its source device is sequential and read-only",
                  backend._io.isSequential() is True
                  and backend._io.writeData(b"x") == 0)
            check("its source device serves reads and reports what is waiting",
                  backend._io.readData(8) == tone[16:24]
                  and backend._io.bytesAvailable() >= 1,
                  f"{len(tone) - 24} bytes still queued")
            # ... and Qt itself pulls from it, which is the part that would be
            # missing if the sink were never started.
            t0 = time.monotonic()
            while time.monotonic() - t0 < 0.6 and len(backend._ring):
                QApplication.processEvents()
                time.sleep(0.01)
            check("Qt pulls the audio out of the ring",
                  len(backend._ring) < len(tone) - 24,
                  f"{len(backend._ring)} of {len(tone) - 24} bytes still queued "
                  f"after 0.6 s")
            backend.close()

        # ---- the wrapping layout's container protocol -------------------
        scratch = ui.FlowLayout()
        scratch.addWidget(QLabel("probe"))
        check("the wrapping row reports what it holds",
              scratch.count() == 1 and scratch.itemAt(0) is not None
              and scratch.itemAt(99) is None)
        item = scratch.takeAt(0)
        check("and can hand an item back",
              item is not None and scratch.count() == 0
              and scratch.takeAt(0) is None)
        del item

        # ---- start / stop from the button ------------------------------
        win.btn_start.click()
        check("the Start button stops the radio and says so",
              not win.radio.opened and "Start" in win.btn_start.text(),
              f"opened={win.radio.opened} label {win.btn_start.text()!r}")
        win.btn_start.click()
        check("and starts it again",
              win.radio.opened and win.radio.status.running
              and "Stop" in win.btn_start.text(),
              f"opened={win.radio.opened} label {win.btn_start.text()!r}")

        # ---- stepping the tuning ---------------------------------------
        win.spn_freq.setValue(10_000_000 / 1e6)
        win.cmb_step.setCurrentIndex(5)                 # 5 kHz
        check("the step size reaches the dial",
              win._step == 5000
              and abs(win.spn_freq.singleStep() - 0.005) < 1e-9,
              f"step {win._step} Hz, dial step {win.spn_freq.singleStep()}")
        win.btn_step_up.click()
        up = win.settings.tuned_freq
        check("the step-up button retunes by one step",
              abs(up - 10_005_000) < 1.0, f"{up/1e6:.6f} MHz")
        win.btn_step_down.click()
        check("and the step-down button comes back",
              abs(win.settings.tuned_freq - 10_000_000) < 1.0,
              f"{win.settings.tuned_freq/1e6:.6f} MHz")

        # A wheel over the *spectrum* steps the tuning (the waterfall's zooms).
        before = win.settings.tuned_freq
        app.sendEvent(win.spectrum.viewport(), QWheelEvent(
            QPointF(30.0, 30.0), QPointF(30.0, 30.0), QPoint(0, 0),
            QPoint(0, 120), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))
        app.processEvents()
        check("the wheel over the spectrum steps the tuning up",
              abs(win.settings.tuned_freq - (before + 5000)) < 1.0,
              f"{(win.settings.tuned_freq - before)/1e3:+.0f} kHz for a 5 kHz step")
        app.sendEvent(win.spectrum.viewport(), QWheelEvent(
            QPointF(30.0, 30.0), QPointF(30.0, 30.0), QPoint(0, 0),
            QPoint(0, -120), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))
        app.processEvents()
        check("and steps it back down",
              abs(win.settings.tuned_freq - before) < 1.0,
              f"{win.settings.tuned_freq/1e6:.6f} MHz vs {before/1e6:.6f} MHz")

        # ---- click and drag on the spectrum ----------------------------
        clicks = []
        win.spectrum.tuned.connect(lambda f: clicks.append(f))
        where = QPointF(300.0, win.spectrum.height() / 2)
        app.sendEvent(win.spectrum.viewport(), QMouseEvent(
            QEvent.Type.MouseButtonPress, where, Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        want = win.spectrum.plotItem.vb.mapSceneToView(where).x() * 1e6
        # Quantised to the nearest FFT bin, which is 488 Hz at 2 MSps/4096.
        check("clicking the spectrum tunes to the frequency under the cursor",
              bool(clicks) and abs(clicks[0] - want) <= 1000.0,
              f"x=300 px tuned {clicks[0]/1e6:.6f} MHz, axis says {want/1e6:.6f} MHz")
        dragging = win.spectrum._dragging
        drag_to = QPointF(420.0, win.spectrum.height() / 2)
        app.sendEvent(win.spectrum.viewport(), QMouseEvent(
            QEvent.Type.MouseMove, drag_to, Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        dragged = win.settings.tuned_freq
        app.sendEvent(win.spectrum.viewport(), QMouseEvent(
            QEvent.Type.MouseButtonRelease, drag_to, Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        check("dragging across the spectrum keeps tuning while held",
              dragging and len(clicks) >= 2 and dragged > clicks[0],
              f"{len(clicks)} tunes from press and drag")
        check("and releasing the button ends the drag",
              win.spectrum._dragging is False)

        # Right-click on the spectrum re-centres the receiver on that pixel.
        win.spn_freq.setValue(10_000_000 / 1e6)
        win.spn_center.setValue(9_600_000 / 1e6)
        right = QPointF(900.0, win.spectrum.height() / 2)
        app.sendEvent(win.spectrum.viewport(), QMouseEvent(
            QEvent.Type.MouseButtonPress, right, Qt.MouseButton.RightButton,
            Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        want_centre = win.spectrum.plotItem.vb.mapSceneToView(right).x()
        # Quantised to the nearest FFT bin, exactly like a left-click on the
        # spectrum: the axis value is continuous but the click snaps to the
        # bin it falls in, so the result can be up to half a bin away.
        bin_mhz = float(win.settings.sample_rate) / 4096 / 1e6
        error_mhz = abs(win.spn_center.value() - want_centre)
        check("right-clicking the spectrum re-centres the receiver there",
              error_mhz <= bin_mhz / 2 + 1e-6
              and abs(win.radio.settings.center_freq - want_centre * 1e6) <= 2.0
              * win.settings.sample_rate / 4096,
              f"centre asked {want_centre:.6f} MHz, radio at "
              f"{win.radio.settings.center_freq/1e6:.6f} MHz "
              f"({error_mhz*1e6:.0f} Hz from the pixel, one bin is "
              f"{bin_mhz*1e6:.0f} Hz)")

        # Dragging across the waterfall tunes continuously, as on the spectrum.
        win.spn_freq.setValue(10_000_000 / 1e6)
        wf_tunes = []
        win.waterfall.tuned.connect(lambda f: wf_tunes.append(f))
        p1 = QPointF(win.waterfall.width() * 0.30, win.waterfall.height() / 2)
        p2 = QPointF(win.waterfall.width() * 0.60, win.waterfall.height() / 2)
        app.sendEvent(win.waterfall.viewport(), QMouseEvent(
            QEvent.Type.MouseButtonPress, p1, Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        app.sendEvent(win.waterfall.viewport(), QMouseEvent(
            QEvent.Type.MouseMove, p2, Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        dragged_wf = win.settings.tuned_freq
        app.sendEvent(win.waterfall.viewport(), QMouseEvent(
            QEvent.Type.MouseButtonRelease, p2, Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        check("dragging across the waterfall tunes continuously",
              len(wf_tunes) >= 2 and dragged_wf >= wf_tunes[0],
              f"{len(wf_tunes)} tunes across the drag, "
              f"{wf_tunes[0]/1e6:.4f} -> {dragged_wf/1e6:.4f} MHz")
        check("and releasing there ends the drag too",
              win.waterfall._dragging is False)

        # ---- jumping to a preset, and the mode buttons ------------------
        idx = next(i for i in range(win.cmb_preset.count())
                   if getattr(win.cmb_preset.itemData(i), "name", None) == "40m FT8")
        win.cmb_preset.setCurrentIndex(idx)
        check("a preset jumps to its frequency, mode and width",
              abs(win.radio.settings.tuned_freq - 7_074_000) < 1.0
              and win.radio.demod.mode == "usb" and win._zoom == 1,
              f"{win.settings.tuned_freq/1e6:.6f} MHz {win.radio.demod.mode}")
        check("the preset list returns to its placeholder",
              win.cmb_preset.currentIndex() == 0)

        win.mode_buttons["cw"].click()
        check("clicking a mode button changes the demodulator and the filter",
              win.radio.demod.mode == "cw" and win.mode_buttons["cw"].isChecked()
              and abs(win.radio.demod.bw - 500.0) < 1.0,
              f"{win.radio.demod.mode} at {win.radio.demod.bw:.0f} Hz")
        check("and only one mode button stays down",
              sum(b.isChecked() for b in win.mode_buttons.values()) == 1)
        win.mode_buttons["am"].click()

        # ---- volume, mute, recentre ------------------------------------
        win.sld_vol.setValue(30)
        check("the volume slider reaches the audio output",
              win.radio.audio is not None
              and abs(win.radio.audio.volume - 0.30) < 1e-6,
              f"{win.radio.audio.volume:.2f}")
        win.chk_mute.setChecked(True)
        check("mute reaches the audio output", win.radio.audio.muted is True)
        win.chk_mute.setChecked(False)
        check("and unmuting does too", win.radio.audio.muted is False)
        win.spn_freq.setValue(10_000_000 / 1e6)
        win.spn_center.setValue(9_000_000 / 1e6)
        win.btn_recentre.click()
        check("the recentre button puts the receiver on the dial",
              abs(win.radio.settings.center_freq - 10_000_000) < 1.0
              and abs(win.spn_center.value() - 10.0) < 1e-6,
              f"centre {win.radio.settings.center_freq/1e6:.6f} MHz")

        # ---- saving a memory through the dialog ------------------------
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        saved_get = QInputDialog.getText
        saved_ask = QMessageBox.question
        QInputDialog.getText = staticmethod(lambda *a, **k: ("probe channel", True))
        try:
            win.btn_mem_save.click()
        finally:
            QInputDialog.getText = saved_get
        names = [c.name for c in win.memory]
        check("the Save button stores what the dialog was given",
              "probe channel" in names, ", ".join(names))
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Yes)
        try:
            win._mem_delete()
        finally:
            QMessageBox.question = saved_ask
        check("and deleting it puts the list back", len(win.memory) == 3,
              f"{len(win.memory)} memories left")

        # ---- the error path -------------------------------------------
        win.radio._error("probe failure")
        QApplication.processEvents()
        shown = win.lbl_status.text()
        check("a radio error is shown in the status line",
              "probe failure" in shown, repr(shown))
        win.radio.status.error = ""                 # leave the bar clean

        # ---- changing the FFT size while streaming ----------------------
        # The earlier FFT check set the box to the value it already held, so
        # the branch that swaps the spectrum object had never run.
        before_frames = win.frames_drawn
        win.cmb_fft.setCurrentIndex(3)                 # 8192
        check("a larger FFT is adopted and the DSP keeps its place in line",
              win.radio.spectrum.n_fft == 8192,
              f"spectrum is now {win.radio.spectrum.n_fft}-point")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.0:
            QApplication.processEvents()
            time.sleep(0.01)
        curve = win.spectrum.curve.getData()
        check("and frames keep arriving with the bigger transform",
              win.frames_drawn - before_frames >= 3
              and curve[0] is not None and len(curve[0]) > 100,
              f"{win.frames_drawn - before_frames} repaints, "
              f"{0 if curve[0] is None else len(curve[0])} drawn points")
        win.cmb_fft.setCurrentIndex(2)                 # back to 4096
        check("and back again", win.radio.spectrum.n_fft == 4096)

        # ---- the palettes that are drawn here, not loaded ----------------
        grey = ui.build_colormap("grey")
        hot = ui.build_colormap("hot")
        fallback = ui.build_colormap("definitely-not-a-palette")
        ramp = lambda cm, n: [tuple(int(v) for v in r)
                              for r in cm.getLookupTable(nPts=n)]
        check("the palettes with no data file are built in process",
              len(set(ramp(grey, 3))) == 3
              and sum(ramp(grey, 2)[0]) < sum(ramp(grey, 2)[-1])
              and len(set(ramp(hot, 4))) == 4,
              f"grey {ramp(grey, 3)}, hot starts {ramp(hot, 4)[0]}")
        check("an unknown palette falls back instead of breaking the waterfall",
              len(fallback.getLookupTable(nPts=3)) == 3,
              "fell back to the built-in spectrum map")
        chosen = None
        for key in ("grey", "hot", "spectrum"):
            chosen = key
            win.cmb_cmap.setCurrentIndex(
                next(i for i in range(win.cmb_cmap.count())
                     if win.cmb_cmap.itemData(i) == key))
            if win.waterfall._cmap_key != key:
                break
        check("every palette in the list reaches the waterfall",
              win.waterfall._cmap_key == chosen,
              f"went through grey, hot and spectrum; ended on "
              f"{win.waterfall._cmap_key}")

        # ---- the cursor readout ----------------------------------------
        app.sendEvent(win.spectrum.viewport(), QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(700.0, win.spectrum.height() / 2), Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        app.processEvents()
        check("moving the mouse over the spectrum reads out the frequency",
              "cursor" in win.lbl_cursor.text() and "MHz" in win.lbl_cursor.text(),
              repr(win.lbl_cursor.text()))

        # ---- the analogue signal meter ---------------------------------
        # It is fed from the receiver running in this window, and its face is
        # painted by hand: what it reads and where it draws the needle are two
        # different questions, and both are asked here.
        live = win.smeter.reading_text()
        check("the meter reads the live signal",
              live not in ("", "--") and win.lbl_smeter.text().startswith(live),
              f"the face says {live!r} ({win.smeter.level_dbfs:.1f} dBFS with S9 "
              f"at {win.smeter.ref_dbfs:g} dBFS)")
        check("and sits in the window at its own size",
              win.smeter.isVisible() and win.smeter.width() == ui.smeter.WIDTH
              and win.smeter.height() == ui.smeter.HEIGHT,
              f"{win.smeter.width()}x{win.smeter.height()} px")

        # Through the controls, not the methods behind them: the calibration,
        # the peak switch and the face all have to reach the widget.
        win.spn_sref.setValue(ui.smeter.DEFAULT_REF_DBFS)
        win.chk_speak.setChecked(True)
        win.cmb_sface.setCurrentIndex(win.cmb_sface.findData("dark"))
        check("the meter's own controls reach it",
              (win.smeter.ref_dbfs, win.smeter.peak_on, win.smeter.face)
              == (ui.smeter.DEFAULT_REF_DBFS, True, "dark"),
              f"S9 at {win.smeter.ref_dbfs:g} dBFS, peak {win.smeter.peak_on}, "
              f"face {win.smeter.face!r}")

        def level_for(dbm):
            """The dBFS that reads a given dBm on this calibration."""
            return dbm - ui.smeter.S9_DBM + win.smeter.ref_dbfs

        # A needle is a line drawn by hand, so "it points where the scale says"
        # is a question about pixels: the red ones have to be where the geometry
        # puts the tip, either side of the middle of the dial.
        for dbm, side in ((-53.0, "right"), (-100.0, "left")):
            win.smeter.reset()
            win.smeter.feed(level_for(dbm), 0.045)
            red = needle_mask(face_pixels(win.smeter))
            cols = np.nonzero(red.any(axis=0))[0]
            tip = win.smeter.needle()[2]
            end = int(cols.max()) if side == "right" else int(cols.min())
            check(f"the needle is drawn {side} of centre, where the scale puts it",
                  int(red.sum()) > 40 and cols.size > 8 and abs(end - tip) <= 4,
                  f"{win.smeter.reading_text()} ({dbm:+.0f} dBm): "
                  f"{int(red.sum())} red pixels over x {cols.min()}..{cols.max()}, "
                  f"the geometry says the tip is at {tip:.1f} "
                  f"of {win.smeter.width()}")

        # The peak marker is a second, shorter needle that stays where the level
        # was while the first one falls away.
        win.smeter.reset()
        win.smeter.feed(level_for(-53.0), 0.045)
        for _ in range(8):                        # 360 ms: inside the half second
            win.smeter.feed(level_for(-140.0), 0.045)
        face = face_pixels(win.smeter)
        red = np.nonzero(needle_mask(face).any(axis=0))[0]
        yellow = np.nonzero(peak_mask(face).any(axis=0))[0]
        check("the peak marker holds the level the needle has left behind",
              yellow.size > 10 and red.size > 10 and yellow.any()
              and abs(int(yellow.max()) - win.smeter.peak_needle()[2]) <= 4
              and red.max() < yellow.max() - 20,
              f"peak {win.smeter.peak_dbm:+.0f} dBm at x {yellow.max() if yellow.size else '-'} "
              f"(geometry {win.smeter.peak_needle()[2]:.1f}), needle "
              f"{win.smeter.dbm:+.0f} dBm at x {red.max() if red.size else '-'}")

        # Both faces are painted from a table, so switching has to change the
        # picture -- and half of the table is which way round it is.
        win.smeter.reset()
        dark = face_pixels(win.smeter)
        win.cmb_sface.setCurrentIndex(win.cmb_sface.findData("cream"))
        cream = face_pixels(win.smeter)
        win.cmb_sface.setCurrentIndex(win.cmb_sface.findData("dark"))
        check("the cream face is a light dial and the dark one a dark one",
              dark[:, :, :3].mean() < 60 and cream[:, :, :3].mean() > 150,
              f"the dark face averages {dark[:, :, :3].mean():.0f} of 255, "
              f"the cream one {cream[:, :, :3].mean():.0f}")
        check("... and the needle is drawn on both of them",
              needle_mask(dark).sum() > 40 and needle_mask(cream).sum() > 40,
              f"{needle_mask(dark).sum()} red pixels on the dark face, "
              f"{needle_mask(cream).sum()} on the cream one")

        # With the receiver stopped the needle has nowhere to stand: it falls
        # back to its rest and the face stops claiming a reading, rather than
        # freezing at the last thing that was heard.
        def pump(seconds):
            t0 = time.monotonic()
            while time.monotonic() - t0 < seconds:
                QApplication.processEvents()
                time.sleep(0.02)

        win.btn_start.click()
        pump(1.2)
        red = needle_mask(face_pixels(win.smeter))
        rest_tip = win.smeter.needle()[2]
        check("stopping the receiver lets the needle fall back to rest",
              win.smeter.reading_text() == "--"
              and "no signal reading" in win.lbl_smeter.text()
              and red.any() and abs(int(np.nonzero(red.any(axis=0))[0].min())
                                    - rest_tip) <= 4,
              f"{int(red.sum())} red pixels with the tip at "
              f"{np.nonzero(red.any(axis=0))[0].min() if red.any() else '-'} "
              f"(rest is {rest_tip:.1f} of {win.smeter.width()}), the face says "
              f"{win.lbl_smeter.text()!r}")
        win.btn_start.click()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 4.0 and not win.radio.opened:
            QApplication.processEvents()
            time.sleep(0.05)
        check("and starting it again brings the radio and the meter back",
              win.radio.opened, f"opened={win.radio.opened}")

        # Leave the state as the rest of the test expects it.
        win.spn_freq.setValue(10_000_000 / 1e6)
        win.cmb_zoom.setCurrentIndex(0)
        QTimer.singleShot(600, guard(finish_and_shoot))

    def finish_and_shoot():
        os.makedirs(os.path.dirname(args.shot) or ".", exist_ok=True)
        pix = win.grab()
        pix.save(args.shot)
        check("window rendered to PNG",
              os.path.exists(args.shot) and os.path.getsize(args.shot) > 5000,
              f"{args.shot} {os.path.getsize(args.shot)//1024} KiB, "
              f"{pix.width()}x{pix.height()}")

        win.radio.close()
        app.quit()

    def phase_low_rate():
        st = win.radio.status
        check("changing the sample rate leaves the radio streaming",
              win.radio.opened and st.running and abs(st.sample_rate - 62500) < 1,
              f"running={st.running} opened={win.radio.opened} "
              f"requested {st.sample_rate/1e3:g} kSps, "
              f"actual {st.actual_rate/1e3:g} kSps")
        check("the Start button still offers Stop after a rate change",
              "Stop" in win.btn_start.text(), repr(win.btn_start.text()))
        QTimer.singleShot(2500, guard(phase_low_rate_check))

    def phase_low_rate_check():
        drawn = win.frames_drawn - drops["low_rate_frames"]
        curve = win.spectrum.curve.getData()
        points = 0 if curve[0] is None else len(curve[0])
        check("the spectrum is alive at the lowest sample rate",
              drawn >= 5 and points > 100 and curve[1] is not None
              and bool(np.all(np.isfinite(curve[1]))),
              f"{drawn} repaints and {points} drawn points "
              f"in the 2.5 s the FFT needs there")
        check("the waterfall is alive at the lowest sample rate",
              float(win.waterfall._wf.max()) > -140.0,
              f"max {float(win.waterfall._wf.max()):.1f} dB")
        # Baseline 0, not the earlier count: changing the sample rate closes
        # and reopens the device, and the fresh Status starts its own count.
        check_no_drops("no drops while at the lowest rate", win.radio.status)

        wide = next(i for i in range(win.cmb_rate.count())
                    if win.cmb_rate.itemData(i) == 2000000)
        win.cmb_rate.setCurrentIndex(wide)
        QTimer.singleShot(1500, guard(phase_finish))

    QTimer.singleShot(int(args.seconds * 1000), guard(phase_measure))
    try:
        app.exec()
    finally:
        shutil.rmtree(MEM_TMP, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("all GUI checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
