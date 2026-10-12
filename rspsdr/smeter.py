"""The signal meter: the scale, the dial and the needle's ballistics.

Three separate things live here, and keeping them apart is what makes the meter
checkable.

**The scale is the IARU recommendation**, which is the only definition of an S
unit there is: S9 is -73 dBm, i.e. 50 uV RMS into 50 ohms at the receiver input;
each S unit below it is 6 dB, so S1 is -121 dBm; and above S9 the standard's
steps are 10 dB, so the dial ends at S9+60, -13 dBm.  That is plain arithmetic
over a level in dBm, and :func:`reading` turns it into the words an operator
would say ("S9 +20 dB").  What the recommendation does *not* define is the
artwork, and nothing here pretends to: see below.

**The dial is the printed-dial convention of the radios this is modelled on.**
The marks are spaced evenly around the arc -- S1 to S9 across the left half, the
dB-over-S9 marks across the right -- which is what those faces look like, and
which is deliberately *not* linear in dB: the top end of a real S-meter dial is
compressed, and reproducing that is the point of drawing one.  The consequence
is that the dial resolves a level to about 1.5 dB in the S region and 2.5 dB
above S9 rather than exactly, so the exact figure is also given as text
(:func:`reading`) and the needle position is never the only thing on offer.

**The ballistics are the recommended quasi-peak ones**: a 10 ms attack and a
500 ms decay time constant, with a peak-hold needle that keeps a maximum for
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
needle, which is what makes one calibration hold across a range.  Below that,
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
TOP_STEP = 60                                    # the dial ends at S9+60

# The dial runs a little below S1, so a needle with no signal at all rests
# *below* the first mark instead of sitting on it, which is where a real one
# sits.
DIAL_LO_DBM = S1_DBM - DB_PER_UNIT               # -127 dBm
DIAL_HI_DBM = S9_DBM + TOP_STEP                  # -13 dBm

# The level that reads S9, in dBFS of the demodulator's passband measurement.
# Measured rather than assumed: see the module docstring.
DEFAULT_REF_DBFS = -45.0

# The meter's own size, in pixels.  A wide, shallow window over a needle whose
# pivot is out of sight below it, which is the shape of every analogue meter.
WIDTH = 236
HEIGHT = 86


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
        # Below the first mark the needle is off the scale, and saying
        # "S0.4" would be pretending to a resolution that is not there.
        return "S<1"
    return f"S{units:.1f}"


# --- the dial ----------------------------------------------------------
# One entry per printed tick: (dBm, label or None, whether it is a long tick).
# Below S9 a tick every half unit (3 dB), numbered every whole one; above S9 a
# tick every 5 dB, with the standard's 10 dB steps long and +20/+40/+60
# numbered.  This is a dial, not a graph: the marks are what an operator reads,
# and the spaces between them carry no information beyond "between these two".
MARKS: list[tuple[float, str | None, bool]] = [
    # The stop the needle rests on, one mark below S1 and no number: with no
    # signal the needle sits here rather than on S1, which is where a real one
    # sits, and anything below it reads off the scale.
    (DIAL_LO_DBM, None, False),
]
for _k in range(17):                      # S1 .. S9, half-unit ticks
    _dbm = S1_DBM + _k * (DB_PER_UNIT / 2.0)
    _major = _k % 2 == 0
    MARKS.append((_dbm, str(1 + _k // 2) if _major else None, _major))
for _k in range(1, 13):                   # S9 + 5 dB .. S9 + 60 dB
    _dbm = S9_DBM + _k * (DB_PER_STEP / 2.0)
    MARKS.append((_dbm, f"+{_k * 5}" if _k % 4 == 0 else None, _k % 2 == 0))

# The needle's sweep, in degrees either side of vertical, and how far below the
# window the pivot sits as a multiple of its height.  A large radius and a
# distant pivot is what makes a meter's scale read as a shallow arc rather than
# a dial, and it is why the needle enters the window almost vertically.
START_DEG = -34.0
SPAN_DEG = 68.0
PIVOT_DROP = 1.5


class Dial:
    """Where the needle points for a given level.

    The marks are evenly spaced around the arc (by *mark*, the way a printed
    dial is laid out) and a level between two of them is interpolated linearly
    in dB, so the mapping is exact at every mark, monotonic, and continuous --
    which is everything a needle can promise.
    """

    def __init__(self, start_deg: float = START_DEG, span_deg: float = SPAN_DEG,
                 marks: list[tuple[float, str | None, bool]] | None = None):
        self.marks = list(marks if marks is not None else MARKS)
        self.start_deg = float(start_deg)
        self.span_deg = float(span_deg)
        self._step = (self.span_deg / (len(self.marks) - 1)
                      if len(self.marks) > 1 else 0.0)

    # -- the arc -----------------------------------------------------
    def mark_angle(self, index: int) -> float:
        """Angle of one mark, in degrees from vertical, positive to the right."""
        return self.start_deg + index * self._step

    def angle(self, dbm: float) -> float:
        """Angle for a level in dBm, clamped to the ends of the dial."""
        lo = self.marks[0][0]
        hi = self.marks[-1][0]
        if dbm <= lo:
            return self.mark_angle(0)
        if dbm >= hi:
            return self.mark_angle(len(self.marks) - 1)
        for i in range(len(self.marks) - 1):
            a, b = self.marks[i], self.marks[i + 1]
            if a[0] <= dbm <= b[0]:
                t = (dbm - a[0]) / (b[0] - a[0])
                return self.mark_angle(i) + t * self._step
        return self.mark_angle(len(self.marks) - 1)     # pragma: no cover

    def dbm_at_angle(self, angle: float) -> float:
        """The level a given needle angle means -- the inverse of :meth:`angle`."""
        if angle <= self.mark_angle(0):
            return self.marks[0][0]
        last = len(self.marks) - 1
        if angle >= self.mark_angle(last):
            return self.marks[last][0]
        i = int((angle - self.start_deg) / self._step)
        i = max(0, min(i, last - 1))
        t = (angle - self.mark_angle(i)) / self._step
        a, b = self.marks[i][0], self.marks[i + 1][0]
        return a + t * (b - a)

    def resolution_db(self, dbm: float) -> float:
        """dB between neighbouring marks at this level: the dial's own limit.

        Half of it is the finest a needle position can be read to, which is the
        honest accuracy of the moving-coil instrument this is a picture of.
        """
        lo = self.marks[0][0]
        hi = self.marks[-1][0]
        dbm = min(max(dbm, lo), hi)
        for i in range(len(self.marks) - 1):
            if self.marks[i][0] <= dbm <= self.marks[i + 1][0]:
                return self.marks[i + 1][0] - self.marks[i][0]
        return self.marks[1][0] - self.marks[0][0]      # pragma: no cover


def needle_geometry(rect_w: float, rect_h: float, angle_deg: float,
                    radius_frac: float = 1.0) -> tuple[float, float, float, float]:
    """The visible needle: a line from the pivot to a point on the arc.

    Coordinates are the widget's own, origin top-left, with the pivot below the
    bottom edge -- so the caller draws only the part that is inside the window,
    exactly as a real meter's needle is only visible through its glass.
    Returns ``(x_pivot, y_pivot, x_tip, y_tip)``.
    """
    cx = rect_w / 2.0
    pivot_y = rect_h * (1.0 + PIVOT_DROP)
    # The arc sits a fixed fraction of the way down the window, which is what
    # the ticks are drawn along and what the tip of the needle reaches.
    radius = (pivot_y - 0.14 * rect_h) * float(radius_frac)
    rad = math.radians(angle_deg)
    return (cx, pivot_y,
            cx + radius * math.sin(rad),
            pivot_y - radius * math.cos(rad))


class Ballistics:
    """The needle's response: how fast it rises, how slowly it falls.

    An exponential integrator on the level in dB, with the recommended quasi-peak
    constants -- a 10 ms attack, a 500 ms decay -- and a peak-hold needle that
    keeps the highest level of the last half second before it starts to fall.

    Deliberately fed by explicit time steps rather than by a clock: the tests
    drive it at 1 ms and can say what the needle does in the first ten, and the
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
            # First sample: come straight to it.  A needle that had to travel
            # from its rest position would show a slow rise that never happened.
            self.value = float(target)
            self.peak = float(target)
            self._hold_left = self.peak_hold
            return self.value, self.peak

        tau = self.attack if target > self.value else self.decay
        alpha = 1.0 - math.exp(-dt / tau) if tau > 0.0 else 1.0
        self.value += (float(target) - self.value) * alpha

        if float(target) > self.peak or self.peak is None:
            # The peak needle follows the *level*, not the needle: a signal
            # that is gone again before the needle has finished rising would
            # otherwise leave no trace of itself on the peak either, which is
            # the one thing a peak hold is for.
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
        "face": "#12161b",          # the dial itself
        "face2": "#1b222a",         # the sheen across it
        "bezel": "#3f88ad",
        "major": "#e8f1f7",
        "minor": "#8fa2b3",
        "number": "#e9f5ff",
        "needle": "#e8362a",
        "needle_edge": "#1a0b09",
        "peak": "#ffd166",
        "caption": "#7f97ab",
    },
    "cream": {
        "face": "#ded7c2",
        "face2": "#efe9d8",
        "bezel": "#6b6656",
        "major": "#17191c",
        "minor": "#6a6a63",
        "number": "#17191c",
        "needle": "#b8231a",
        "needle_edge": "#5a4038",
        "peak": "#2f6f4f",
        "caption": "#5a5f68",
    },
}
FACE_NAMES = ("dark", "cream")
