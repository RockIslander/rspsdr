"""Tests for the device at startup: what makes an open fail, and what the
window does about it.

The SDRplay front end serves one client at a time.  While another program holds
it, ``Device.enumerate()`` returns nothing and ``Device()`` raises "no available
RSP devices found" -- so the window used to report "no SDR found", then
"error: no available RSP devices found", and stay dead until the program was
restarted.  It is not absent, it is busy, and it frees itself the moment the
other program closes.

None of that can be tested with a real device, because a real device cannot be
made busy to order.  So the SoapySDR module is replaced by a fake that fails
wherever it is told to, the audio backend by a stub, and the retry policy is
driven one step at a time instead of by sleeping.

Run with:  QT_QPA_PLATFORM=offscreen python3 tests/test_startup.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rspsdr.radio as radio_mod  # noqa: E402
from PyQt6.QtCore import QEventLoop, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from rspsdr.radio import (  # noqa: E402
    Radio, Settings, list_devices, transient_open_error,
)
from rspsdr.ui import (  # noqa: E402
    OPEN_RETRY_FIRST, OPEN_RETRY_MAX, DeviceOpener, MainWindow,
)

failures: list[str] = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + (f" :: {detail}" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


BUSY = "no available RSP devices found"     # what SDRplay says when held

print("== which open failures are worth waiting for ==")
# Retryable: the device is there but somebody else has it.
for text in (BUSY,
             "SoapySDR::Device::make() no match",
             "sdrplay_api_Open() failed: device not ready",
             "device is already in use by another process",
             "resource unavailable"):
    check(f"“{text[:38]}…” is waited for", transient_open_error(text))
# Not retryable: waiting would never help, and a "waiting" message would hide
# the real problem -- a missing binding or a driver that does not exist.
for text in ("SoapySDR Python bindings are not available (install python3-soapysdr)",
             "no driver found for driver=nosuchdriver",
             "SoapySDR::Device::make() failed: invalid argument"):
    check(f"“{text[:38]}…” is reported at once", not transient_open_error(text))
check("it accepts an exception as well as a message",
      transient_open_error(RuntimeError(BUSY)))


class FakeStream:
    def __init__(self, ret=0):
        self.ret = ret


class FakeDevice:
    """The parts of a SoapySDR device that Radio.open() touches."""

    def __init__(self, fail_at=None, error=BUSY):
        self.fail_at = fail_at
        self.error = error
        self.closed = False
        self.activated = False
        self.deliver = True             # samples while the cable is in

    def _fail(self, step):
        if self.fail_at == step:
            raise RuntimeError(self.error)

    def setSampleRate(self, *a):
        self._fail("setSampleRate")

    def setFrequency(self, *a):
        self._fail("setFrequency")

    def setAntenna(self, *a):
        pass

    def setGainMode(self, *a):
        pass

    def setGain(self, *a):
        pass

    def setBandwidth(self, *a):
        self._fail("setBandwidth")

    def setDCOffsetMode(self, *a):
        pass

    def setupStream(self, *a):
        self._fail("setupStream")
        return FakeStream()

    def activateStream(self, *a):
        self.activated = True
        self._fail("activateStream")

    def deactivateStream(self, *a):
        pass

    def closeStream(self, *a):
        pass

    def readStream(self, *a, **kw):
        # Slow enough that the reader thread is not a busy loop for the moment
        # it is alive.  `deliver` is what a real device stops doing when its
        # cable comes out: no exception, just no samples ever again.
        time.sleep(0.002)
        if not self.deliver:
            return FakeStream(0)
        # Small blocks: enough to count as a live stream, not enough to set the
        # DSP thread working through a megasample a second of made-up IQ.
        return FakeStream(min(int(a[2]) if len(a) > 2 else 0, 256))

    def getSampleRate(self, *a):
        return 2_000_000

    def close(self):
        self.closed = True


class FakeDevices:
    """``SoapySDR.Device``: callable to make a device, and able to enumerate.

    One object, not two, because that is what the real module is: the window
    lists devices through ``SoapySDR.Device.enumerate()`` while the radio makes
    one through ``SoapySDR.Device(args)``, so a fake that only did the second
    broke the first.
    """

    def __init__(self, soapy: "FakeSoapy"):
        self.soapy = soapy

    def __call__(self, args):
        s = self.soapy
        if s.fail_make:
            raise RuntimeError(s.error)
        dev = FakeDevice(s.fail_at, s.error)
        s.made.append(dev)
        return dev

    def enumerate(self):
        self.soapy.enumerated += 1
        return []


class FakeSoapy:
    """Stands in for the SoapySDR module, failing where it is told to."""

    def __init__(self, fail_at=None, error=BUSY, fail_make=False):
        self.fail_at = fail_at
        self.error = error
        self.fail_make = fail_make
        self.made: list[FakeDevice] = []
        self.enumerated = 0
        self.Device = FakeDevices(self)


class _StubBackend:
    def describe(self):
        return "stub"


class StubAudio:
    """Stands in for the audio output so no sound card is needed."""

    def __init__(self, rate, backend="auto"):
        self.rate = rate
        self.backend = _StubBackend()
        self.volume = 1.0
        self.muted = False
        self.closed = False
        self.started = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def write(self, data):
        return True


class StubRadio:
    """Open/close only: what DeviceOpener is allowed to touch."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)      # exceptions, or None for success
        self.calls = 0
        self._dev = None
        self.closed = 0

    @property
    def opened(self) -> bool:
        return self._dev is not None

    def open(self):
        self.calls += 1
        exc = self.outcomes.pop(0) if self.outcomes else None
        if exc is not None:
            raise exc
        self._dev = object()

    def close(self):
        self._dev = None
        self.closed += 1


