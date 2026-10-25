"""The signal meter: the scale, the bar that shows it, and the ballistics.

Three separate things live here, and keeping them apart is what makes the meter
checkable.

**The scale is the IARU recommendation**, which is the only definition of an S
unit there is: S9 is -73 dBm, i.e. 50 uV RMS into 50 ohms at the receiver input;
each S unit below it is 6 dB, so S1 is -121 dBm; and above S9 the standard's
steps are 10 dB, so the scale ends at S9+60, -13 dBm.  That is plain arithmetic
over a level in dBm, and :func:`reading` turns it into the words an operator
would say ("S9 +20 dB").  What the recommendation does *not* define is the
artwork, and nothing here pretends to: see below.

**The bar is that scale drawn as a strip** (:class:`Bar`).  It fills to the
position of the level on the scale, and the scale's marks are drawn across it
keeping the printed-dial convention: evenly spaced *by mark* rather than linear
in dB, so S1 to S9 take the first three fifths of the strip and the 10 dB steps
above S9 the rest, and the top of the scale is compressed the way a printed face
is.  That is what an S-meter looks like and what a transceiver's bar meter looks
like, and reproducing it is the point.  The consequence is that the strip
resolves a level to the distance between two marks -- 3 dB in the S region, 5 dB
above S9 -- so the exact figure is also given as text beside it (:func:`reading`)
and the bar is never the only thing on offer.

An arc with a needle over it used to be drawn above the bar, on this same scale.
It was removed for the picture's sake: the bar carries what the instrument is for
-- where the signal is on the IARU scale, and where the peak of it was -- in 38 px
of height instead of 222, and the waterfall that height paid for is the thing
being watched.  What only the needle could do, being read to half a mark, the
figures beside the bar do better.

**The ballistics are the recommended quasi-peak ones**: a 10 ms attack and a
500 ms decay time constant, with a peak-hold marker that keeps a maximum for
half a second and then falls away slowly (the half second is Icom's own
documented figure for the peak segment on their digital meters).

Where the meter is *fed* from matters as much as any of that.  It reads the
demodulator's passband level (``Demodulator.passband_db``): the channel filter's
own output, before the AGC, the noise reduction and the volume, so it shows what
arrived at the receiver rather than how loudly the user is listening.  That
level is in dBFS, and the absolute calibration of the RSP1 is not known -- there
is no power sensor on this device and the level is taken after the gain stages
-- so one number is asked of the user: :data:`DEFAULT_REF_DBFS`, the level that
reads S9.  The default was chosen from measurements on a real RSP1 (2 MSps, a
6 kHz AM passband, the default IF gain of 40 dB): a whole band of HF noise reads
around S5-S7 from 60 m to 19 m, and 10 dB of gain shows as exactly 10 dB on the
bar, which is what makes one calibration hold across a range.  Below that,
S1 is -121 dBm -- under this receiver's own noise floor even at its lowest gain,
where the noise still reads S3.8 -- so the bottom of the scale is the
calibration's business rather than the hardware's, and anyone with a known
signal, or a signal generator, can set it to make the meter agree with it.
"""

from __future__ import annotations

import math

# --- the standard ------------------------------------------------------
# IARU Region 1 technical recommendation: 50 uV into 50 ohms at the receiver
# input is S9, which is -73 dBm in a 50 ohm system; 6 dB per S unit below it and
# the standard's 10 dB steps above it.
S9_DBM = -73.0
DB_PER_UNIT = 6.0
DB_PER_STEP = 10.0
S1_DBM = S9_DBM - 8.0 * DB_PER_UNIT              # -121 dBm
TOP_STEP = 60                                    # the scale ends at S9+60

# The scale runs a little below S1, so a bar with no signal at all rests *below*
# the first mark instead of sitting on it, and anything under it reads "S<1".
SCALE_LO_DBM = S1_DBM - DB_PER_UNIT              # -127 dBm
SCALE_HI_DBM = S9_DBM + TOP_STEP                 # -13 dBm

# The level that reads S9, in dBFS of the demodulator's passband measurement.
# Measured rather than assumed: see the module docstring.
DEFAULT_REF_DBFS = -45.0


def dbm_for_s(units: float) -> float:
    """dBm for a number of S units -- 6 dB each, below S9."""
    return S9_DBM - (9.0 - float(units)) * DB_PER_UNIT


def s_for_dbm(dbm: float) -> float:
    """S units for a level in dBm (9.0 at S9, above 9 in units of 6 dB)."""
    return 9.0 + (float(dbm) - S9_DBM) / DB_PER_UNIT


