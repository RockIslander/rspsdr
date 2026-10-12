"""Tests for the settings the window remembers between runs.

    python3 tests/test_prefs.py        # no radio, no display, no sound card

The claim is that a listener sets the receiver up once.  So the checks here are
about a *second run*: whether every control comes back to where it was left,
whether a hand-edited or half-written file costs one setting or the program,
whether the command line still overrides the file where it was typed, and
whether the three settings FT8 borrows while it listens are put back rather than
quietly written down as the new normal.

The last section opens the real window -- offscreen, with the device left
unopened -- changes the controls as a user would, closes it, and opens it again.
That is the only way to answer "does the user have to set this up again": it is
a question about widgets, and only a widget can answer it.

Run with:  python3 tests/test_prefs.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import fields

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Before anything reads the config directory: the whole file works under a
# temporary one, so a run of this test cannot touch the channels or settings of
# the person running it.
TMP = tempfile.mkdtemp(prefix="rspsdr-prefs-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "config")

import rspsdr.ui as ui_mod  # noqa: E402
from rspsdr import __main__ as main_mod  # noqa: E402
from rspsdr import prefs  # noqa: E402
from rspsdr.radio import Settings  # noqa: E402

failures: list[str] = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + (f" :: {detail}" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


def tmp_path(name: str) -> str:
    return os.path.join(TMP, name)


print("== reading one remembered value ==")
# Everything in the file is read back through one table, and anything that
# cannot be used is *ignored*: a damaged file costs a setting, never the window.
for name, kind, raw, spec, want in (
        ("a number in range", "number", 0.4, (0.0, 2.0), 0.4),
        ("an integer in range", "int", 1_536_000, (50_000, 20_000_000), 1_536_000),
        ("an integer written as 4096.0", "int", 4096.0, (256, 1_048_576), 4096),
        ("true", "flag", True, (), True),
        ("a power-of-two transform", "fft", 8192, (256, 1_048_576), 8192),
        ("a mode the receiver knows", "mode", "usb", (), "usb"),
        ("... and the one it does not have, received as USB",
         "mode", "ft8", (), "ft8"),
        ("a choice, whatever its case", "choice", "QT", ("auto", "pipe", "qt"),
         "qt"),
        ("a string", "text", "driver=stub", (120,), "driver=stub"),
        ("a string with space around it", "text", "  pipe  ", (120,), "pipe")):
    got = prefs.read_value(kind, raw, spec)
    check(f"{name} is accepted", got == want, repr(got))

for name, args in (
        ("a number out of range", ("number", 9.0, (0.0, 2.0))),
        ("a number that is not one", ("number", "0.4", (0.0, 2.0))),
        ("a NaN", ("number", float("nan"), (0.0, 2.0))),
        ("an infinity", ("number", float("inf"), (0.0, 2.0))),
        ("a boolean where an integer belongs", ("int", True, (0, 20))),
        ("an integer out of range", ("int", 40_000_000, (50_000, 20_000_000))),
        ("a string where a flag belongs", ("flag", "yes", ())),
        ("a mode nothing knows", ("mode", "bogus", ())),
        ("a mode that would raise in the receiver", ("mode", "ctcss", ())),
        ("something that is not a choice", ("choice", "alsa", ("auto", "pipe", "qt"))),
        ("a blank string", ("text", "   ", (120,))),
        ("a string longer than the limit", ("text", "x" * 121, (120,))),
        ("a transform that is not a power of two", ("fft", 5000, (256, 1_048_576))),
        ("a transform below the range", ("fft", 64, (256, 1_048_576))),
        ("missing entirely", ("number", None, (0.0, 2.0)))):
    check(f"{name} is refused", prefs.read_value(args[0], args[1], args[2]) is None)


print("\n== the file ==")
path = tmp_path("settings.json")
store = prefs.PrefsStore(path)
check("a missing file yields an empty store", store.data["radio"] == {}
      and store.value("radio", "volume", "number", (0.0, 2.0), 0.6) == 0.6)

# The whole of Settings, remembered and read back.  Distinctive values, so a
# field that is silently dropped or crossed with another one is visible.
spicy = Settings(
    device_args="driver=stub,serial=0001", sample_rate=1_536_000,
    center_freq=14_080_000.0, tuned_freq=14_074_123.0, mode="cw",
    filter_bw=500.0, cw_pitch=620.0, rf_agc=True, if_gain=33.0, rf_gain=2,
    tuner_bw=1_536_000, audio_agc=False, nr=True, nr_level=70.0, volume=0.35,
    squelch_db=-90.0, n_fft=8192, spec_avg=0.25, audio_backend="qt")
prefs.remember_settings(store, spicy)
store.save()
check("the file names its format", json.load(open(path))["version"] == prefs.FORMAT_VERSION)

back = prefs.settings_from(prefs.PrefsStore(path))
came_back = [f.name for f in fields(Settings)
             if getattr(back, f.name) != getattr(spicy, f.name)]
check("every receiver setting survives a restart", not came_back,
      ", ".join(came_back) or f"all {len(fields(Settings))} fields")
check("the table covers every field of Settings, so a new one cannot be forgotten",
      set(prefs.RADIO_FIELDS) == {f.name for f in fields(Settings)},
      ", ".join(sorted(set(prefs.RADIO_FIELDS) ^ {f.name for f in fields(Settings)})))

# What is not a remembered setting is not written either.
raw = json.load(open(path))
check("nothing outside the three sections is stored",
      set(raw) == {"version", "radio", "ui", "window"}, ", ".join(sorted(raw)))
check("only receiver fields are written under radio",
      set(raw["radio"]) == set(prefs.RADIO_FIELDS))

# A newer version's keys are kept rather than dropped: a downgrade must not
# lose settings it does not understand.
both = prefs.PrefsStore(path)
both.load()
both.put("radio", "future_thing", 7)
both.put("radio", "volume", 0.5)
both.save()
check("keys a future version wrote survive a load and save",
      json.load(open(path))["radio"].get("future_thing") == 7)
check("... while the values it does understand are the ones in use",
      prefs.settings_from(prefs.PrefsStore(path)).volume == 0.5)

damaged = tmp_path("damaged.json")
for name, text in (("not JSON at all", "{ this is not json"),
                   ("a list where an object belongs", '["volume"]'),
                   ("a number where an object belongs", "4"),
                   ("an empty file", "")):
    with open(damaged, "w") as fh:
        fh.write(text)
    try:
        bad = prefs.PrefsStore(damaged)
        ok = (bad.value("radio", "volume", "number", (0.0, 2.0), 0.6) == 0.6
              and bad.data.get("radio") == {})
        detail = str(bad.data)
    except Exception as exc:               # noqa: BLE001 - any raise is a failure
        ok, detail = False, repr(exc)
    check(f"a file that is {name} reads as “nothing remembered”", ok, detail)

# One value that cannot be used costs that value, not the file.
with open(damaged, "w") as fh:
    json.dump({"version": 1, "radio": {"volume": 99.0, "nr": "yes", "n_fft": 5000,
                                       "mode": "usb", "if_gain": 31.0}}, fh)
salvaged = prefs.settings_from(prefs.PrefsStore(damaged))
check("an unusable value falls back to the default, the rest is kept",
      salvaged.volume == Settings().volume and salvaged.nr is False
      and salvaged.n_fft == Settings().n_fft and salvaged.mode == "usb"
      and salvaged.if_gain == 31.0)

check("no half-written temporary file is left behind",
      not [f for f in os.listdir(TMP) if f.startswith(".settings-")],
      ", ".join(os.listdir(TMP)))


print("\n== the command line over the file ==")
cli_path = tmp_path("cli.json")
remembered = Settings(sample_rate=250_000, center_freq=3_500_000.0,
                      tuned_freq=3_500_000.0, mode="cw", filter_bw=500.0,
                      rf_agc=True, nr=True, volume=0.2)
cli_store = prefs.PrefsStore(cli_path)
prefs.remember_settings(cli_store, remembered)
cli_store.save()


def settings_for(argv, store=cli_store):
    args = main_mod.build_parser().parse_args(argv)
    return main_mod._make_settings(args, store, main_mod._typed_options(argv))


plain = settings_for([])
check("with no arguments the window opens on what was remembered",
      (plain.sample_rate, plain.tuned_freq, plain.mode, plain.filter_bw,
       plain.rf_agc, plain.nr, plain.volume)
      == (250_000, 3_500_000.0, "cw", 500.0, True, True, 0.2),
      f"{plain.sample_rate/1e6:g} MSps {plain.tuned_freq/1e6:.3f} MHz "
      f"{plain.mode} {plain.filter_bw:g} Hz")

typed = settings_for(["--freq", "14074000", "--mode", "am"])
check("an argument that was typed wins over the remembered one",
      typed.tuned_freq == 14_074_000.0 and typed.center_freq == 14_074_000.0
      and typed.mode == "am", f"{typed.tuned_freq/1e6:.3f} MHz {typed.mode}")
check("... and everything that was not typed is still remembered",
      (typed.sample_rate, typed.volume, typed.rf_agc, typed.nr)
      == (250_000, 0.2, True, True))
check("... and a mode brings its own filter with it", typed.filter_bw == 6000.0,
      f"{typed.filter_bw:g} Hz")
check("--bw was typed, so the remembered filter is the one that stands",
      settings_for(["--bw", "1800"]).filter_bw == 1800.0)

# The store_true flags are the awkward case: not typing one looks exactly like
# saying no to it, so the remembered yes must not be undone by silence.
quiet_flag = settings_for([])
check("a flag that was not typed leaves the remembered value alone",
      quiet_flag.rf_agc is True and quiet_flag.nr is True)
off_store = prefs.PrefsStore(tmp_path("off.json"))
prefs.remember_settings(off_store, Settings(rf_agc=False))
off_store.save()
check("... and one that was typed wins", settings_for(["--agc"], off_store).rf_agc is True)

headless = main_mod._make_settings(
    main_mod.build_parser().parse_args(["--selftest", "--freq", "9805000"]))
check("a headless run ignores the file and uses the defaults",
      (headless.sample_rate, headless.tuned_freq, headless.mode, headless.volume)
      == (2_000_000, 9_805_000.0, "lsb", Settings().volume),
      f"{headless.sample_rate/1e6:g} MSps {headless.tuned_freq/1e6:.3f} MHz")

out = subprocess.run([sys.executable, "-m", "rspsdr", "--check"], cwd=ROOT,
                     capture_output=True, text=True, env=os.environ.copy())
line = [ln for ln in out.stdout.splitlines() if "remembered settings" in ln]
check("--check says where the settings live and how many are remembered",
      bool(line) and prefs.default_path() in line[0],
      line[0] if line else out.stdout[-200:])


print("\n== --reset-settings ==")
dpath = prefs.default_path()
dirty = prefs.PrefsStore(dpath)
prefs.remember_settings(dirty, Settings(volume=0.11, tuned_freq=3_600_000.0))
dirty.put("ui", "zoom", 8)
dirty.save()
check("there is something to reset",
      prefs.PrefsStore(dpath).value("radio", "volume", "number", (0.0, 2.0),
                                    None) == 0.11)

reset_store = main_mod.open_prefs(
    main_mod.build_parser().parse_args(["--reset-settings"]))
after = prefs.PrefsStore(dpath)
check("--reset-settings empties the file before the window opens",
      after.data.get("radio") == {} and after.data.get("ui") == {},
      str(after.data))
check("... and the window that follows opens on the built-in defaults",
      main_mod._make_settings(main_mod.build_parser().parse_args([]),
                              reset_store, set()).tuned_freq == Settings().tuned_freq)
check("--screenshot remembers nothing at all, so a scripted run cannot "
      "reconfigure the user",
      main_mod.open_prefs(main_mod.build_parser().parse_args(
          ["--screenshot", "x.png"])) is None)


print("\n== the window ==")
from PyQt6.QtWidgets import QApplication  # noqa: E402

# The device never has to exist: the window is built with the radio unopened,
# and nothing below starts it.
ui_mod.list_devices = lambda: [{"index": 0, "label": "stub RSP1",
                                "args": "driver=stub"}]
app = QApplication.instance() or QApplication([])
mem_path = tmp_path("memories.json")


def window(settings=None, store=None):
    return ui_mod.MainWindow(settings or Settings(), memory_path=mem_path,
                             prefs_store=store if store is not None
                             else prefs.PrefsStore())


# --- one session, the way a listener would set it up -------------------
first = prefs.PrefsStore()
win = window()
check("a fresh profile opens on the defaults",
      win.settings.tuned_freq == Settings().tuned_freq
      and not win.chk_nr.isChecked() and win.cmb_zoom.currentData() == 1)

win.cmb_rate.setCurrentIndex(win.cmb_rate.findData(1_536_000))
win.cmb_step.setCurrentIndex(1)                       # 10 Hz
win.cmb_zoom.setCurrentIndex(2)                       # 4x
win.spn_freq.setValue(14.080)
win.mode_buttons["usb"].click()
win.cmb_bw.setCurrentIndex(win.cmb_bw.findData(1800.0))
win.spn_pitch.setValue(620.0)
win.sld_vol.setValue(35)
win.chk_aagc.setChecked(True)
win.chk_nr.setChecked(True)
win.sld_nr.setValue(70)
win.spn_sq.setValue(-90.0)
win.chk_agc.setChecked(True)
win.sld_if.setValue(33)
win.cmb_rf.setCurrentIndex(win.cmb_rf.findData(2))
win.cmb_tbw.setCurrentIndex(win.cmb_tbw.findData(1_536_000))
win.cmb_fft.setCurrentIndex(win.cmb_fft.findData(8192))
win.chk_wfauto.setChecked(False)
win.spn_wflo.setValue(-115.0)
win.spn_wfhi.setValue(-45.0)
win.spn_contrast.setValue(52.0)
win.cmb_cmap.setCurrentIndex(1)
win.chk_cmrev.setChecked(True)
win.cmb_wfdetail.setCurrentIndex(1)
win.chk_ft8every.setChecked(False)
# The signal meter's own setup: the level that reads S9 (calibrated here against
# a station of known strength), the peak marker, and the cream face of the older
# radios rather than the dark one.
win.spn_sref.setValue(-60.5)
win.chk_speak.setChecked(False)
win.cmb_sface.setCurrentIndex(win.cmb_sface.findData("cream"))
check("the meter takes the calibration, the peak switch and the face",
      (win.smeter.ref_dbfs, win.smeter.peak_on, win.smeter.face)
      == (-60.5, False, "cream"),
      f"S9 at {win.smeter.ref_dbfs} dBFS, peak {win.smeter.peak_on}, "
      f"face {win.smeter.face}")
# A size that fits the offscreen screen (800x800) and is above the window's
# own minimum (780x560), so nothing but the remembered value is what is being
# checked here.  _size_to_screen() and the screen clamp each have their own
# check below.
win.split.setSizes([180, 460])     # the divider, dragged
win.resize(790, 620)
win.move(60, 40)
watch = win.cmb_wfdetail.currentData()

# ... and then selects FT8, which does not want any of the listening aids.
win.cmb_ft8band.setCurrentIndex(win.cmb_ft8band.findData("20m"))
check("choosing a band tunes to its dial", win.settings.tuned_freq == 14_074_000.0
      and abs(win.spn_freq.value() - 14.074) < 1e-9)
win.spn_freq.setValue(14.080)              # a hand-tuned frequency on that band
win.mode_buttons["ft8"].click()
check("FT8 turns off what would subtract from the signal",
      not win.chk_nr.isChecked() and not win.chk_aagc.isChecked()
      and win.spn_sq.value() == ui_mod.FT8_SQUELCH_DB,
      f"NR {win.chk_nr.isChecked()}, AGC {win.chk_aagc.isChecked()}, "
      f"squelch {win.spn_sq.value():g} dB")
check("... and the strip and the decoder come up with it",
      not win.ft8_panel.isHidden() and win.spotter.armed)

# The frame's corner and the client's size: what move() and resize() take.
rect = win.geometry()
frame = win.frameGeometry()
split = list(win.split.sizes())
win.close()
check("closing writes the file", os.path.exists(dpath))

saved = json.load(open(dpath))
radio = saved["radio"]
check("the mode comes back as FT8, not as the USB it is heard in",
      radio["mode"] == "ft8", radio["mode"])
check("closing while in FT8 writes down the *listening* settings it borrowed",
      radio["nr"] is True and radio["audio_agc"] is True
      and radio["squelch_db"] == -90.0,
      f"nr {radio['nr']}, agc {radio['audio_agc']}, squelch {radio['squelch_db']}")
check("the tuning and the receiver settings are all in the file",
      (radio["sample_rate"], radio["tuned_freq"], radio["center_freq"],
       radio["if_gain"], radio["rf_gain"], radio["tuner_bw"], radio["n_fft"],
       radio["cw_pitch"], radio["volume"], radio["nr_level"], radio["filter_bw"],
       radio["rf_agc"])
      == (1_536_000, 14_080_000.0, 14_080_000.0, 33.0, 2, 1_536_000, 8192,
          620.0, 0.35, 70.0, ui_mod.ft8.FILTER_BW, True),
      json.dumps({k: radio[k] for k in ("if_gain", "rf_gain", "tuner_bw",
                                        "n_fft", "filter_bw")}))
ui = saved["ui"]
check("the window's own controls are in the file too",
      (ui["step"], ui["zoom"], ui["wf_auto"], ui["wf_floor"], ui["wf_ceiling"],
       ui["wf_contrast"], ui["wf_cmap"], ui["wf_reverse"], ui["wf_detail"],
       ui["ft8_band"], ui["ft8_every"], ui["s_meter_ref"],
       ui["s_meter_peak"], ui["s_meter_face"],
       ui["split_spectrum"], ui["split_waterfall"])
      == (10, 4, False, -115.0, -45.0, 52.0, "viridis", True, watch, "20m",
          False, -60.5, False, "cream", split[0], split[1]),
      json.dumps(ui))
check("the window's own size and place are written down",
      (saved["window"]["x"], saved["window"]["y"], saved["window"]["w"],
       saved["window"]["h"])
      == (frame.x(), frame.y(), rect.width(), rect.height())
      and saved["window"]["maximised"] is False, json.dumps(saved["window"]))

# --- the next start ----------------------------------------------------
second = prefs.PrefsStore()
args = main_mod.build_parser().parse_args([])
reopened = window(main_mod._make_settings(args, second, main_mod._typed_options([])),
                  store=second)

check("the receiver comes back on the same frequency, mode and filter",
      (reopened.settings.sample_rate, reopened.settings.tuned_freq,
       reopened.settings.mode, reopened.settings.filter_bw)
      == (1_536_000, 14_080_000.0, "ft8", ui_mod.ft8.FILTER_BW),
      f"{reopened.settings.tuned_freq/1e6:.3f} MHz {reopened.settings.mode}")
check("the mode button is lit and the strip is up",
      reopened.mode_buttons["ft8"].isChecked()
      and not reopened.ft8_panel.isHidden() and reopened.spotter.armed)
check("the band label comes back without overriding the frequency",
      reopened.cmb_ft8band.currentData() == "20m"
      and abs(reopened.spn_freq.value() - 14.080) < 1e-9,
      f"{reopened.cmb_ft8band.currentData()} at {reopened.spn_freq.value():.6f} MHz")

restored = [
    ("the sample rate", reopened.cmb_rate.currentData(), 1_536_000),
    ("the tuning step", reopened.cmb_step.currentData(), 10),
    ("the zoom", reopened.cmb_zoom.currentData(), 4),
    ("the filter", reopened.cmb_bw.currentData(), ui_mod.ft8.FILTER_BW),
    ("the CW pitch", reopened.spn_pitch.value(), 620.0),
    ("the volume", reopened.sld_vol.value(), 35),
    ("the NR strength", reopened.sld_nr.value(), 70),
    ("RF AGC", reopened.chk_agc.isChecked(), True),
    ("the IF gain", reopened.sld_if.value(), 33),
    ("the RF gain", reopened.cmb_rf.currentData(), 2),
    ("the tuner bandwidth", reopened.cmb_tbw.currentData(), 1_536_000),
    ("the FFT size", reopened.cmb_fft.currentData(), 8192),
    ("the waterfall's level tracking", reopened.chk_wfauto.isChecked(), False),
    ("the waterfall floor", reopened.spn_wflo.value(), -115.0),
    ("the waterfall ceiling", reopened.spn_wfhi.value(), -45.0),
    ("the waterfall contrast", reopened.spn_contrast.value(), 52.0),
    ("the colour map", reopened.cmb_cmap.currentData(), "viridis"),
    ("the reversed colour map", reopened.chk_cmrev.isChecked(), True),
    ("the waterfall detail", reopened.cmb_wfdetail.currentData(), watch),
    ("“decode every slot”", reopened.chk_ft8every.isChecked(), False),
    ("the meter's S9 calibration", reopened.spn_sref.value(), -60.5),
    ("the meter's peak marker", reopened.chk_speak.isChecked(), False),
    ("the meter's face", reopened.cmb_sface.currentData(), "cream"),
]
wrong = [f"{name} ({got!r} not {want!r})" for name, got, want in restored
         if got != want]
check(f"all {len(restored)} controls come back as they were left", not wrong,
      "; ".join(wrong))

check("... and the meter itself is set up that way again, not just the boxes",
      (reopened.smeter.ref_dbfs, reopened.smeter.peak_on, reopened.smeter.face)
      == (-60.5, False, "cream"),
      f"S9 at {reopened.smeter.ref_dbfs} dBFS, peak {reopened.smeter.peak_on}, "
      f"face {reopened.smeter.face}")
# The calibration is the whole of the meter's absolute accuracy, so the check
# is the one that matters: the level it was calibrated at must come back as S9.
reopened.smeter.feed(-60.5, 0.045)
check("a level fed at the calibration reads S9, not S6.4",
      abs(reopened.smeter.level_dbm() + 73.0) < 1e-9
      and reopened.smeter.reading_text() == "S9 +0 dB",
      f"-60.5 dBFS reads {reopened.smeter.level_dbm():.1f} dBm "
      f"({reopened.smeter.reading_text()}); at the default calibration it "
      f"would read {ui_mod.smeter.reading(ui_mod.smeter.dbm_for_dbfs(-60.5))}")

check("the window comes back the same size and in the same place",
      abs(reopened.width() - rect.width()) <= 2
      and abs(reopened.height() - rect.height()) <= 2
      and (reopened.frameGeometry().x(), reopened.frameGeometry().y())
      == (frame.x(), frame.y()),
      f"{reopened.width()}x{reopened.height()} at "
      f"{reopened.frameGeometry().x()},{reopened.frameGeometry().y()} "
      f"(was {rect.width()}x{rect.height()} at {frame.x()},{frame.y()})")
saved_split = [ui["split_spectrum"], ui["split_waterfall"]]
check("the divider between the plots comes back where it was",
      list(reopened.split.sizes()) == saved_split
      and abs(saved_split[0] / sum(saved_split) - 180 / 640) < 0.02,
      f"the file says {saved_split}, the window has "
      f"{list(reopened.split.sizes())}, the default is [300, 340]")

# The borrowed settings belong to the mode, not to the file: leaving FT8 gives
# them back, unless the user has taken one over in the meantime.
reopened.chk_nr.setChecked(True)           # "actually, I want NR here too"
reopened.mode_buttons["am"].click()
check("leaving FT8 puts the listening settings back",
      reopened.chk_agc.isChecked() and reopened.chk_aagc.isChecked()
      and reopened.spn_sq.value() == -90.0 and reopened.settings.nr is True,
      f"agc {reopened.chk_aagc.isChecked()}, squelch {reopened.spn_sq.value():g} dB")
check("... but a setting the user changed while in the mode stays theirs",
      reopened.chk_nr.isChecked())
reopened.close()

final = json.load(open(dpath))["radio"]
check("and the file follows the mode back out of FT8",
      final["mode"] == "am" and final["nr"] is True
      and final["audio_agc"] is True and final["squelch_db"] == -90.0,
      json.dumps({k: final[k] for k in ("mode", "nr", "audio_agc", "squelch_db")}))

# A window restored off the edge of every screen is worse than one that opens
# in the middle: monitors are unplugged and resolutions change.
wild = tmp_path("wild.json")
w = prefs.PrefsStore(wild)
w.put("window", "x", 30_000)
w.put("window", "y", 30_000)
w.put("window", "w", 790)
w.put("window", "h", 600)
w.save()
off = window(Settings(), store=w)
check("a remembered position that is off every screen is ignored",
      off.x() < 20_000 and off.y() < 20_000, f"{off.x()},{off.y()}")
check("... but the size, which is the window's own, is still used",
      (off.width(), off.height()) == (790, 600),
      f"{off.width()}x{off.height()}")
off.close()

# A rectangle remembered on a larger monitor must not open with its controls off
# the edges of this one.
huge = prefs.PrefsStore(tmp_path("huge.json"))
for key, value in (("x", 10), ("y", 10), ("w", 6000), ("h", 4000)):
    huge.put("window", key, value)
huge.save()
large = window(Settings(), store=huge)
area = QApplication.primaryScreen().availableGeometry()
check("a size remembered from a bigger monitor is cut down to this screen",
      large.width() <= area.width() and large.height() <= area.height()
      and large.width() >= large.minimumWidth(),
      f"{large.width()}x{large.height()} on a {area.width()}x{area.height()} screen")
large.close()

# A maximised window comes back maximised -- and the size to un-maximise to is
# still the one the user chose, not the screen's.
mx = prefs.PrefsStore(tmp_path("max.json"))
mx.put("window", "x", 60)
mx.put("window", "y", 40)
mx.put("window", "w", 790)
mx.put("window", "h", 620)
mx.put("window", "maximised", True)
mx.save()
big = window(Settings(), store=mx)
big.show()
app.processEvents()
check("a window that was left maximised comes back maximised", big.isMaximized(),
      str(big.isMaximized()))
big.close()
again_w = json.load(open(tmp_path("max.json")))["window"]
check("... and the size to un-maximise to is still the one the user chose",
      (again_w["w"], again_w["h"], again_w["maximised"]) == (790, 620, True),
      json.dumps(again_w))
plain_w = window(Settings(), store=w)
plain_w.show()
app.processEvents()
check("a window that was left normal does not come back maximised",
      not plain_w.isMaximized() and (plain_w.width(), plain_w.height()) == (790, 600),
      f"maximised={plain_w.isMaximized()} {plain_w.width()}x{plain_w.height()}")
plain_w.close()

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("all prefs checks passed")