print("\n== a failed open leaves nothing claimed ==")
real_soapy, real_have = radio_mod.SoapySDR, radio_mod._HAVE_SOAPY
real_audio = radio_mod.AudioOutput
try:
    radio_mod.AudioOutput = StubAudio

    # Failing *after* the device was made is the case that matters: the handle
    # exists, nothing is streaming, and the front end serves one client at a
    # time -- so leaving it claimed would make every later attempt fail.
    fake = FakeSoapy(fail_at="setupStream")
    radio_mod._HAVE_SOAPY, radio_mod.SoapySDR = True, fake
    r = Radio(Settings())
    try:
        r.open()
        raised = None
    except Exception as exc:
        raised = exc
    check("a failure mid-open propagates", isinstance(raised, RuntimeError)
          and BUSY in str(raised), repr(raised))
    check("... and the device it had made is released",
          len(fake.made) == 1 and fake.made[0].closed,
          f"{len(fake.made)} device(s) made, released: "
          f"{fake.made[0].closed if fake.made else '-'}")
    check("... and the radio does not claim to be open",
          not r.opened and r.status.running is False and r.status.error == "")
    check("... and no worker threads are left behind",
          r._reader is None and r._dsp is None and r.audio is None)

    # The same radio must be usable afterwards: a retry starts from a clean
    # state rather than from a half-claimed one.
    radio_mod.SoapySDR = FakeSoapy(fail_at=None)
    r.open()
    check("the next attempt on the same object opens normally",
          r.opened and radio_mod.SoapySDR.made[-1].closed is False,
          f"opened {r.opened}")
    check("... and it is really streaming: threads, audio and status",
          r._reader is not None and r._reader.is_alive()
          and r._dsp is not None and r._dsp.is_alive()
          and r.audio is not None and r.audio.started
          and r.status.message == "streaming")
    r.close()
    check("closing releases it again",
          not r.opened and radio_mod.SoapySDR.made[-1].closed
          and r.audio is None and r._reader is None)

    # A device that cannot even be made -- someone else holds it, so the module
    # finds nothing -- must leave nothing behind to clean up either.
    fake3 = FakeSoapy(fail_make=True)
    radio_mod.SoapySDR = fake3
    r2 = Radio(Settings())
    try:
        r2.open()
        raised = None
    except Exception as exc:
        raised = exc
    check("a device that cannot be made reports the real error",
          isinstance(raised, RuntimeError) and BUSY in str(raised), repr(raised))
    check("... and leaves nothing claimed behind it",
          not r2.opened and fake3.made == [] and r2.audio is None
          and r2._reader is None)
finally:
    radio_mod.SoapySDR, radio_mod._HAVE_SOAPY = real_soapy, real_have
    radio_mod.AudioOutput = real_audio

