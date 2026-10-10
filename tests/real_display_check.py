"""Real-display checks: the window manager, and the installed copy.

    python3 tests/real_display_check.py [--shot-dir artifacts]

The offscreen suite cannot test two things.  It has no window manager, so the
maximise check there is skipped; and it runs the *source tree*, whereas what a
user actually launches is a copy under ``~/.local/share`` that has to be
reinstalled by hand after every change -- a stale copy is exactly what once made
a fixed bug look unfixed.  This runs on the real display, against the installed
copy when there is one, which is the interface the user has.

Exits 0 with a SKIP when there is no display to open a window on.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLED = os.path.expanduser("~/.local/share/rspsdr")

if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    print("SKIPPED: no DISPLAY or WAYLAND_DISPLAY -- nothing to open a window on")
    sys.exit(0)

# Prefer the installed copy: that is what the launcher and the menu entry run.
MODULE_ROOT = INSTALLED if os.path.isdir(os.path.join(INSTALLED, "rspsdr")) \
    else ROOT
sys.path.insert(0, MODULE_ROOT)

from PyQt6.QtCore import Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from rspsdr import ui  # noqa: E402
from rspsdr.radio import Settings, list_devices  # noqa: E402
from rspsdr.ui import MainWindow  # noqa: E402

if not list_devices():
    print("SKIPPED: no SDRplay device found")
    sys.exit(0)

FAILURES: list[str] = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" :: {detail}" if detail else ""),
          flush=True)
    if not ok:
        FAILURES.append(name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot-dir", default=os.path.join(ROOT, "artifacts"))
    args = ap.parse_args()
    os.makedirs(args.shot_dir, exist_ok=True)

    app = QApplication(sys.argv[:1])
    win = MainWindow(Settings(center_freq=10_000_000.0, tuned_freq=10_000_000.0,
                              mode="am", filter_bw=6000.0, if_gain=50.0,
                              audio_backend="pipe"),
                     memory_path=os.path.join(args.shot_dir, ".real-check-mem.json"))
    win.show()
    QTimer.singleShot(0, win.autostart)

    print("== real display ==")
    print(f"  module under test : {os.path.dirname(ui.__file__)}")
    print(f"  platform          : {QApplication.platformName()}")

    state: dict = {}

    def phase_maximise():
        # The state request a title-bar click actually sends.  showMaximized()
        # sets the same flag but skips asking the window manager, so it passes
        # even where a real maximise would not.
        win.windowHandle().setWindowStates(Qt.WindowState.WindowMaximized)
        QApplication.processEvents()
        QTimer.singleShot(1200, phase_maximise_report)

    def phase_maximise_report():
        scr = QApplication.primaryScreen().availableGeometry()
        frame = win.frameGeometry()
        check("the window manager honours a maximise request",
              win.isMaximized(),
              "" if win.isMaximized() else "isMaximized() is False")
        check("the maximised window fills the available screen",
              frame.width() >= scr.width() - 4 and frame.height() >= scr.height() - 60,
              f"client {win.width()}x{win.height()}, "
              f"frame {frame.width()}x{frame.height()} "
              f"at +{frame.x()}+{frame.y()}, screen {scr.width()}x{scr.height()}")
        check("the maximised frame is still on the screen",
              frame.x() >= -1 and frame.y() >= -1
              and frame.right() <= scr.right() + 1,
              f"+{frame.x()}+{frame.y()} {frame.width()}x{frame.height()}")
        pix = win.grab()
        path = os.path.join(args.shot_dir, "installed_maximised.png")
        pix.save(path)
        check("the maximised window rendered to a PNG",
              os.path.getsize(path) > 5000 if os.path.exists(path) else False,
              f"{path} {pix.width()}x{pix.height()}")
        QTimer.singleShot(500, phase_low_rate)

    def phase_low_rate():
        # The rate where a hop holds fewer samples than one FFT: both displays
        # used to stay blank there, and the Start button ended up wrong.
        state["frames"] = win.frames_drawn
        low = next(i for i in range(win.cmb_rate.count())
                   if win.cmb_rate.itemData(i) == 62500)
        win.cmb_rate.setCurrentIndex(low)
        QTimer.singleShot(5000, phase_low_rate_report)

    def phase_low_rate_report():
        st = win.radio.status
        drawn = win.frames_drawn - state["frames"]
        curve = win.spectrum.curve.getData()
        points = 0 if curve[0] is None else len(curve[0])
        check("the radio is still streaming at 62.5 kSps",
              win.radio.opened and st.running,
              f"requested {st.sample_rate/1e3:g} kSps, "
              f"actual {st.actual_rate/1e3:g} kSps")
        check("the Start button still shows Stop",
              "Stop" in win.btn_start.text(), repr(win.btn_start.text()))
        check("the spectrum keeps repainting at 62.5 kSps",
              drawn >= 5 and points > 100,
              f"{drawn} repaints, {points} drawn points")
        check("the waterfall holds real signal at 62.5 kSps",
              float(win.waterfall._wf.max()) > -140.0,
              f"max {float(win.waterfall._wf.max()):.1f} dB")
        pix = win.grab()
        path = os.path.join(args.shot_dir, "installed_lowrate.png")
        pix.save(path)
        check("the low-rate window rendered to a PNG", os.path.exists(path),
              f"{path} {pix.width()}x{pix.height()}")
        QTimer.singleShot(200, finish)

    def finish():
        win.radio.close()
        app.quit()

    def guard(phase):
        def run():
            try:
                phase()
            except Exception as exc:                  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(f"{phase.__name__} ran without raising", False, repr(exc))
                try:
                    win.radio.close()
                except Exception:
                    pass
                app.quit()
        return run

    QTimer.singleShot(4000, guard(phase_maximise))
    app.exec()

    try:
        os.unlink(os.path.join(args.shot_dir, ".real-check-mem.json"))
    except OSError:
        pass

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("all real-display checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