def dbm_for_dbfs(dbfs: float, ref_dbfs: float = DEFAULT_REF_DBFS) -> float:
    """The meter's level in dBm, from a passband level in dBFS.

    ``ref_dbfs`` is the dBFS reading that should show as S9.  One number, and
    the only calibration the meter has: everything else follows from the
    standard.
    """
    return (float(dbfs) - float(ref_dbfs)) + S9_DBM


def reading(dbm: float) -> str:
    """The level as an operator would say it: "S9 +20 dB", "S7.4", "S<1"."""
    if dbm >= S9_DBM:
        return f"S9 +{float(dbm) - S9_DBM:.0f} dB"
    units = s_for_dbm(dbm)
    if units < 1.0:
        # Below the first mark the bar is off the scale, and saying "S0.4"
        # would be pretending to a resolution that is not there.
        return "S<1"
    return f"S{units:.1f}"


# --- the scale ---------------------------------------------------------
# One entry per printed tick: (dBm, label or None, whether it is a long tick).
# Below S9 a tick every half unit (3 dB), numbered every whole one; above S9 a
# tick every 5 dB, with the standard's 10 dB steps long and +20/+40/+60
# numbered.  This is a meter's scale, not a graph: the marks are what an
# operator reads, and the spaces between them carry no information beyond
# "between these two".
MARKS: list[tuple[float, str | None, bool]] = [
    # The stop the bar rests on, one mark below S1 and no number: with no
    # signal it fills nothing rather than standing on S1, and anything below it
    # reads off the scale.
    (SCALE_LO_DBM, None, False),
]
for _k in range(17):                      # S1 .. S9, half-unit ticks
    _dbm = S1_DBM + _k * (DB_PER_UNIT / 2.0)
    _major = _k % 2 == 0
    MARKS.append((_dbm, str(1 + _k // 2) if _major else None, _major))
for _k in range(1, 13):                   # S9 + 5 dB .. S9 + 60 dB
    _dbm = S9_DBM + _k * (DB_PER_STEP / 2.0)
    MARKS.append((_dbm, f"+{_k * 5}" if _k % 4 == 0 else None, _k % 2 == 0))


class Bar:
    """Where a level sits on the scale, and where that is along the strip.

    The scale is the printed-dial convention: the marks are evenly spaced (by
    *mark*, the way a printed dial is laid out) and a level between two of them
    is interpolated linearly in dB, so the mapping is exact at every mark,
    monotonic and continuous -- which is everything a strip of light can
    promise.  Kept here, outside the widget, so that "does the bar fill where the
    scale says?" is arithmetic a test can ask without a display.
    """

    def __init__(self, marks: list[tuple[float, str | None, bool]] | None = None):
        self.marks = list(marks if marks is not None else MARKS)

    def mark_position(self, index: int) -> float:
        """Where one mark is drawn: 0 at the first, 1 at the last."""
        last = max(1, len(self.marks) - 1)
        return index / last

    def position(self, dbm: float) -> float:
        """How far along the bar a level fills: 0 at the first mark, 1 at the
        last.  Levels past either end stop there, as a meter's reading does."""
        lo = self.marks[0][0]
        hi = self.marks[-1][0]
        if dbm <= lo:
            return 0.0
        if dbm >= hi:
            return 1.0
        for i in range(len(self.marks) - 1):
            a, b = self.marks[i], self.marks[i + 1]
            if a[0] <= dbm <= b[0]:
                step = self.mark_position(i + 1) - self.mark_position(i)
                return self.mark_position(i) + (dbm - a[0]) / (b[0] - a[0]) * step
        return 1.0                                     # pragma: no cover

    def x(self, dbm: float, left: float, width: float) -> float:
        """The pixel the bar reaches for a level, in ``left`` .. ``left+width``."""
        return left + self.position(dbm) * float(width)

    def resolution_db(self, dbm: float) -> float:
        """dB between neighbouring marks at this level: the scale's own limit.

        Half of it is the finest a bar can be read to, which is the honest
        accuracy of the instrument -- which is why the exact figure is printed
        beside it as well.
        """
        dbm = min(max(dbm, self.marks[0][0]), self.marks[-1][0])
        for i in range(len(self.marks) - 1):
            if self.marks[i][0] <= dbm <= self.marks[i + 1][0]:
                return self.marks[i + 1][0] - self.marks[i][0]
        return self.marks[1][0] - self.marks[0][0]      # pragma: no cover


# --- the strip's own sizes, in pixels ----------------------------------
# The bar is drawn at the size it is drawn at -- there is no second size to
# scale from and nothing to keep in step -- and it takes whatever width the
# window gives it, from its minimum up.  Everything here is either the height of
# a band in the widget or the width of a line.
BAR_HEIGHT = 38                 # the widget: the trough and the numbers under it
BAR_MIN_WIDTH = 320             # what is left for it at the narrowest window
BAR_TRACK = 18.0                # the depth of the filled trough
BAR_EDGE = 8.0                  # the gap from the glass to its ends
BAR_TOP = 3.0                   # and from the top of the glass to its top
BAR_R = 3.0                     # the trough's corner radius
BAR_GLASS_R = 6.0               # the glass's
BAR_BEZEL_W = 1.4
BAR_MARK_W = (1.4, 0.8)         # (long mark, short mark)
BAR_PEAK_W = 1.5
BAR_SHORT = 6.0                 # how far a short mark reaches up into the trough
BAR_DARKEN = 150                # the trough is the face's colour, darkened
BAR_TYPE_PT = 10                # the numbers under the trough


class Ballistics:
    """The meter's response: how fast it rises, how slowly it falls.

    An exponential integrator on the level in dB, with the recommended quasi-peak
    constants -- a 10 ms attack, a 500 ms decay -- and a peak-hold marker that
    keeps the highest level of the last half second before it starts to fall.

    Deliberately fed by explicit time steps rather than by a clock: the tests
    drive it at 1 ms and can say what the bar does in the first ten, and the
    window drives it at its repaint rate with the time it really took.
    """

    def __init__(self, attack: float = 0.010, decay: float = 0.500,
                 peak_hold: float = 0.5, peak_fall: float = 2.0):
        self.attack = float(attack)
        self.decay = float(decay)
        self.peak_hold = float(peak_hold)
        self.peak_fall = float(peak_fall)
        self.value = None            # dBm; None until the first sample
        self.peak = None
        self._hold_left = 0.0

    def reset(self) -> None:
        self.value = None
        self.peak = None
        self._hold_left = 0.0

    def update(self, target: float, dt: float) -> tuple[float, float]:
        """Advance by ``dt`` seconds towards ``target``; returns (value, peak)."""
        dt = max(0.0, float(dt))
        if self.value is None:
            # First sample: come straight to it.  A meter that had to travel
            # from its rest position would show a slow rise that never happened.
            self.value = float(target)
            self.peak = float(target)
            self._hold_left = self.peak_hold
            return self.value, self.peak

        tau = self.attack if target > self.value else self.decay
        alpha = 1.0 - math.exp(-dt / tau) if tau > 0.0 else 1.0
        self.value += (float(target) - self.value) * alpha

        if float(target) > self.peak or self.peak is None:
            # The peak marker follows the *level*, not the bar: a signal that is
            # gone again before the bar has finished rising would otherwise
            # leave no trace of itself on the peak either, which is the one
            # thing a peak hold is for.
            self.peak = float(target)
            self._hold_left = self.peak_hold
        elif self._hold_left > 0.0:
            self._hold_left = max(0.0, self._hold_left - dt)
        else:
            fall = 1.0 - math.exp(-dt / self.peak_fall)
            self.peak += (self.value - self.peak) * fall
        return self.value, self.peak


# --- the face ----------------------------------------------------------
# Two faces, both drawn from this table.  The meters these are modelled on came
# in both: a black face with white markings on the later radios and their
# colour displays, and a cream face with black markings on the older analogue
# ones.  Which one Icom used on a given model is not something the sources
# settle, so the choice is offered rather than guessed at, and "dark" is the
# default because the rest of this window is dark.
FACES: dict[str, dict[str, str]] = {
    "dark": {
        "face": "#12161b",          # the glass the strip is set in
        "face2": "#1b222a",         # the sheen across it
        "bezel": "#3f88ad",
        "major": "#e8f1f7",
        "minor": "#8fa2b3",
        "number": "#e9f5ff",
        "bar": "#e8362a",           # the filled strip
        "peak": "#ffd166",
    },
    "cream": {
        "face": "#ded7c2",
        "face2": "#efe9d8",
        "bezel": "#6b6656",
        "major": "#17191c",
        "minor": "#6a6a63",
        "number": "#17191c",
        "bar": "#b8231a",
        "peak": "#2f6f4f",
    },
}
FACE_NAMES = ("dark", "cream")