print("\n== a stream that dies mid-session ==")
# The device can go away while the radio is running -- unplugged, the service
# restarted, a driver wedged after a resume.  Reading it again is what used to
# happen, for ever: a frozen waterfall and a Stop button that stops nothing.
STALL = 0.6                     # seconds, instead of the three in the field
fake4 = FakeSoapy()
lost: list[str] = []
real_stall = radio_mod.STALL_SECONDS
real_audio2 = radio_mod.AudioOutput
try:
    radio_mod.AudioOutput = StubAudio
    radio_mod.STALL_SECONDS = STALL
    radio_mod.SoapySDR, radio_mod._HAVE_SOAPY = fake4, True
    r3 = Radio(Settings(), on_lost=lost.append)
    r3.open()
    dev4 = fake4.made[-1]
    opened_at = r3._last_data
    deadline = time.monotonic() + 3 * STALL
    while time.monotonic() < deadline:
        time.sleep(0.05)
    # The false positive that matters: a stream that is delivering samples must
    # never be mistaken for a dead one, or the window would reopen the radio at
    # random while somebody is listening to it.
    check("a stream that is delivering samples is left alone",
          not lost and r3.opened and r3._reader.is_alive()
          and r3._last_data > opened_at and r3.status.running,
          f"{len(lost)} loss report(s) over {3 * STALL:.1f} s, "
          f"samples {r3._last_data > opened_at}")

    dev4.deliver = False            # the cable comes out
    fake4.fail_make = True          # ... and the device is not there to reopen
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not lost:
        time.sleep(0.05)
    check("a stream that goes quiet is called a lost device",
          bool(lost) and "no samples" in lost[0] and not r3.status.running
          and r3.status.message == "device lost",
          f"{lost!r}, status {r3.status.message!r}")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and (r3._reader.is_alive()
                                           or r3._dsp.is_alive()):
        time.sleep(0.02)
    check("... both of its threads stop instead of spinning on a dead stream",
          not r3._reader.is_alive() and not r3._dsp.is_alive())
    check("... it is reported once, not every time round the loop",
          len(lost) == 1, f"{len(lost)} reports")
    check("... and the device is left for the owner to release",
          r3.opened and not dev4.closed)
    r3.close()
    check("... which they can, and then the same object opens again",
          not r3.opened and dev4.closed)

    # Stopping on purpose must not look like losing the device: the check is
    # skipped once the radio is being stopped, so a rate change (which closes
    # and reopens) cannot report a loss it caused itself.
    fake5 = FakeSoapy()
    radio_mod.SoapySDR = fake5
    lost5: list[str] = []
    r4 = Radio(Settings(), on_lost=lost5.append)
    r4.open()
    r4.close()
    check("stopping the radio on purpose is not a lost device",
          not lost5, f"{lost5!r}")
finally:
    radio_mod.STALL_SECONDS = real_stall
    radio_mod.AudioOutput = real_audio2

print("\n== the retry policy ==")
app = QApplication.instance() or QApplication([])

started: list[str] = []
waits: list[tuple[str, float]] = []
gaves: list[str] = []
stub = StubRadio([RuntimeError(BUSY), RuntimeError(BUSY), None])
opener = DeviceOpener(stub)
opener.opened.connect(lambda: started.append("opened"))
opener.waiting.connect(lambda why, d: waits.append((why, d)))
opener.gave_up.connect(lambda why: gaves.append(why))

check("a busy device is not reported as a failed start",
      opener.start() is False and stub.calls == 1 and not gaves,
      f"{stub.calls} attempt(s), {len(gaves)} give-up(s)")
check("... it schedules another attempt instead",
      opener.waiting_for_device and opener._timer.interval() == int(OPEN_RETRY_FIRST * 1000),
      f"timer {opener._timer.interval()} ms, attempts {opener.attempts}")
check("... and says so, with the reason and the wait",
      len(waits) == 1 and BUSY in waits[0][0]
      and abs(waits[0][1] - OPEN_RETRY_FIRST) < 1e-6,
      f"{waits!r}")
check("... and the device stays reported as not open", not stub.opened)

opener._attempt()                       # the timer's slot, one step at a time
check("a second failure keeps trying, at the longer gap",
      opener.waiting_for_device
      and opener._timer.interval() == int(OPEN_RETRY_MAX * 1000)
      and opener.attempts == 2,
      f"timer {opener._timer.interval()} ms, attempts {opener.attempts}")
opener._attempt()
check("the attempt that succeeds reports the radio open",
      stub.opened and started == ["opened"] and not opener.waiting_for_device,
      f"opened signal x{len(started)}, timer active {opener.waiting_for_device}")
check("... and the attempt count is reset for next time", opener.attempts == 0)

# Out of time: still busy after the whole budget, so stop asking rather than
# hammer the front end for the rest of the session.
stub3 = StubRadio([RuntimeError(BUSY)] * 3)
opener3 = DeviceOpener(stub3)
gave3: list[str] = []
opener3.gave_up.connect(lambda why: gave3.append(why))
opener3.start()
opener3._deadline = time.monotonic() - 1.0        # the whole budget is spent
opener3._attempt()
check("after the retry budget is spent it stops asking",
      gave3 == [BUSY] and not opener3.waiting_for_device and stub3.calls == 2,
      f"{stub3.calls} attempt(s), timer active {opener3.waiting_for_device}, "
      f"gave up with {gave3!r}")

# An error that no amount of waiting will fix is reported immediately.
stub4 = StubRadio([RuntimeError("SoapySDR Python bindings are not available")])
opener4 = DeviceOpener(stub4)
gave4: list[str] = []
opener4.gave_up.connect(lambda why: gave4.append(why))
check("a failure that waiting cannot fix is not retried",
      opener4.start() is False and opener4.attempts == 1
      and not opener4.waiting_for_device and gave4
      and "bindings" in gave4[0],
      f"{stub4.calls} attempt(s), gave_up {gave4!r}")

# The window closing, or the user pressing Stop, must stop the retrying.
stub5 = StubRadio([RuntimeError(BUSY)] * 3)
opener5 = DeviceOpener(stub5)
opener5.start()
opener5.cancel()
check("cancelling stops the retry", not opener5.waiting_for_device)

# And the timer really does fire on its own: the check above drives _attempt()
# by hand, which would hide a timer that was never started.
stub6 = StubRadio([RuntimeError(BUSY), None])
opener6 = DeviceOpener(stub6)
opener6.start()
loop = QEventLoop()
QTimer.singleShot(int(OPEN_RETRY_FIRST * 1000) + 700, loop.quit)
loop.exec()
check("the retry fires on the event loop without help",
      stub6.opened and opener6.attempts == 0 and stub6.calls == 2,
      f"{stub6.calls} attempt(s), opened {stub6.opened}")

print("\n== the window recovers by itself ==")
MEM = os.path.join(tempfile.mkdtemp(prefix="rsp1-startup-"), "memories.json")
win = MainWindow(Settings(), MEM)
real_open, real_close = win.radio.open, win.radio.close


def busy_open():
    raise RuntimeError(BUSY)


win.radio.open = busy_open
check("a busy device at startup leaves the app stopped, not dead",
      win.start_radio() is False and not win.radio.opened
      and win.btn_start.text().startswith("▶"),
      f"button “{win.btn_start.text()}”, status “{win.lbl_status.text()}”")
check("... the status bar says the device is busy and it is retrying",
      "busy" in win.lbl_status.text() and "trying again" in win.lbl_status.text(),
      win.lbl_status.text())
check("... and it no longer claims there is no SDR attached",
      "no SDR" not in win.lbl_device.text(),
      f"device label “{win.lbl_device.text()}”")
check("... with a retry pending", win.opener.waiting_for_device)

# Pressing Start while it waits must try immediately rather than refuse.
check("pressing Start while it waits tries again at once",
      win.start_radio() is False and win.opener.attempts == 1
      and win.opener.waiting_for_device)
check("... and restarts the retry budget, so a delayed press still works",
      win.opener._deadline > time.monotonic(),
      f"{win.opener._deadline - time.monotonic():.0f} s of budget left")

# The other program closes: the next scheduled attempt gets the device, and
# everything the user sees comes back with it.
win.radio.open = lambda: setattr(win.radio, "_dev", object())
win.opener._attempt()
check("when the device frees up it starts streaming, unattended",
      win.radio.opened and win.btn_start.text().startswith("■")
      and not win.opener.waiting_for_device,
      f"button “{win.btn_start.text()}”")
# Refreshed from the device list, whatever that list says: the label has to
# match an enumeration taken now -- not the "waiting" placeholder, and not a
# stale "no SDR found" from before the device turned up.  (Another program on a
# machine can hold the RSP1 for a moment, and then the list really is empty.)
found = list_devices()
check("... and the device label is refreshed from the device list",
      win.lbl_device.text() == (found[0]["label"] if found else "no SDR found"),
      f"“{win.lbl_device.text()}” for "
      f"{found[0]['label'] if found else 'nothing enumerated'}")
check("... and the status bar stops saying it is still trying",
      "trying again" not in win.lbl_status.text(), win.lbl_status.text())

win.radio.close = lambda: setattr(win.radio, "_dev", None)
win._toggle()                            # the user presses Stop
check("pressing Stop cancels any pending retry", not win.opener.waiting_for_device)


def missing_bindings():
    raise RuntimeError("SoapySDR Python bindings are not available")


win.radio.open = missing_bindings
win.start_radio()
check("an error that waiting cannot fix is reported and not retried",
      not win.opener.waiting_for_device and "bindings" in win.lbl_status.text(),
      win.lbl_status.text())

win.radio.open, win.radio.close = real_open, real_close
win.close()

print("\n== the window recovers from a device lost mid-session ==")
# The whole path, through a real Radio: the reader thread notices the silence,
# the window gives the device up, and the same retry that covers a busy device
# at startup brings it back when it returns -- with nothing clicked.
fake6 = FakeSoapy()
real_stall2 = radio_mod.STALL_SECONDS
real_audio3 = radio_mod.AudioOutput
try:
    radio_mod.AudioOutput = StubAudio
    radio_mod.STALL_SECONDS = 0.6
    radio_mod.SoapySDR, radio_mod._HAVE_SOAPY = fake6, True
    win2 = MainWindow(Settings(), os.path.join(tempfile.mkdtemp(
        prefix="rsp1-lost-"), "memories.json"))
    # Every status message, not just the one left on the bar at the end: the
    # "stopped streaming" line is deliberately short-lived, because the retry
    # replaces it a moment later with what it is doing about it.
    seen: list[str] = []
    real_status_line = win2._on_status_line

    def record(text: str):
        seen.append(text)
        real_status_line(text)

    win2._on_status_line = record
    check("the window streams through a stand-in device",
          win2.start_radio() and win2.radio.opened
          and win2.btn_start.text().startswith("■"),
          f"button “{win2.btn_start.text()}”")
    dev6 = fake6.made[-1]
    for _ in range(20):                     # let a few blocks really arrive
        app.processEvents()
        time.sleep(0.02)
    check("the stream is live before anything is pulled out",
          dev6.deliver and win2.radio.opened)

    dev6.deliver = False                    # the cable comes out
    fake6.fail_make = True                  # and it is not there to reopen yet
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and win2.radio.opened:
        app.processEvents()
        time.sleep(0.02)
    check("the window gives a device that stopped up",
          not win2.radio.opened and win2.btn_start.text().startswith("▶"),
          f"opened {win2.radio.opened}, button “{win2.btn_start.text()}”")
    check("... says what happened", any("stopped streaming" in t for t in seen),
          next((t for t in seen if "stopped streaming" in t), "nothing"))
    check("... and is already looking for it again",
          win2.opener.waiting_for_device
          and "trying again" in win2.lbl_status.text(),
          win2.lbl_status.text())

    fake6.fail_make = False                 # plugged back in
    win2.opener._attempt()
    check("when it comes back the window streams again, unattended",
          win2.radio.opened and win2.btn_start.text().startswith("■")
          and not win2.opener.waiting_for_device,
          f"button “{win2.btn_start.text()}”, status “{win2.lbl_status.text()}”")
    check("... with the device name back in the header",
          "not available" not in win2.lbl_device.text(), win2.lbl_device.text())

    # Closing the window while a retry is pending must not start one afterwards.
    dev6.deliver = False
    win2.close()
    app.processEvents()
    check("closing the window stops the looking",
          win2._closing and not win2.opener.waiting_for_device)
finally:
    radio_mod.STALL_SECONDS = real_stall2
    radio_mod.AudioOutput = real_audio3
    radio_mod.SoapySDR, radio_mod._HAVE_SOAPY = real_soapy, real_have

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all startup checks passed")
