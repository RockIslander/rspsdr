"""PyQt6 user interface: spectrum, waterfall and every control.

Layout
------
    +--------------------------------------------------------------+
    | header: the RSPSDR mark, name and tagline                     |
    +--------------------------------------------------------------+
    | tuning row: start, rate, dial, step, band, preset             |
    +--------------------------------------------------------------+
    | mode row:  AM SAM USB LSB CW NFM | filter | audio | NR        |
    +--------------------------------------------------------------+
    | memory row: saved channels                                    |
    +--------------------------------------------------------------+
    | signal meter, top right, above the picture                    |
    +--------------------------------------------------------------+
    | spectrum plot                                                 |
    +--------------------------------------------------------------+
    | waterfall                                                     |
    +--------------------------------------------------------------+
    | fold row: theme, and the meter's own setup                    |
    +--------------------------------------------------------------+
    | fold row: gains, tuner bandwidth, fft                         |
    +--------------------------------------------------------------+
    | fold row: waterfall levels, colour, detail, zoom              |
    +--------------------------------------------------------------+
    | status bar                                                    |
---------------------------------------------------------------

The control rows use :class:`FlowLayout`, so they wrap onto another line when
the window is narrower than their contents.  With an ordinary QHBoxLayout each
row imposes a hard minimum width equal to the sum of its controls -- 1469 px
for the tuning row -- which is wider than many screens, and a window whose
minimum exceeds the screen cannot be maximised or fitted at all.

Everything below the picture is inside one :class:`SettingsPane`, which folds
away when the waterfall is worth more than the controls are worth.  The
colours come from :mod:`rspsdr.theme`: one table, applied to the palette, the
style sheet, the plots' own pens and the labels that carry a colour of their
own, so that a scheme is a name rather than a dozen hex values in two files.
"""

from __future__ import annotations

import os
import time

# Must be set before pyqtgraph is imported: pyqtgraph would otherwise pick up
# the PyQt5 that Debian drags in as a dependency and two Qt bindings in one
# process is a recipe for crashes.
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")

import numpy as np
import pyqtgraph as pg

# Raster painting: avoids flaky OpenGL drivers and, more importantly, keeps
# QWidget.grab() able to capture the plots.
#
# Antialiasing is off, and that is a measurement rather than a taste.  On the
# machine this is tuned for -- an i5-4590T whose display is driven by Intel HD
# 4600 graphics, so every line is rasterised on the CPU -- one repaint of the
# two plots costs 81 ms with antialiasing and 21 ms without it: 12.9 frames a
# second against 22.5.  Two thirds of the frame budget, spent on smoothing a
# 1 px grid and a spectrum trace.  Without it the trace is merely crisper.  The
# waterfall image is the one thing that genuinely wants interpolation, and it
# asks for it on its own in FastImage below.
pg.setConfigOptions(useOpenGL=False, antialias=False)

from PyQt6.QtCore import (QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt,
                          QTimer, pyqtSignal)
from PyQt6.QtGui import (QBrush, QColor, QFont, QKeySequence, QLinearGradient,
                         QPainter, QPainterPath, QPalette, QPen, QShortcut)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLayout, QMainWindow, QMessageBox,
    QPushButton, QSlider, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from . import bands, ft8, logo, prefs, smeter, theme
from .memory import Channel, MemoryStore
from .radio import (TUNER_BANDWIDTHS, Radio, Settings, list_devices,
                    transient_open_error)

MODES = [
    ("AM", "am", 6000.0), ("SAM", "sam", 6000.0), ("USB", "usb", 2400.0),
    ("LSB", "lsb", 2400.0), ("CW", "cw", 500.0), ("NFM", "nfm", 12500.0),
    # FT8 sits with the other mode buttons because that is where a listener
    # looks for it, but it is not a demodulator: the receiver is put in USB
    # with a 3 kHz filter (see ft8.receiver_mode) and what makes the mode FT8
    # is the decoder behind it, in rspsdr/ft8.py.
    ("FT8", "ft8", ft8.FILTER_BW),
]
MODE_BW = {m[1]: m[2] for m in MODES}

STEPS = [("1 Hz", 1), ("10 Hz", 10), ("100 Hz", 100), ("1 kHz", 1000),
         ("2.5 kHz", 2500), ("5 kHz", 5000), ("9 kHz", 9000),
         ("10 kHz", 10000), ("12.5 kHz", 12500), ("25 kHz", 25000),
         ("100 kHz", 100000)]

BANDWIDTHS = [("500 Hz", 500.0), ("1.8 kHz", 1800.0), ("2.4 kHz", 2400.0),
              ("3 kHz", 3000.0), ("4 kHz", 4000.0), ("6 kHz", 6000.0),
              ("8 kHz", 8000.0), ("10 kHz", 10000.0), ("12.5 kHz", 12500.0),
              ("15 kHz", 15000.0)]

RATES = [("62.5 kSps", 62500), ("96 kSps", 96000), ("125 kSps", 125000),
         ("192 kSps", 192000), ("250 kSps", 250000), ("384 kSps", 384000),
         ("500 kSps", 500000), ("768 kSps", 768000), ("1 MSps", 1000000),
         ("1.536 MSps", 1536000), ("2 MSps", 2000000), ("2.048 MSps", 2048000),
         ("3 MSps", 3000000), ("4 MSps", 4000000), ("5 MSps", 5000000),
         ("6 MSps", 6000000), ("7 MSps", 7000000), ("8 MSps", 8000000),
         ("9 MSps", 9000000), ("10 MSps", 10000000)]

COLORMAP_STOPS = [
    (0.00, (0, 0, 0)), (0.14, (0, 0, 70)), (0.30, (0, 10, 170)),
    (0.45, (0, 90, 225)), (0.56, (0, 185, 200)), (0.66, (40, 225, 110)),
    (0.76, (175, 230, 55)), (0.86, (255, 195, 35)), (0.93, (255, 105, 20)),
    (1.00, (255, 250, 235)),
]

# Waterfall colour maps.  "spectrum" and "grey"/"hot" are built here so the
# feature cannot break if pyqtgraph's data files are missing; the rest come
# from pyqtgraph's bundled maps.
COLORMAPS = [
    ("Spectrum", "spectrum"), ("Viridis", "viridis"), ("Inferno", "inferno"),
    ("Plasma", "plasma"), ("Turbo", "turbo"), ("Cividis", "cividis"),
    ("CET-L9", "CET-L9"), ("Grey", "grey"), ("Hot metal", "hot"),
]

# Frequency magnification for the plots.  The receiver span is divided by this.
#
# The ladder stops at 256x, and where it stops is arithmetic rather than taste:
# the window is the receiver span over the zoom, and the FFT divides that span
# into bins, so the number of bins left across the window is n_fft / zoom.  The
# smallest transform the window offers is 1024 points, so even the deepest step
# has four bins to draw -- and a window one bin wide would be a single column
# stretched across the whole plot.  At the lowest sample rate that is 244 Hz
# across the window, a CW signal and its neighbours; at 2 MSps it is 7.8 kHz,
# which resolves the carriers inside one AM channel.
ZOOMS = [("1x", 1), ("2x", 2), ("4x", 4), ("8x", 8), ("16x", 16), ("32x", 32),
         ("64x", 64), ("128x", 128), ("256x", 256)]
ZOOM_MAX = ZOOMS[-1][1]                 # the ladder's deepest step

# FFT sizes on offer.  This is where the waterfall's resolution ultimately comes
# from: a bin is one span/n_fft of spectrum, and the waterfall shows the visible
# slice of those bins, so doubling the FFT halves the width of the finest thing
# that can be drawn -- and halves the number of bins the deepest zoom of the
# ladder above has left to draw.  It costs DSP time in proportion, which is why
# the top of this list only became affordable once the transform moved to
# scipy.fft (measured on an i5-4590T: a 16384-point transform of two million
# samples a second went from 5.2 ms to 2.1 ms a frame, so 32768 and 65536 now
# cost what 8192 and 16384 used to).  The two tables are tied together by that
# arithmetic and tests/test_units.py checks the tie.
FFTS = (1024, 2048, 4096, 8192, 16384, 32768, 65536)

# Width of the left axis, forced to be the same on both plots.
#
# The spectrum labels its left axis in dB and the waterfall draws no values on
# its, and left to itself that made the spectrum's plot area start ~57 px
# further right.  Two plots with different scales do not line up: a carrier
# drawn at one x in the spectrum sits at another x in the waterfall, and a
# click lands tens of kHz from the frequency the screen appears to point at
# (measured: 69 kHz at one edge of the span, 16 kHz at the other).  One shared
# width keeps the frequency-to-pixel mapping identical in both.
PLOT_AXIS_WIDTH = 62

# Waterfall history depth: one row per repaint, so this is how much of the
# past the picture remembers.  The picture is sized to the plot it is drawn in
# -- one row per pixel of plot height, one column per pixel of width, which is
# what SDR++ does and the only shape at which nothing is stretched by the
# rasteriser -- and these bound how far that can go.  WF_ROWS is both the
# starting size and the answer when the plot's height cannot be known yet; the
# bounds keep a very tall window from turning into millions of pixels a frame.
WF_ROWS = 240
WF_MIN_ROWS = 120
WF_MAX_ROWS = 600

# Displayed spectrum is max-pooled down to one point per pixel of the trace's
# own width, and to this many when that width is not known yet.  Max (not mean)
# keeps narrow carriers at full height while shortening the painted path.
SPECTRUM_POINTS = 2048

# Waterfall horizontal resolution: how many columns (frequency points) a row is
# built from.  The FFT decides how many bins exist; this decides how many of
# them survive into the picture.
#
# This used to be a hard 800, which quietly made the waterfall the coarsest
# display in the window: an 8192- or 16384-point FFT measured far more detail
# than the picture could show, and on a zoomed view the 800 columns were
# stretched back across the plot.  The default now follows the plot itself.
#
# "Screen" (0) matches a row to the plot's own pixel width, which is what SDR++
# does -- its waterfall row is dataWidth = widget width less the frequency
# scale.  The picture is then 1:1 with the display: no row is stretched or
# squeezed by the rasteriser, a narrow carrier lands on its own pixel instead of
# being averaged with its neighbours, and columns beyond that would be work no
# one can see.  The fixed sizes are the deliberate other half of the trade --
# more columns than the screen has pixels, which buys smoother decimation of a
# big FFT at a proportional cost per repaint.  Neither ever stretches a row
# beyond the bins behind it; a row is only ever pooled down towards the
# display.
SCREEN_DETAIL = 0
WF_DETAILS = [
    ("Screen", SCREEN_DETAIL), ("800", 800), ("1600", 1600),
    ("2400", 2400), ("3200", 3200), ("4800", 4800), ("6400", 6400),
]
WF_MAX_COLS = WF_DETAILS[-1][1]         # the widest picture on offer

# What a waterfall row holds before any data has gone into it.  Every plausible
# level window sits above this, so a cleared or newly added row is unmistakably
# black.  Filling them with the *previous* window's floor drew a bright band
# across the foot of the waterfall whenever the floor then moved down: the
# stale rows were suddenly above the new floor and lit up at full brightness.
WF_BLANK_DB = -200.0

# The window's smallest size.  Two floors, not one, because the controls below
# the picture are what decides it:
#
#   * with the pane folded away the window can be as small as it has always
#     been -- 780x560, the size that fits any screen this is likely to be run
#     on, so maximising and fitting both work;
#   * with the rows on show it cannot.  Qt does not say so: when the rows need
#     more height than the window has it squeezes the *picture* down to nothing
#     and then clips the rows at the bottom edge, so the zoom buttons sit below
#     the window with nothing on screen to explain it (measured: +/-/Fit 18 px
#     under the bottom edge at 780x560 with the pane open).  The floor is
#     therefore the rows' own height-for-width at the width the window has,
#     plus PLOT_MIN_HEIGHT for the picture.  See MainWindow._update_floor.
MIN_WIDTH = 780
FOLDED_MIN_HEIGHT = 560
PLOT_MIN_HEIGHT = 120

REPAINT_MS = 45

# How long after the last change the settings file is written.  A slider being
# dragged is one setting changing a hundred times; the file is small, but not
# that small, and nothing is gained by writing it while the pointer is down.
PREFS_SAVE_MS = 1500

# The squelch FT8 sets while it is listening: off, so that nothing subtracts
# from the audio the decoder is fed.  Remembered before it is applied, and put
# back when the mode is left (see _ft8_prepare).
FT8_SQUELCH_DB = -140.0

# --- the FT8 strip ------------------------------------------------------
# The decoder list is a table rather than a text block because every row is
# also a tuning button: the whole point of a decode is the frequency it was
# heard on.  Monospaced, left to right: UTC, dB, DT, audio offset, message.
FT8_COLS = [("UTC", 58), ("dB", 34), ("DT", 40), ("Audio", 46), ("Message", 0)]
FT8_ROWS = 6                    # visible decodes; the rest scroll off the top
FT8_MIN_ROWS = 3                # the strip yields height to the plots when the
                                # window is at its smallest
FT8_KEEP = 400                  # rows held before the oldest are dropped
FT8_ROW_PX = 16
FT8_READOUT_MS = 200            # how often the slot clock redraws


def make_colormap() -> pg.ColorMap:
    pos = [p for p, _ in COLORMAP_STOPS]
    cols = [QColor(r, g, b) for _, (r, g, b) in COLORMAP_STOPS]
    return pg.ColorMap(pos, cols)


def build_colormap(key: str, reverse: bool = False) -> pg.ColorMap:
    """Colour map by name, optionally reversed.

    ``pg.colormap.get`` returns a *cached, shared* object and ``reverse()``
    mutates it in place (returning ``None``), so reversing a cached map would
    silently reverse it for every later caller too.  Hence ``skipCache``, and
    hence not using the return value of ``reverse``.
    """
    if key == "spectrum":
        cm = make_colormap()
    elif key == "grey":
        cm = pg.ColorMap([0.0, 1.0], [QColor(0, 0, 0), QColor(255, 255, 255)])
    elif key == "hot":
        cm = pg.ColorMap([0.0, 0.35, 0.7, 1.0],
                         [QColor(0, 0, 0), QColor(150, 0, 0),
                          QColor(255, 150, 0), QColor(255, 255, 235)])
    else:
        try:
            cm = pg.colormap.get(key, skipCache=True)
        except Exception:
            cm = None
        if cm is None:
            cm = make_colormap()
    if reverse:
        cm.reverse()
    return cm


def colour_lut(key: str, reverse: bool = False) -> np.ndarray:
    """A colour map as the 256 RGBA bytes the waterfall is drawn through.

    The waterfall is kept as pixels, not as dB values, so that a repaint is a
    blit and the arithmetic happens once per row instead of once per pixel per
    frame.  That means the picture needs the colour map as bytes it can index,
    which is what this returns -- and it needs a *fixed* table, because ids
    into a table that changed underneath it would recolour the history.
    """
    cm = build_colormap(key, reverse)
    rgb = cm.getLookupTable(nPts=256, alpha=False)
    lut = np.empty((256, 4), dtype=np.uint8)
    lut[:, :3] = np.asarray(rgb, dtype=np.uint8)[:, :3]
    lut[:, 3] = 255
    return lut


def visible_bins(center: float, db: np.ndarray, span: float,
                 lo: float, hi: float):
    """The part of a full-span spectrum that falls inside [lo, hi] Hz.

    The plots display only this slice, so zooming in magnifies real data at
    full resolution instead of stretching a version already decimated to fit
    the whole span.
    """
    n = db.size
    step = span / n
    i0 = int(np.floor((lo - center) / step + n / 2.0))
    i1 = int(np.ceil((hi - center) / step + n / 2.0))
    i0 = max(0, min(i0, n - 1))
    i1 = max(i0 + 1, min(i1, n))
    return center + (np.arange(i0, i1) - n // 2) * step, db[i0:i1]


class FlowLayout(QLayout):
    """A row that wraps onto the next line when it runs out of width.

    A QHBoxLayout reports a minimum width equal to the sum of its children,
    and Qt treats that as a hard floor on the whole window.  With this many
    controls that floor was 1469 px, wider than a 1360 px screen, so the window
    could neither shrink nor be maximised properly.  Wrapping removes the
    floor: the worst case becomes one control per line, and the row reflows to
    whatever width it is given.
    """

    def __init__(self, parent=None, margin: int = 4, spacing: int = 6):
        super().__init__(parent)
        self._items: list = []
        self.setContentsMargins(margin, 0, margin, 0)
        self.setSpacing(spacing)

    def addItem(self, item):                    # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):                    # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):                    # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):              # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:        # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._place(QRect(0, 0, width, 0), dry=True)

    def setGeometry(self, rect: QRect):         # noqa: N802
        super().setGeometry(rect)
        self._place(rect, dry=False)

    def sizeHint(self) -> QSize:                # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:             # noqa: N802
        # Worst case is one control per line, so the floor is the widest single
        # control rather than the sum of them all.
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _place(self, rect: QRect, dry: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            w, h = hint.width(), hint.height()
            if x + w > area.right() + 1 and line > 0:
                x = area.x()
                y += line + self.spacing()
                line = 0
            if not dry:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += w + self.spacing()
            line = max(line, h)
        return y + line + m.bottom() - rect.y()


def mono_font(size: int, bold: bool = False) -> QFont:
    f = QFont("DejaVu Sans Mono")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSize(size)
    f.setBold(bold)
    return f


def two_tick_levels(plot: pg.PlotWidget):
    """Give a plot two levels of ticks instead of pyqtgraph's three.

    With the grid on, pyqtgraph extends *every* tick of *every* level right
    across the view, so a third level is a few hundred more lines drawn into
    the picture each repaint for labels nobody reads.  Two levels -- the major
    ticks and one subdivision -- is what an SDR's frequency scale looks like
    anyway, and it takes a repaint from 20.5 ms to 14.4 ms on the machine this
    is tuned for.
    """
    for name in ("bottom", "left", "top", "right"):
        plot.getAxis(name).setStyle(maxTickLevel=1, maxTextLevel=1)


def apply_theme(app: QApplication, name: str) -> dict:
    """Put a colour scheme on the whole application, and hand it back.

    Returned as well as installed, because a scheme covers two different kinds
    of thing: what Qt paints from the palette and the style sheet below, and
    what this module paints by hand -- the plots' pens and backgrounds, the
    passband wash, and the labels whose colour is not the ordinary text one
    (see ``MainWindow._recolour``).

    Fusion is set here rather than left to the desktop.  A GTK, a Windows and
    a macOS style each draw their widgets from their own images and take only
    part of a palette, so one scheme came out as three different windows; of
    the styles Qt ships, Fusion is the one that follows the table on every
    platform.  Setting it does not change what the window can do -- the
    layouts, the wrapping rows and the plots are the same widgets either way.
    """
    pal = theme.palette(name)
    role = QPalette.ColorRole
    colours = (
        ("window", role.Window), ("text", role.WindowText),
        ("field", role.Base), ("panel", role.AlternateBase),
        ("text", role.Text), ("panel", role.Button),
        ("text", role.ButtonText), ("bright", role.BrightText),
        ("highlight", role.Highlight), ("highlight_text", role.HighlightedText),
        ("panel", role.ToolTipBase), ("text", role.ToolTipText),
        ("muted", role.PlaceholderText), ("border", role.Mid),
        ("border", role.Dark), ("border", role.Light),
        ("border", role.Shadow), ("accent", role.Link),
    )
    palette = QPalette()
    for key, which in colours:
        palette.setColor(which, QColor(pal[key]))
    # Greyed-out text is a shade of the theme rather than the platform's idea
    # of grey, which on a light scheme is unreadable on the panel behind it.
    muted = QColor(pal["muted"])
    for which in (role.Text, role.ButtonText, role.WindowText,
                  role.PlaceholderText):
        palette.setColor(QPalette.ColorGroup.Disabled, which, muted)

    app.setStyle("Fusion")
    app.setPalette(palette)
    app.setStyleSheet(theme.stylesheet(pal))
    return pal


class FastImage(pg.ImageItem):
    """An image item that smooths its own scaling whatever the plots do.

    The window runs with antialiasing off (see the note on ``setConfigOptions``
    at the top of this module), and Qt takes SmoothPixmapTransform from the same
    place, so an image that has to be scaled -- any fixed detail setting, a plot
    whose height has not caught up with its pixel count -- would be enlarged
    with nearest-neighbour and look like blocks.  Interpolation is cheap for an
    image and is exactly what SDR++ gets from its linear texture filtering, so
    this item asks for it directly rather than enabling it for every line in the
    window.
    """

    def paint(self, p, *args):
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        super().paint(p, *args)


class SpectrumPlot(pg.PlotWidget):
    """Spectrum with click/drag tuning."""

    tuned = pyqtSignal(float)
    recentre = pyqtSignal(float)
    stepped = pyqtSignal(int)
    cursor = pyqtSignal(float)
    settled = pyqtSignal(float)          # see WaterfallPlot.settled

    def __init__(self):
        super().__init__()
        self.setBackground("#0a0c10")
        self.setMouseEnabled(x=False, y=False)
        self.hideButtons()
        self.showGrid(x=True, y=True, alpha=0.18)
        self.setLabel("left", "dB")
        self.setLabel("bottom", "MHz")
        self.getAxis("left").setWidth(PLOT_AXIS_WIDTH)
        two_tick_levels(self)
        self.setYRange(-120.0, 0.0)

        self.curve = self.plot([], [], pen=pg.mkPen("#7fe7ff", width=1.2))
        self.peak_curve = self.plot([], [], pen=pg.mkPen("#ffd166", width=1.0))
        self.peak: np.ndarray | None = None

        self.band = pg.LinearRegionItem(brush=pg.mkBrush(40, 200, 120, 40),
                                        movable=False)
        self.band.setZValue(-5)
        self.addItem(self.band)
        self.band.setVisible(False)

        self.marker = pg.InfiniteLine(angle=90,
                                      pen=pg.mkPen("#ff5f6d", width=1.6))
        self.marker.setZValue(10)
        self.addItem(self.marker)

        self._freqs = None
        self._center = 0.0
        self._span = 1.0
        self._dragging = False
        # The pixel-to-frequency mapping frozen for the duration of a drag.
        self._drag_map = None
        self._y_lo, self._y_hi = -130.0, -20.0
        self._y_applied = (-130.0, -20.0)
        self._x_applied = (0.0, 0.0)

    def _screen_points(self) -> int:
        """One plotted point per pixel of the trace's own width.

        More points than pixels is work the rasteriser cannot show; fewer is a
        coarser trace than the screen could draw.  SPECTRUM_POINTS stands in
        until the widget has been laid out.
        """
        px = int(round(self.plotItem.vb.width() * self.devicePixelRatioF()))
        return px if px > 0 else SPECTRUM_POINTS

    def update_spectrum(self, center: float, db: np.ndarray, span: float,
                        lo: float, hi: float):
        self._center, self._span = center, span
        # The full-resolution slice is kept for the mouse: _freq_at maps a pixel
        # back through it, so a click lands on a real bin even though the trace
        # itself is painted one point per pixel.
        self._freqs, vis = visible_bins(center, db, span, lo, hi)
        x = self._freqs / 1e6
        points = self._screen_points()
        if vis.size > points:
            # Even groups again, so the trace reaches the right-hand edge of the
            # span instead of stopping at the last whole block of bins, and each
            # point sits at the middle of the bins it stands for.
            edges = (np.arange(points + 1) * vis.size) // points
            vis = np.maximum.reduceat(vis, edges[:-1])
            x = 0.5 * (x[edges[:-1]] + x[edges[1:] - 1])
        self.curve.setData(x, vis)
        if self.peak is None or self.peak.size != vis.size:
            self.peak = vis.copy()
        else:
            np.maximum(self.peak * 0.86, vis, out=self.peak)
        self.peak_curve.setData(x, self.peak)

        # Auto-scale the vertical axis to the data, smoothed so the trace does
        # not jump around when signals come and go.
        floor = float(np.percentile(vis, 3.0))
        top = float(np.max(self.peak))
        ylo, yhi = floor - 6.0, max(top + 6.0, floor + 25.0)
        self._y_lo += 0.12 * (ylo - self._y_lo)
        self._y_hi += 0.12 * (yhi - self._y_hi)
        if abs(self._y_lo - self._y_applied[0]) > 0.5 or \
                abs(self._y_hi - self._y_applied[1]) > 0.5:
            self._y_applied = (self._y_lo, self._y_hi)
            self.setYRange(self._y_lo, self._y_hi, padding=0)

    def set_marker(self, freq: float, bw: float, mode: str = "am",
                   pitch: float = 700.0):
        """Show the dial and the passband the demodulator is really using.

        The band is drawn on the correct side of the dial, because a symmetric
        region is the *AM* shape: drawn for every mode it made USB, LSB and CW
        indistinguishable from AM on screen.  USB keeps the upper sideband, LSB
        the lower, and CW listens on the dial plus its pitch offset.
        """
        f = freq / 1e6
        self.marker.setValue(f)
        half = 0.5 * bw / 1e6
        m = mode.lower()
        if m == "usb":
            lo, hi = f, f + 2.0 * half
        elif m == "lsb":
            lo, hi = f - 2.0 * half, f
        elif m == "cw":
            centre = f + pitch / 1e6
            lo, hi = centre - half, centre + half
        else:                       # am, sam, nfm -- symmetric about the dial
            lo, hi = f - half, f + half
        self.band.setRegion((lo, hi))
        self.band.setVisible(True)

    def set_view(self, lo: float, hi: float):
        """Show exactly [lo, hi] Hz -- narrowed by the zoom control."""
        lim = (lo / 1e6, hi / 1e6)
        if lim != self._x_applied:
            self._x_applied = lim
            self.setXRange(lim[0], lim[1], padding=0)

    def _axis_px_map(self):
        """(frequency Hz at x=0, Hz per pixel) along the axis as drawn.

        Sampled from the plot's own transform -- what the frequency scale under
        the picture is drawn from -- at the two edges of the plotting area and
        with a y inside it.  Reconstructing it from the axis range and the
        widget's rectangle instead disagreed with the drawn scale by about a
        pixel (half a bin at a 4096-point FFT), and a point outside the box can
        be mapped through a degenerate axis and come back not-a-number, which
        would silently turn a click into no tune at all.
        """
        vb = self.plotItem.vb
        r = vb.sceneBoundingRect()
        if r.width() <= 0.0:
            return None
        y = r.center().y()
        f0 = vb.mapSceneToView(QPointF(r.left(), y)).x() * 1e6
        f1 = vb.mapSceneToView(QPointF(r.right(), y)).x() * 1e6
        if not f0 == f0 or not f1 > f0:                 # NaN, or no span
            return None
        per_px = (f1 - f0) / r.width()
        return (f0 - r.left() * per_px, per_px)

    def _freq_at(self, pos, mapping=None) -> float | None:
        """The frequency under a pixel, snapped to the nearest bin shown.

        While a drag is in progress the caller passes the mapping frozen at the
        moment the button went down, so nothing that repaints during the
        gesture -- a re-centre commanded just before it, a resize, the window
        following the dial -- can change the frequency under the pointer.
        """
        if self._freqs is None or self._freqs.size < 2:
            return None
        m = mapping if mapping is not None else self._axis_px_map()
        if m is None:
            return None
        f0, per_px = m
        step = self._freqs[1] - self._freqs[0]
        k = int(round((f0 + pos.x() * per_px - self._freqs[0]) / step))
        k = max(0, min(k, self._freqs.size - 1))
        return float(self._freqs[k])

    def mousePressEvent(self, ev):
        m = self._axis_px_map()
        if ev.button() == Qt.MouseButton.LeftButton:
            f = self._freq_at(ev.position(), m)
            if f is not None:
                self._dragging = True
                self._drag_map = m
                self.tuned.emit(f)
                ev.accept()
                return
        elif ev.button() == Qt.MouseButton.RightButton:
            f = self._freq_at(ev.position(), m)
            if f is not None:
                self.recentre.emit(f)
                ev.accept()
                return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if self._dragging:
            f = self._freq_at(pos, self._drag_map)
            if f is not None:
                self.tuned.emit(f)
            ev.accept()
            return
        f = self._freq_at(pos)
        if f is not None:
            self.cursor.emit(f)
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        was, mapping = self._dragging, self._drag_map
        self._dragging, self._drag_map = False, None
        if was:
            f = self._freq_at(ev.position(), mapping)
            if f is not None:
                self.settled.emit(f)
        super().mouseReleaseEvent(ev)

    def wheelEvent(self, ev):
        self.stepped.emit(1 if ev.angleDelta().y() > 0 else -1)
        ev.accept()


class WaterfallPlot(pg.PlotWidget):
    """Rolling waterfall driven by the same spectrum frames."""

    tuned = pyqtSignal(float)
    # +1 / -1 from the wheel: the waterfall zooms rather than retunes, because
    # a wheel over the *spectrum* already steps the tuning and the two would
    # otherwise be indistinguishable to a user who is looking at the picture.
    zoom = pyqtSignal(int)
    # A pointer gesture finished, at this frequency.  The window is allowed to
    # follow the dial only here: a recentre while the pointer is down moves the
    # picture out from under it, so the frequency under the cursor jumps by up
    # to half a window and a drag tunes in steps instead of tracking.
    settled = pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self._laid_out = False
        self.setBackground("#0a0c10")
        self.setMouseEnabled(x=False, y=False)
        self.hideButtons()
        self.setLabel("bottom", "MHz")
        self.getAxis("left").setStyle(showValues=False)
        # Same width as the spectrum's, not merely hidden values: what matters
        # is that both plot areas start and end at the same x.
        self.getAxis("left").setWidth(PLOT_AXIS_WIDTH)
        two_tick_levels(self)
        self.img = FastImage(axisOrder="row-major")
        self.addItem(self.img)
        self.invertY(True)          # newest line at the top

        # Two copies of the same history.  `_wf` is the truth: the dB rows the
        # arithmetic is done on, and what a floor, contrast or colour change is
        # applied to.  `_rgba` is what the plot draws.  Colourising a whole
        # picture is far too expensive to redo every frame (measured: 21 ms for
        # a 240x6400 frame, against 0.1 ms for the one row that is new), so a
        # frame colourises its new row and shifts the rest, and only a control
        # change rebuilds the whole thing.  SDR++ splits it the same way, with
        # rawFFTs beside its rendered framebuffer and updateWaterfallFb() to
        # rebuild one from the other.
        self._rows = WF_ROWS
        self._cols = 64
        self._wf = np.full((self._rows, self._cols), WF_BLANK_DB,
                           dtype=np.float32)
        self._lut = colour_lut("spectrum")
        self._rgba = np.zeros((self._rows, self._cols, 4), dtype=np.uint8)
        self._rgba[..., 3] = 255
        self._vis_bins = 64            # frequency points the last frame offered
        self._detail = SCREEN_DETAIL   # 0 = Screen: follow the plot's pixels
        self._lo, self._hi = -120.0, -20.0
        self._auto = True
        self._contrast = 30.0
        self._manual_lo, self._manual_hi = -110.0, -40.0
        self._floor = None
        self._cmap_key, self._cmap_rev = "spectrum", False
        self._lo_freq, self._hi_freq = 0.0, 1.0
        self._x_applied = (0.0, 0.0)
        self._rect_applied = None
        self._dragging = False
        # The pixel-to-frequency mapping frozen for the duration of a drag.
        self._drag_map = None
        self.img.setImage(self._rgba, autoLevels=False, levels=None)
        self._laid_out = True

    # ---------------------------------------------------------- geometry
    def _screen_cols(self) -> int:
        """Columns that map onto the plot's own pixels (SDR++'s dataWidth)."""
        px = int(round(self.plotItem.vb.width() * self.devicePixelRatioF()))
        if px <= 0:                     # nothing has been laid out yet
            px = max(1, (self.width() or 0) - PLOT_AXIS_WIDTH)
        return px

    def _screen_rows(self) -> int:
        """Rows that map onto the plot's own pixels -- one line per pixel."""
        px = int(round(self.plotItem.vb.height() * self.devicePixelRatioF()))
        if px <= 0:
            return WF_ROWS
        return max(WF_MIN_ROWS, min(px, WF_MAX_ROWS))

    def resizeEvent(self, ev):                 # noqa: N802
        """Re-fit the history: the picture is as big as the plot is.

        Both dimensions follow the widget, exactly as SDR++ re-derives its
        row width from the widget size on every resize.  Rows change whatever
        the detail setting says -- one line per pixel of height is a property
        of the display, not a resolution choice -- while columns only follow the
        plot when the detail control is on "Screen".
        """
        super().resizeEvent(ev)
        # pyqtgraph calls this from its own constructor, before this object has
        # any state at all -- and PlotWidget's __getattr__ raises for a missing
        # attribute rather than answering with a default, so the flag is looked
        # up defensively.
        if getattr(self, "_laid_out", False):
            self._fit_to_plot(self._vis_bins)

    def _target_cols(self, bins: int) -> int:
        """How wide a row should be built from ``bins`` frequency points.

        Screen keeps what the plot can show; an explicit size asks for more,
        which is a deliberate trade of CPU for a smoother decimation.  Never
        wider than the data, so a request for more columns than there are bins
        leaves the picture alone instead of stretching it.
        """
        return max(1, min(int(bins), int(self._detail) or self._screen_cols()))

    def _fit_to_plot(self, bins: int):
        """Give the history the shape the plot is asking for."""
        self._resize_image(self._target_cols(bins), self._screen_rows())

    # Named _resize_image, not _resize: QWidget.resize already exists and
    # shadowing it in Python is a trap for later edits.
    def _resize_image(self, cols: int, rows: int | None = None):
        """Rebuild the history at a new shape, keeping what it already holds.

        Only the resolution changes here, never the span the rows were taken
        over, so the history is still meaningful and is resampled onto the new
        shape rather than thrown away -- a fresh image would blank the picture
        every time the detail control moved or the window was resized.
        Nearest column, not block-averaged: these rows are a picture, and
        averaging would soften exactly the narrow carriers that the extra
        detail exists to show.  Rows that did not exist before are blank rather
        than a copy of their neighbour, because inventing history is worse than
        showing none, and the newest row keeps its place at the top.
        """
        cols = max(1, int(cols))
        rows = max(1, int(rows if rows is not None else self._rows))
        if cols == self._cols and rows == self._rows:
            return
        old = self._wf
        self._wf = np.full((rows, cols), WF_BLANK_DB, dtype=np.float32)
        if old.size:
            idx = np.rint(np.linspace(0, old.shape[1] - 1, cols)).astype(int)
            keep = min(rows, old.shape[0])
            self._wf[:keep] = old[:keep][:, idx]
        self._rgba = np.zeros((rows, cols, 4), dtype=np.uint8)
        self._rgba[..., 3] = 255
        self._rows, self._cols = rows, cols
        self._repaint_history()
        # Hand the new array over *before* placing it.  setRect scales by the
        # image the item already holds -- pyqtgraph's own warning is that it
        # "cannot be used before an image is assigned" -- so a rect applied
        # first was built from the *previous* column count and the picture was
        # drawn at the wrong horizontal scale: 37.6 MHz of span over a 2 MHz
        # window on the very first frame, and the width ratio out again after
        # any resize.  The axis and the click mapping stayed correct, so what a
        # user saw was a waterfall that tuned to the wrong frequency.
        self._rect_applied = None
        self.img.setImage(self._rgba, autoLevels=False, levels=None)
        self._apply_rect()

    # ------------------------------------------------------------ colour
    def _quantise(self, db: np.ndarray, out: np.ndarray):
        """Turn a block of dB rows into the RGBA pixels for them.

        This is the whole reason the picture is stored as pixels: the levels
        are baked in here, once, instead of being applied at paint time to
        every pixel of every frame.  ``np.take`` writes straight into the
        framebuffer, so the common case -- one new row -- does not allocate.
        """
        lo, hi = self._lo, self._hi
        x = (db - lo) * (255.0 / max(hi - lo, 1e-6))
        blank = db <= WF_BLANK_DB
        np.clip(x, 0.0, 255.0, out=x)
        np.take(self._lut, x.astype(np.uint8), axis=0, out=out)
        # A row with no data in it yet is drawn black, not in the colour the
        # bottom of the level window happens to have.  The two are the same for
        # an unreversed map whose first entry is black, but a reversed one has
        # its brightest colour there, and that drew a band of full-brightness
        # pixels wherever the history had not filled in.
        if blank.any():
            out[blank] = (0, 0, 0, 255)

    def _repaint_history(self):
        """Re-colour every stored row: what a level or colour change needs."""
        self._quantise(self._wf, self._rgba)

    def set_detail(self, cols: int):
        """Column limit for a row: 0 means the plot's own width."""
        self._detail = max(0, int(cols))
        # Take effect now, not at the next sweep: a stopped radio never calls
        # push(), and a picture control that appears to do nothing is broken.
        self._fit_to_plot(self._vis_bins)

    def cols(self) -> int:
        """Columns in the picture right now -- what the detail control bought."""
        return self._cols

    def rows(self) -> int:
        """Rows of history the picture holds -- one per pixel of plot height."""
        return self._rows

    def bins(self) -> int:
        """Frequency points the last frame offered, before any pooling."""
        return self._vis_bins

    # Deliberately NOT called clear(): PlotWidget copies plotItem's methods
    # (including 'clear') onto itself as instance attributes, so a method named
    # clear() on this class is shadowed and PlotItem.clear() runs instead --
    # which removes every item from the plot, ImageItem included, blanking the
    # rendered waterfall for good while the data behind it still looks fine.
    def clear_history(self):
        """Blank the history -- the levels no longer describe the old rows."""
        self._wf[:] = WF_BLANK_DB
        self._floor = None
        self._repaint_history()
        self.img.setImage(self._rgba, autoLevels=False, levels=None)

    # ---- levels
    def _levels_now(self) -> tuple[float, float]:
        """The level window the current controls ask for."""
        if self._auto:
            floor = self._floor if self._floor is not None else -110.0
            lo = floor + 3.0
            return lo, lo + max(self._contrast, 1.0)
        # A zero-width window would be degenerate: keep at least 1 dB.
        return self._manual_lo, max(self._manual_hi, self._manual_lo + 1.0)

    def push(self, center: float, db: np.ndarray, span: float,
             lo: float, hi: float):
        self._lo_freq, self._hi_freq = lo, hi
        _, vis = visible_bins(center, db, span, lo, hi)
        self._vis_bins = int(vis.size)
        self._fit_to_plot(vis.size)
        row = self._pool(vis, self._cols)
        self._wf[1:] = self._wf[:-1]
        self._wf[0] = row
        if self._auto:
            # Track the noise floor rather than the signal level, otherwise the
            # whole picture darkens whenever a strong carrier appears.  A
            # percentile is a sort; a strided sample of the row is enough to
            # find the floor and costs a fraction as much at 6400 columns.
            f = float(np.percentile(row[::4] if row.size > 256 else row, 15.0))
            self._floor = f if self._floor is None else \
                0.9 * self._floor + 0.1 * f
        self._lo, self._hi = self._levels_now()
        self._rgba[1:] = self._rgba[:-1]
        self._quantise(row, self._rgba[0])
        # Image first, then its rectangle -- see _resize_image.  The shape does
        # not change here, so this is belt and braces on the same trap.
        self.img.setImage(self._rgba, autoLevels=False, levels=None)
        self._apply_rect()

    @staticmethod
    def _pool(vis: np.ndarray, cols: int) -> np.ndarray:
        """Reduce a line of dB values to ``cols`` columns, keeping peaks.

        Maximum, not mean, which is what SDR++'s ``doZoom`` does when it has
        more bins than pixels: a narrow carrier that falls between two columns
        is averaged into the noise by a mean and keeps its full strength under
        a maximum.  That is the difference between a waterfall that shows a
        weak signal as a faint smudge and one that shows it as a line, and it
        is why the picture can be pooled down to the screen without losing the
        thing the user is looking at.
        """
        if vis.size <= cols:
            return vis
        # Every bin has to end up in a column, and the columns have to be as
        # even as the bin count allows.  Grouping the first k*cols bins and
        # dropping the remainder -- which is what this did -- drew a row
        # covering 97 % of the span into a rectangle that claimed 100 % of it,
        # so the whole picture was stretched and every frequency sat to the left
        # of where the axis said it was: at 2 MSps with 16384 bins and a 1324 px
        # plot that was 20 px in the middle of the window and 57 kHz at its
        # edge.  reduceat over an even split of the bins closes the gap.
        edges = (np.arange(cols + 1) * vis.size) // cols
        return np.maximum.reduceat(vis, edges[:-1])

    def _apply_rect(self):
        """Place the image over the visible span -- but only when it moved."""
        rect = (self._lo_freq / 1e6, 0.0,
                (self._hi_freq - self._lo_freq) / 1e6, float(self._rows))
        if rect != self._rect_applied:
            self._rect_applied = rect
            self.img.setRect(pg.QtCore.QRectF(*rect))

    def levels(self) -> tuple[float, float]:
        return self._lo, self._hi

    def set_levels(self, auto: bool, contrast: float,
                   floor: float, ceiling: float):
        self._auto = bool(auto)
        self._contrast = float(contrast)
        self._manual_lo, self._manual_hi = float(floor), float(ceiling)
        lo, hi = self._levels_now()
        if (lo, hi) == (self._lo, self._hi):
            return
        # Take effect now, not at the next sweep of the waterfall: the levels
        # are baked into the pixels, so they have to be reapplied to the rows
        # already on screen or the spin boxes and the picture disagree.
        self._lo, self._hi = lo, hi
        self._repaint_history()
        self.img.setImage(self._rgba, autoLevels=False, levels=None)

    def set_colormap(self, key: str, reverse: bool = False):
        self._cmap_key, self._cmap_rev = key, bool(reverse)
        self._lut = colour_lut(key, reverse)
        self._repaint_history()
        self.img.setImage(self._rgba, autoLevels=False, levels=None)

    def set_view(self, lo: float, hi: float):
        lim = (lo / 1e6, hi / 1e6)
        if lim != self._x_applied:
            self._x_applied = lim
            self.setXRange(lim[0], lim[1], padding=0)
            self.setYRange(0, self._rows, padding=0)

    def _axis_px_map(self):
        """(frequency Hz at x=0, Hz per pixel) along the axis as drawn.

        Sampled from the plot's own transform, at the two edges of the plotting
        area and with a y inside it, for the reasons spelled out on
        :meth:`SpectrumPlot._axis_px_map`.
        """
        vb = self.plotItem.vb
        r = vb.sceneBoundingRect()
        if r.width() <= 0.0:
            return None
        y = r.center().y()
        f0 = vb.mapSceneToView(QPointF(r.left(), y)).x() * 1e6
        f1 = vb.mapSceneToView(QPointF(r.right(), y)).x() * 1e6
        if not f0 == f0 or not f1 > f0:                 # NaN, or no span
            return None
        per_px = (f1 - f0) / r.width()
        return (f0 - r.left() * per_px, per_px)

    def _freq_at(self, pos, mapping=None) -> float | None:
        """The frequency under a pixel, clamped to the window on screen.

        While a drag is in progress the caller passes the mapping frozen at the
        moment the button went down.  Nothing that repaints during a gesture --
        a re-centre commanded just before it, a resize, the window following the
        dial -- may then change the frequency under the pointer, which is what
        made a drag feel like it tuned in steps instead of following the hand.
        """
        m = mapping if mapping is not None else self._axis_px_map()
        if m is None or self._hi_freq <= self._lo_freq:
            return None
        f0, per_px = m
        f = f0 + pos.x() * per_px
        return float(min(max(f, self._lo_freq), self._hi_freq))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            m = self._axis_px_map()
            f = self._freq_at(ev.position(), m)
            if f is not None:
                self._dragging = True
                self._drag_map = m
                self.tuned.emit(f)
                ev.accept()
                return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._dragging:
            f = self._freq_at(ev.position(), self._drag_map)
            if f is not None:
                self.tuned.emit(f)
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        was, mapping = self._dragging, self._drag_map
        self._dragging, self._drag_map = False, None
        if was:
            f = self._freq_at(ev.position(), mapping)
            if f is not None:
                self.settled.emit(f)
        super().mouseReleaseEvent(ev)

    def wheelEvent(self, ev):
        # Accept the event: left to pyqtgraph the view would scroll its own
        # range, which the next repaint immediately overwrites, so the picture
        # would twitch without ever zooming.
        self.zoom.emit(1 if ev.angleDelta().y() > 0 else -1)
        ev.accept()


class Bridge(QObject):
    """Marshals radio callbacks from the DSP thread onto the GUI thread."""

    spectrum = pyqtSignal(float, object, float)
    status = pyqtSignal(object)
    error = pyqtSignal(str)
    lost = pyqtSignal(str)          # the stream died and the device needs reopening
    ft8_decodes = pyqtSignal(object, object)   # list[ft8.Decode], slot datetime
    ft8_status = pyqtSignal(str)
    ft8_error = pyqtSignal(str)


# ---------------------------------------------------------------- the meter
#
# Drawing the analogue signal meter.  The scale (IARU S units), the dial's
# geometry and the needle's ballistics all live in rspsdr/smeter.py; what is
# here is the picture, and the level it is fed.


class SMeter(QWidget):
    """An analogue S-meter: a needle over a printed dial.

    Fed one sample of the demodulator's passband level at a time, along with
    how long it has been since the last one, so that the needle's rise and fall
    are the recommended quasi-peak ones rather than whatever the repaint rate
    happens to be.

    Everything it draws is also available as numbers -- :meth:`needle`,
    :meth:`peak_needle`, :meth:`reading_text` -- because a meter that is only
    ever checked by looking at it has not been checked.
    """

    def __init__(self, ref_dbfs: float | None = None, face: str = "dark",
                 peak: bool = True, parent: QWidget | None = None):
        super().__init__(parent)
        self.dial = smeter.Dial()
        self.ball = smeter.Ballistics()
        self.ref_dbfs = float(smeter.DEFAULT_REF_DBFS if ref_dbfs is None
                              else ref_dbfs)
        self.face = face if face in smeter.FACES else "dark"
        self.peak_on = bool(peak)
        self.level_dbfs = -200.0        # the passband level, as last measured
        self.dbm = smeter.DIAL_LO_DBM   # what the needle is showing
        self.peak_dbm = smeter.DIAL_LO_DBM
        self.setFixedSize(smeter.WIDTH, smeter.HEIGHT)
        self.setToolTip(
            "Signal strength on the IARU scale: S9 is -73 dBm (50 uV into "
            "50 ohms), each S unit below it is 6 dB, and above it the steps "
            "are 10 dB.\n"
            "Measured in the passband being listened to, before the audio AGC "
            "and the volume, so it does not follow the volume control.\n"
            "The needle rises in ~10 ms and falls with a 500 ms time constant, "
            "which is the recommended quasi-peak response; the yellow marker "
            "holds the highest level for half a second.\n"
            "“S9 at” is the level that reads S9 -- set it so the meter agrees "
            "with a station or signal generator of known strength.")

    # -- the level ----------------------------------------------------
    def feed(self, dbfs: float, dt: float) -> None:
        """One passband level in dBFS, and the seconds since the last one."""
        self.level_dbfs = float(dbfs)
        value, peak = self.ball.update(smeter.dbm_for_dbfs(dbfs, self.ref_dbfs),
                                       dt)
        self.dbm = self._clamp(value)
        self.peak_dbm = self._clamp(peak)
        self.update()

    def reset(self) -> None:
        """Back to rest, as though the radio had only just been switched on."""
        self.ball.reset()
        self.level_dbfs = -200.0
        self.dbm = self.peak_dbm = smeter.DIAL_LO_DBM
        self.update()

    def set_ref(self, ref_dbfs: float) -> None:
        """Say which level reads S9, and start the needle again from rest."""
        self.ref_dbfs = float(ref_dbfs)
        self.ball.reset()
        self.update()

    def set_face(self, name: str) -> None:
        self.face = name if name in smeter.FACES else "dark"
        self.update()

    def set_peak(self, on: bool) -> None:
        self.peak_on = bool(on)
        self.update()

    @staticmethod
    def _clamp(dbm: float) -> float:
        return max(smeter.DIAL_LO_DBM, min(float(dbm), smeter.DIAL_HI_DBM))

    # -- what is drawn, as numbers ------------------------------------
    @property
    def palette(self) -> dict:
        return smeter.FACES[self.face]

    def needle(self) -> tuple[float, float, float, float]:
        """The needle as a line in the widget's own coordinates."""
        return smeter.needle_geometry(self.width(), self.height(),
                                      self.dial.angle(self.dbm))

    def peak_needle(self) -> tuple[float, float, float, float] | None:
        """The peak-hold marker, or None when the peak is switched off."""
        if not self.peak_on:
            return None
        return smeter.needle_geometry(self.width(), self.height(),
                                      self.dial.angle(self.peak_dbm), 0.94)

    def level_dbm(self) -> float:
        """The measured level in dBm -- the exact figure, not the needle's."""
        return smeter.dbm_for_dbfs(self.level_dbfs, self.ref_dbfs)

    def reading_text(self) -> str:
        """What the face says, which is the level rather than the needle."""
        if self.level_dbfs <= -190.0:
            return "--"                  # nothing measured: no radio running
        return smeter.reading(self.level_dbm())

    # -- painting -----------------------------------------------------
    def paintEvent(self, _ev):
        p = QPainter(self)
        try:
            self._paint(p)
        finally:
            p.end()

    def _paint(self, p: QPainter) -> None:
        # Antialiasing on here, unlike the plots: this is a few hundred pixels
        # of thin lines on a small widget, where a stair-stepped needle is the
        # first thing anyone would notice, and the cost is a fraction of a
        # millisecond.
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        col = self.palette
        w, h = float(self.width()), float(self.height())
        glass = QPainterPath()
        glass.addRoundedRect(QRectF(1.0, 1.0, w - 2.0, h - 2.0), 6.0, 6.0)

        sheen = QLinearGradient(0.0, 0.0, 0.0, h)
        sheen.setColorAt(0.0, QColor(col["face2"]))
        sheen.setColorAt(0.5, QColor(col["face"]))
        sheen.setColorAt(1.0, QColor(col["face"]))
        p.fillPath(glass, QBrush(sheen))
        bezel = QPen(QColor(col["bezel"]))
        bezel.setWidthF(1.4)
        p.setPen(bezel)
        p.drawPath(glass)

        p.save()
        p.setClipPath(glass)            # nothing is drawn outside the glass
        self._paint_dial(p, w, h)
        peak = self.peak_needle()
        if peak is not None:
            self._paint_needle(p, peak, col["peak"], 1.5)
        self._paint_needle(p, self.needle(), col["needle"], 1.8,
                           col["needle_edge"])
        p.restore()

        # The caption and the exact reading, along the foot of the face: the
        # needle is the instrument, the text is the measurement.
        p.setFont(mono_font(7))
        band = QRectF(7.0, h - 15.0, w - 14.0, 11.0)
        p.setPen(QPen(QColor(col["caption"])))
        p.drawText(band, int(Qt.AlignmentFlag.AlignLeft
                            | Qt.AlignmentFlag.AlignVCenter), "S-METER")
        p.setPen(QPen(QColor(col["number"])))
        p.drawText(band, int(Qt.AlignmentFlag.AlignRight
                            | Qt.AlignmentFlag.AlignVCenter),
                   self.reading_text())

    def _paint_dial(self, p: QPainter, w: float, h: float) -> None:
        """The arc: its ticks, and the numbers beside the long ones."""
        col = self.palette
        p.setFont(mono_font(7))
        for i, (_dbm, label, major) in enumerate(self.dial.marks):
            angle = self.dial.mark_angle(i)
            inner = smeter.needle_geometry(w, h, angle, self._frac(6.0, h))
            outer = smeter.needle_geometry(w, h, angle, 1.0)
            pen = QPen(QColor(col["major"] if major else col["minor"]))
            pen.setWidthF(1.4 if major else 0.8)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(outer[2], outer[3]), QPointF(inner[2], inner[3]))
            if label:
                spot = smeter.needle_geometry(w, h, angle, self._frac(18.0, h))
                p.setPen(QPen(QColor(col["number"])))
                p.drawText(QRectF(spot[2] - 13.0, spot[3] - 5.0, 26.0, 11.0),
                           int(Qt.AlignmentFlag.AlignCenter), label)

    @staticmethod
    def _frac(short_by: float, h: float) -> float:
        """Radius fraction that sits ``short_by`` pixels inside the arc."""
        pivot_y = h * (1.0 + smeter.PIVOT_DROP)
        full = pivot_y - 0.14 * h
        return max(0.05, (full - short_by) / full)

    @classmethod
    def _paint_needle(cls, p: QPainter, line, colour: str, width: float,
                      edge: str | None = None) -> None:
        x1, y1, x2, y2 = line
        if edge is not None:
            outline = QPen(QColor(edge))
            outline.setWidthF(width + 1.6)
            outline.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(outline)
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        pen = QPen(QColor(colour))
        pen.setWidthF(width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))


# Opening the radio, and not giving up when the device is not free yet.
#
# The SDRplay front end serves one client at a time, so while another program
# holds it -- SDRuno, SoapySDRUtil, a second copy of this one -- open() fails at
# once with "no available RSP devices found".  Nothing shows that beforehand:
# enumerate() returns an empty list while the device is held, which is why the
# window used to say "no SDR found" and then "error: no available RSP devices
# found", and never looked again.  The device is not absent, it is busy, and it
# is free the moment the other program closes -- so ask again, on a timer.  Not
# in a blocking loop: this is the GUI thread, which has to stay able to paint
# and be closed while it waits.
OPEN_RETRY_FIRST = 1.0      # seconds before the first retry
OPEN_RETRY_MAX = 5.0        # longest gap between attempts after that
OPEN_RETRY_TOTAL = 300.0    # stop asking after this long without the device


class DeviceOpener(QObject):
    """Open the radio, waiting out a device another program is holding.

    Kept out of the window so the policy -- what is worth waiting for, how
    often to ask, when to give up -- can be driven one step at a time by a test
    rather than by sleeping on a real timer with a real radio attached.
    """

    opened = pyqtSignal()               # the radio is streaming
    waiting = pyqtSignal(str, float)    # (why it failed, seconds until the retry)
    gave_up = pyqtSignal(str)           # not retryable, or out of time

    def __init__(self, radio: Radio, parent: QObject | None = None):
        super().__init__(parent)
        self.radio = radio
        self.attempts = 0
        self._deadline = 0.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._attempt)

    @property
    def waiting_for_device(self) -> bool:
        """Is another attempt already scheduled?"""
        return self._timer.isActive()

    def start(self) -> bool:
        """Attempt now, and arrange to try again if the device is busy.

        A deliberate start (the Start button, or a setting that restarts the
        stream) gets a fresh budget: the user asking again should not be
        answered with a deadline that has already expired.
        """
        if self.radio.opened:
            return True
        self.cancel()
        self.attempts = 0
        return self._try_now()

    def cancel(self):
        """Forget the retry -- the window is closing, or the user stopped."""
        self._timer.stop()

    def _try_now(self) -> bool:
        # open() is atomic -- a failure part-way through leaves no device
        # claimed -- so there is nothing to undo here beyond the state of the
        # radio itself, which close() resets to "stopped".
        try:
            self.radio.open()
        except Exception as exc:
            self.radio.close()
            if self.attempts == 0:
                self.attempts = 1
                self._deadline = time.monotonic() + OPEN_RETRY_TOTAL
            else:
                self.attempts += 1
            left = self._deadline - time.monotonic()
            if not transient_open_error(exc) or left <= 0:
                self.cancel()
                self.gave_up.emit(str(exc))
                return False
            self._wait(OPEN_RETRY_FIRST if self.attempts == 1
                       else min(OPEN_RETRY_MAX, left), str(exc))
            return False
        self.cancel()
        self.attempts = 0
        self.opened.emit()
        return True

    def _wait(self, delay: float, why: str):
        self._timer.start(int(max(delay, 0.1) * 1000))
        self.waiting.emit(why, delay)

    def _attempt(self):
        self._try_now()


class SettingsPane(QWidget):
    """Every control below the picture, folded away when it is not wanted.

    The picture is what is being watched and the controls are what is being
    changed, and on a short window the two compete for the same height.
    Shutting this pane hides its rows, and the splitter above takes the space
    back -- the waterfall grows by exactly what the rows were using: 120 px of
    it on a 1360x768 screen, and more again once the rows have wrapped on a
    narrow one.

    The fold button is part of the pane rather than a row inside it, because
    it is the one thing here that has to stay reachable when everything else
    has been put away.  The state is called *folded* everywhere -- the file,
    the shortcut and the tests -- so that one word means one thing.
    """

    folded = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._folded = False
        self._rows: list[QWidget] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        head = QWidget()
        h = QHBoxLayout(head)
        h.setContentsMargins(4, 0, 4, 0)
        h.setSpacing(8)
        self.btn_fold = QPushButton()
        self.btn_fold.setCheckable(True)
        self.btn_fold.setToolTip(
            "Fold the controls below the waterfall away and give their height "
            "to the waterfall (Ctrl+H)")
        self.btn_fold.clicked.connect(lambda: self.set_folded(not self._folded))
        h.addWidget(self.btn_fold)
        self.lbl_hint = QLabel("")
        h.addWidget(self.lbl_hint)
        h.addStretch(1)
        self.head = head
        lay.addWidget(head)

        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 0, 0, 0)
        self.body_lay.setSpacing(4)
        lay.addWidget(self.body)
        self._sync()

    def add_row(self, row: QWidget):
        """Add one control row to the half of the pane that folds."""
        self._rows.append(row)
        self.body_lay.addWidget(row)

    def rows(self) -> list[QWidget]:
        """The rows being shown or hidden, in the order they were added."""
        return list(self._rows)

    def is_folded(self) -> bool:
        return self._folded

    def toggle(self):
        self.set_folded(not self._folded)

    def set_folded(self, folded: bool, *, notify: bool = True):
        """Fold or unfold.  ``notify=False`` is for a value read from the file:
        the window is only being brought into line with it, not changed."""
        folded = bool(folded)
        changed = folded != self._folded
        self._folded = folded
        self._sync()
        if notify and changed:
            self.folded.emit(folded)

    def _sync(self):
        shown = not self._folded
        self.body.setVisible(shown)
        self.btn_fold.setChecked(self._folded)
        self.btn_fold.setText("▸  Settings" if self._folded else "▾  Settings")
        self.lbl_hint.setText(
            "folded away — the waterfall has the height" if self._folded else
            "every control below the picture; hide them for a taller waterfall")


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None,
                 memory_path: str | None = None,
                 prefs_store: prefs.PrefsStore | None = None,
                 theme_name: str | None = None):
        """``prefs_store`` is where the settings are remembered between runs.

        Passed in rather than opened here, and optional: with no store the
        window is a plain window that forgets everything, which is what a test
        and a ``--screenshot`` run both want -- neither should be able to write
        over the settings of the person using the program.

        ``theme_name`` is the colour scheme the command line asked for, if it
        asked for one.  Last of the arguments, and by keyword at every call
        site, so that the ones before it keep their places: this parameter once
        goes before ``memory_path`` and ``MainWindow(Settings(), path)`` places
        a channel file in a colour scheme.
        """
        super().__init__()
        self.setWindowTitle(f"{logo.APP_NAME} -- {logo.APP_TAGLINE}")
        # The task-bar entry, set here as well as in __main__ so that a window
        # built by a test or by --screenshot carries the mark too.
        self.setWindowIcon(logo.logo_icon())
        self._size_to_screen()

        self.settings = settings or Settings()
        self.memory = MemoryStore(memory_path)
        self.bridge = Bridge()
        # FT8 listens to the demodulated audio rather than to the sound card,
        # so what it decodes is what the receiver is tuned to even with the
        # volume down or muted.  Its callbacks arrive on the DSP and decode
        # threads and go through the bridge like every other thread's do.
        self.spotter = ft8.FT8Spotter(
            dial_hz=self.settings.tuned_freq,
            on_decodes=lambda d, when: self.bridge.ft8_decodes.emit(d, when),
            on_status=lambda s: self.bridge.ft8_status.emit(s),
            on_error=lambda s: self.bridge.ft8_error.emit(s))
        self.radio = Radio(self.settings,
                           on_spectrum=lambda c, db, sr: self.bridge.spectrum.emit(c, db, sr),
                           on_status=lambda st: self.bridge.status.emit(st),
                           on_error=lambda m: self.bridge.error.emit(m),
                           on_lost=lambda why: self.bridge.lost.emit(why),
                           on_audio=lambda a: self.spotter.feed(a))
        self.opener = DeviceOpener(self.radio, self)
        self._closing = False
        self._latest: tuple | None = None
        self._step = 1000
        self._zoom = 1
        self._building = True
        self._ui_running = False       # mirrors what the Start button shows
        self.frames_drawn = 0
        self.render_seconds = 0.0
        self._ft8_clock = ""            # the slot countdown's text
        self._ft8_status_text = ""      # the last thing the spotter reported
        self._ft8_once = False          # "decode this slot, then stop"
        self._ft8_borrowed: dict[str, tuple] = {}   # settings FT8 is using
        self._meter_t: float | None = None          # when the meter was last fed
        self._meter_text = ""                       # what its readout says
        self.prefs = prefs_store
        self.wants_maximised = False    # set by _restore_window_state
        self._shown = False             # has showEvent run?

        # The colour scheme first, before a single row is built: every control
        # below reads its colour from it as it is made, and a window that
        # opened in one scheme and repainted itself in another would flash.
        self.theme_name = self._chosen_theme(theme_name)
        self.theme = theme.palette(self.theme_name)
        # (label, theme key) for everything whose colour is its own rather than
        # the ordinary text colour; a scheme change has to walk them, because
        # a style sheet set on a widget beats the application's (see
        # _recolour).
        self._tinted: list[tuple[QLabel, str]] = []
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, self.theme_name)

        self._build_ui()
        self._connect()
        self._recolour()
        # The stored window state, applied while _building is still set: the
        # handlers run (so the widgets and the picture agree with the file), but
        # nothing is written back out of a half-built window.
        self._apply_saved_ui()
        self._building = False
        # ... and with the pane's state settled, the shortest this window may
        # be (before _restore_window_state, which asks for it).
        self._update_floor()

        self._timer = QTimer(self)
        self._timer.setInterval(REPAINT_MS)
        self._timer.timeout.connect(self._render)
        self._timer.start()

        # The slot clock is the one thing here that is timed rather than
        # reported: it counts down to a boundary that the audio itself will
        # then land on (see FT8Spotter.feed), so it only runs while the FT8
        # panel is on screen.
        self._ft8_timer = QTimer(self)
        self._ft8_timer.setInterval(FT8_READOUT_MS)
        self._ft8_timer.timeout.connect(self._ft8_tick)

        self._prefs_timer = QTimer(self)
        self._prefs_timer.setSingleShot(True)
        self._prefs_timer.setInterval(PREFS_SAVE_MS)
        self._prefs_timer.timeout.connect(self._remember)
        self._restore_window_state()
        # After the geometry: the divider is a share of whatever height the
        # window ended up with, so it is the last word on the layout.
        self._restore_split()

        if self._current_mode() == "ft8":
            # Started as "--mode ft8": the buttons and the filter are already
            # where the command line put them, but the strip still has to be
            # brought up and the decoder armed -- and only here, after every
            # widget and timer it touches exists.
            self._ft8_mode(True)

        self._refresh_devices()
        self.lbl_device.setText(self._device_label())
        self._set_running_ui(False)

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 4)
        root.setSpacing(5)

        root.addWidget(self._build_header())
        root.addWidget(self._build_tuning_row())
        root.addWidget(self._build_mode_row())
        root.addWidget(self._build_memory_row())
        # The last row before the picture, and the one nearest it: the signal
        # meter reads what the picture below it shows, and an operator glancing
        # at the needle is looking at the top right of the set.
        root.addWidget(self._build_meter_row())

        self.spectrum = SpectrumPlot()
        self.waterfall = WaterfallPlot()
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.addWidget(self.spectrum)
        self.split.addWidget(self.waterfall)
        self.split.setSizes([300, 340])     # the divider is remembered too
        root.addWidget(self.split, 1)

        # Everything that is set rather than listened to, in one pane that
        # folds away (Ctrl+H, and remembered).  With it shut, the height these
        # rows were using is the splitter's, so the waterfall grows by what the
        # controls gave up.  The FT8 strip is deliberately *not* in here: it is
        # a decode log that appears when the mode is selected, not a setting,
        # and folding the settings away must not take the log with them.
        self.settings_pane = SettingsPane()
        self.settings_pane.add_row(self._build_look_row())
        self.settings_pane.add_row(self._build_rf_row())
        self.settings_pane.add_row(self._build_waterfall_row())
        self._tint(self.settings_pane.lbl_hint, "muted")
        root.addWidget(self.settings_pane)

        # Last, under the controls, and hidden until FT8 is selected: a decode
        # list belongs at the foot of the window, where it reads as a log and
        # where making it appear does not move the controls the pointer is
        # already aiming at.
        self.ft8_panel = self._build_ft8_panel()
        root.addWidget(self.ft8_panel)
        self.setCentralWidget(central)

        self.status = self.statusBar()
        self.lbl_status = QLabel("")
        self.status.addPermanentWidget(self.lbl_status)
        self.lbl_cursor = QLabel("")
        self.status.addWidget(self.lbl_cursor, 1)

    def _size_to_screen(self):
        """Open at a size that actually fits the display.

        Both halves of this matter on a 1360x768 screen: a window taller than
        the screen loses its waterfall and status bar off the bottom, and a
        minimum size larger than the screen means the window manager cannot
        maximise it sensibly at all.  The layout can wrap now, so the floor can
        be small.
        """
        # Small enough to fit any realistic screen (so maximising always
        # works), large enough that the plots are still worth looking at once
        # the control rows have wrapped.  This is the *folded* floor; the
        # unfolded one is worked out from the rows themselves once they exist
        # (see _update_floor).
        self.setMinimumSize(MIN_WIDTH, FOLDED_MIN_HEIGHT)
        screen = QApplication.primaryScreen()
        if screen is None:                      # pragma: no cover
            self.resize(1280, 860)
            return
        avail = screen.availableGeometry()
        w = max(self.minimumWidth(), min(1280, avail.width() - 40))
        h = max(self.minimumHeight(), min(940, avail.height() - 40))
        self.resize(w, h)
        self.move(avail.x() + max(0, (avail.width() - w) // 2),
                  avail.y() + max(0, (avail.height() - h) // 2))

    def _chosen_theme(self, given: str | None) -> str:
        """Which colour scheme to open in: the command line, then the file.

        An unknown name from either is not an error and does not become one:
        the default scheme is used instead, the same way every other unusable
        remembered value falls back to its default rather than refusing.
        """
        if given in theme.names():
            return given
        if self.prefs is not None:
            remembered = self.prefs.value("ui", "theme", "choice",
                                          tuple(theme.names()), None)
            if remembered in theme.names():
                return remembered
        return theme.DEFAULT

    def _tint(self, widget: QLabel, key: str) -> QLabel:
        """Colour a label from the theme, and remember it to be re-coloured.

        A style sheet set on a widget wins over the application's, so these
        cannot be left to the palette -- they are recorded here and repainted
        one by one when the scheme changes (see :meth:`_recolour`).
        """
        self._tinted.append((widget, key))
        widget.setStyleSheet(f"color:{self.theme[key]}")
        return widget

    def _build_header(self) -> QWidget:
        """The application's mark and name, across the top of the window."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 0, 4, 0)
        h.setSpacing(9)

        mark = logo.Logo(30)
        h.addWidget(mark)

        name = QLabel(logo.APP_NAME)
        name.setFont(mono_font(17, True))
        self._tint(name, "bright")
        h.addWidget(name)

        tag = QLabel(logo.APP_TAGLINE)
        self._tint(tag, "muted")
        h.addWidget(tag)
        h.addStretch(1)

        self.logo = mark
        self.lbl_app = name
        self.lbl_tag = tag
        return w

    def _row(self) -> tuple[QWidget, FlowLayout]:
        """An empty control row that wraps instead of forcing a wide window."""
        w = QWidget()
        lay = FlowLayout(w)
        policy = w.sizePolicy()
        policy.setHeightForWidth(True)      # let the row choose its own height
        w.setSizePolicy(policy)
        return w, lay

    @staticmethod
    def _pair(label: str | QWidget, *controls: QWidget) -> QWidget:
        """A label and its control glued into one item.

        The rows wrap, and the flow layout wraps between *items* -- so a label
        and the widget it names, added separately, can end up on different
        lines ("Squelch" left dangling at the end of the mode row with its
        spin box on the next one).  Grouping them keeps the pair together and
        keeps the caption next to what it describes.
        """
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        h.addWidget(QLabel(label) if isinstance(label, str) else label)
        for c in controls:
            h.addWidget(c)
        return box

    def _build_meter_row(self) -> QWidget:
        """The signal meter, upper right, in the row above the picture.

        It used to sit inside the gain row, among a dozen boxes, where the one
        instrument in the window that is meant to be read at a glance was the
        hardest thing to find.  Here it is the top right corner of the display
        area, which is where a transceiver puts it, with the same reading in
        figures beside it for the times the needle is between two marks.  Its
        setup -- the calibration, the peak marker and the face -- is with the
        rest of the settings below the picture.
        """
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 0, 4, 0)
        h.setSpacing(8)
        h.addStretch(1)                 # the pair sits against the right edge

        self.lbl_smeter = QLabel("")            # the same reading as text
        self.lbl_smeter.setFont(mono_font(9))
        self._tint(self.lbl_smeter, "text")
        self.lbl_smeter.setMinimumWidth(132)
        self.lbl_smeter.setAlignment(Qt.AlignmentFlag.AlignRight
                                     | Qt.AlignmentFlag.AlignVCenter)
        h.addWidget(self.lbl_smeter)

        self.smeter = SMeter()
        h.addWidget(self.smeter)
        return w

    def _build_look_row(self) -> QWidget:
        """How the window looks, and what the meter is set to read."""
        w, lay = self._row()

        self.cmb_theme = QComboBox()
        for name in theme.names():
            self.cmb_theme.addItem(theme.label(name), name)
            self.cmb_theme.setItemData(self.cmb_theme.count() - 1,
                                       theme.note(name),
                                       Qt.ItemDataRole.ToolTipRole)
        self.cmb_theme.setToolTip(
            "The window's colour scheme.  One table sets the palette, the "
            "controls, the plots and the labels, so everything that can be "
            "coloured follows this one choice.\n"
            "Daylight is for a bright room or a projector; the rest are dark.")
        lay.addWidget(self._pair("Theme", self.cmb_theme))

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep)

        # The meter's own settings, here rather than beside the needle: the
        # needle is up above the picture now, and a control that changes a
        # setting belongs with the other settings, not in the corner of the
        # display.
        self.spn_sref = QDoubleSpinBox()
        self.spn_sref.setDecimals(1)
        self.spn_sref.setRange(-140.0, 0.0)
        self.spn_sref.setSingleStep(0.5)
        self.spn_sref.setSuffix(" dBFS")
        self.spn_sref.setValue(smeter.DEFAULT_REF_DBFS)
        self.spn_sref.setToolTip(
            "The level that reads S9.  The RSP1 reports the level in dBFS and "
            "has no absolute calibration of its own, so this one number is "
            "what puts the scale in the right place: set it until the meter "
            "agrees with a station, or a signal generator, of known strength.\n"
            "Set it at the IF and RF gain you actually use, because the level "
            "it is measured from is taken after those stages.")
        lay.addWidget(self._pair("S9 at", self.spn_sref))

        self.chk_speak = QCheckBox("Peak")
        self.chk_speak.setChecked(True)
        self.chk_speak.setToolTip(
            "A second needle that holds the highest level of the last half "
            "second, so a brief signal is not missed between repaints")
        lay.addWidget(self.chk_speak)

        self.cmb_sface = QComboBox()
        for name in smeter.FACE_NAMES:
            self.cmb_sface.addItem(name.capitalize(), name)
        self.cmb_sface.setToolTip(
            "Meter scale colouring: the dark face of the later radios, or the "
            "cream dial of the older analogue ones.  The face is the meter's "
            "own -- a scheme does not change it -- and the cream one is the "
            "one that sits well on the Daylight scheme.")
        lay.addWidget(self._pair("Face", self.cmb_sface))

        return w

    def _build_tuning_row(self) -> QWidget:
        w, lay = self._row()

        self.btn_start = QPushButton("▶  Start")
        self.btn_start.setMinimumWidth(96)
        self.btn_start.setStyleSheet(self._start_button_style(False))
        lay.addWidget(self.btn_start)

        self.lbl_device = QLabel("no device")
        self._tint(self.lbl_device, "muted")
        lay.addWidget(self.lbl_device)

        self.cmb_rate = QComboBox()
        for name, val in RATES:
            self.cmb_rate.addItem(name, val)
        # A rate outside the list -- "--rate 7000000" on the command line, or
        # any rate the hardware accepts that the list does not name -- used to
        # raise ValueError here and take the whole window down with it.  Add it
        # to the list instead, so what the window shows is what is running.
        idx = self.cmb_rate.findData(int(self.settings.sample_rate))
        if idx < 0:
            self.cmb_rate.addItem(f"{self.settings.sample_rate/1e6:g} MSps",
                                  int(self.settings.sample_rate))
            idx = self.cmb_rate.count() - 1
        self.cmb_rate.setCurrentIndex(idx)
        lay.addWidget(self._pair("Rate", self.cmb_rate))

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep)

        self.spn_freq = QDoubleSpinBox()
        self.spn_freq.setDecimals(6)
        self.spn_freq.setRange(0.01, 2000.0)
        self.spn_freq.setSuffix(" MHz")
        self.spn_freq.setFont(mono_font(20, True))
        self.spn_freq.setMinimumWidth(280)
        self.spn_freq.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.spn_freq.setValue(self.settings.tuned_freq / 1e6)
        lay.addWidget(self.spn_freq)

        self.lbl_band = QLabel("—")
        self.lbl_band.setFont(mono_font(11, True))
        self._tint(self.lbl_band, "accent")
        self.lbl_band.setMinimumWidth(60)
        lay.addWidget(self.lbl_band)

        self.btn_step_down = QPushButton("◀")
        self.btn_step_down.setFixedWidth(30)
        self.btn_step_down.setToolTip("Step the tuning down by the step size")
        self.btn_step_down.clicked.connect(lambda: self._step_tune(-1))
        lay.addWidget(self.btn_step_down)
        self.btn_step_up = QPushButton("▶")
        self.btn_step_up.setFixedWidth(30)
        self.btn_step_up.setToolTip("Step the tuning up by the step size")
        self.btn_step_up.clicked.connect(lambda: self._step_tune(1))
        lay.addWidget(self.btn_step_up)

        self.cmb_step = QComboBox()
        for name, val in STEPS:
            self.cmb_step.addItem(name, val)
        self.cmb_step.setCurrentIndex(3)
        lay.addWidget(self.cmb_step)

        self.spn_center = QDoubleSpinBox()
        self.spn_center.setDecimals(6)
        self.spn_center.setRange(0.01, 2000.0)
        self.spn_center.setSuffix(" MHz")
        self.spn_center.setSingleStep(0.1)
        self.spn_center.setValue(self.settings.center_freq / 1e6)
        self.spn_center.setMinimumWidth(150)
        self.btn_recentre = QPushButton("⌖")
        self.btn_recentre.setToolTip("Recentre the spectrum on the tuned frequency")
        self.btn_recentre.setFixedWidth(30)
        lay.addWidget(self._pair("Centre", self.spn_center, self.btn_recentre))

        self.cmb_band = QComboBox()
        self.cmb_band.addItem("Jump to band…", None)
        self.cmb_band.insertSeparator(1)
        for bd in bands.HAM_BANDS:
            self.cmb_band.addItem(f"Ham {bd.name}   ({bd.note})", bd)
        self.cmb_band.insertSeparator(self.cmb_band.count())
        for bd in bands.SW_BANDS:
            self.cmb_band.addItem(f"SW  {bd.name}", bd)
        lay.addWidget(self.cmb_band)

        self.cmb_preset = QComboBox()
        self.cmb_preset.addItem("Presets…", None)
        for p in bands.PRESETS:
            self.cmb_preset.addItem(f"{p.name}  {p.freq/1e6:.4f}", p)
        lay.addWidget(self.cmb_preset)

        return w

    def _build_mode_row(self) -> QWidget:
        w, lay = self._row()

        self.mode_buttons = {}
        for label, key, _ in MODES:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setFixedWidth(52)
            self.mode_buttons[key] = b
        lay.addWidget(self._pair("Mode", *self.mode_buttons.values()))
        # Tick the mode the window was started in.  Not _select_mode: the
        # receiver is already running the settings it was built with (possibly
        # "--bw" off the command line), and all that was ever wrong here was
        # that the button for the mode actually in use was not lit.
        if self.settings.mode in self.mode_buttons:
            self.mode_buttons[self.settings.mode].setChecked(True)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep)

        self.cmb_bw = QComboBox()
        for name, val in BANDWIDTHS:
            self.cmb_bw.addItem(name, val)
        self._select_bw(self.settings.filter_bw)
        lay.addWidget(self._pair("Filter", self.cmb_bw))

        self.spn_pitch = QDoubleSpinBox()
        self.spn_pitch.setRange(200.0, 1500.0)
        self.spn_pitch.setSingleStep(10.0)
        self.spn_pitch.setValue(self.settings.cw_pitch)
        self.spn_pitch.setSuffix(" Hz")
        lay.addWidget(self._pair("CW pitch", self.spn_pitch))

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep)

        self.sld_vol = QSlider(Qt.Orientation.Horizontal)
        self.sld_vol.setRange(0, 150)
        self.sld_vol.setValue(int(self.settings.volume * 100))
        self.sld_vol.setFixedWidth(110)
        lay.addWidget(self._pair("Vol", self.sld_vol))

        self.chk_mute = QCheckBox("Mute")
        lay.addWidget(self.chk_mute)
        self.chk_aagc = QCheckBox("Audio AGC")
        self.chk_aagc.setChecked(self.settings.audio_agc)
        lay.addWidget(self.chk_aagc)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep)

        self.chk_nr = QCheckBox("NR")
        self.chk_nr.setChecked(self.settings.nr)
        self.chk_nr.setToolTip("Noise reduction -- subtracts the hiss from the "
                               "demodulated audio")
        lay.addWidget(self.chk_nr)

        self.sld_nr = QSlider(Qt.Orientation.Horizontal)
        self.sld_nr.setRange(0, 100)
        self.sld_nr.setValue(int(self.settings.nr_level))
        self.sld_nr.setFixedWidth(90)
        self.sld_nr.setEnabled(self.settings.nr)
        self.sld_nr.setToolTip("How hard the noise reduction works")
        lay.addWidget(self.sld_nr)
        self.lbl_nr = QLabel(f"{int(self.settings.nr_level)}%")
        self.lbl_nr.setFont(mono_font(9))
        self.lbl_nr.setMinimumWidth(34)
        lay.addWidget(self.lbl_nr)

        self.spn_sq = QDoubleSpinBox()
        self.spn_sq.setRange(-140.0, 0.0)
        self.spn_sq.setSuffix(" dB")
        # The remembered squelch, floored at what the control offers: anything
        # at or below -140 is "off", and -140 is as low as this spinner goes
        # (the settings themselves use -200 for off, as the decoder does), so
        # what is shown is the value in force rather than a floor of the range.
        self.spn_sq.setValue(max(-140.0, float(self.settings.squelch_db)))
        self.spn_sq.setToolTip("Mute audio below this signal level (-140 = off)")
        lay.addWidget(self._pair("Squelch", self.spn_sq))

        return w

    def _build_memory_row(self) -> QWidget:
        w, lay = self._row()

        self.cmb_mem = QComboBox()
        self.cmb_mem.setMinimumWidth(360)
        self.cmb_mem.setToolTip("Saved channels -- Recall, or double-click")

        self.btn_mem_recall = QPushButton("Recall")
        self.btn_mem_save = QPushButton("Save current…")
        self.btn_mem_update = QPushButton("Update")
        self.btn_mem_update.setToolTip("Overwrite the selected channel with the "
                                       "current frequency and settings")
        self.btn_mem_delete = QPushButton("Delete")

        self.lbl_mem = QLabel("")
        self._tint(self.lbl_mem, "muted")
        lay.addWidget(self._pair("Memory", self.cmb_mem))
        for b in (self.btn_mem_recall, self.btn_mem_save, self.btn_mem_update,
                  self.btn_mem_delete):
            lay.addWidget(b)
        lay.addWidget(self.lbl_mem)

        self._mem_refresh()
        return w

    def _build_rf_row(self) -> QWidget:
        w, lay = self._row()

        self.chk_agc = QCheckBox("RF AGC")
        self.chk_agc.setChecked(self.settings.rf_agc)
        lay.addWidget(self.chk_agc)

        self.sld_if = QSlider(Qt.Orientation.Horizontal)
        self.sld_if.setRange(20, 59)
        self.sld_if.setValue(int(self.settings.if_gain))
        self.sld_if.setFixedWidth(160)
        self.lbl_if = QLabel(f"{int(self.settings.if_gain)} dB")
        self.lbl_if.setFont(mono_font(9))
        self._tint(self.lbl_if, "muted")
        self.lbl_if.setMinimumWidth(48)
        lay.addWidget(self._pair("IF gain", self.sld_if, self.lbl_if))

        self.cmb_rf = QComboBox()
        for i in range(4):
            self.cmb_rf.addItem(str(i), i)
        # Showing the setting rather than the first entry of the list: these
        # two are remembered like everything else, and a control that disagrees
        # with the value in use is worse than no control at all.
        self.cmb_rf.setCurrentIndex(max(0, self.cmb_rf.findData(
            int(self.settings.rf_gain))))
        lay.addWidget(self._pair("RF gain", self.cmb_rf))

        self.cmb_tbw = QComboBox()
        self.cmb_tbw.addItem("Auto", 0)
        for bw in TUNER_BANDWIDTHS:
            self.cmb_tbw.addItem(f"{bw/1e6:g} MHz", bw)
        self.cmb_tbw.setCurrentIndex(max(0, self.cmb_tbw.findData(
            int(self.settings.tuner_bw))))
        lay.addWidget(self._pair("Tuner BW", self.cmb_tbw))

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep)

        self.cmb_fft = QComboBox()
        for n in FFTS:
            self.cmb_fft.addItem(str(n), n)
        # Whatever the settings say, which is the remembered size -- or one from
        # a settings file this list does not name, in which case it is added so
        # that what the window shows is what is really in use (the same
        # treatment the sample-rate list gets, and for the same reason).
        idx = self.cmb_fft.findData(int(self.settings.n_fft))
        if idx < 0:
            self.cmb_fft.addItem(str(int(self.settings.n_fft)),
                                 int(self.settings.n_fft))
            idx = self.cmb_fft.count() - 1
        self.cmb_fft.setCurrentIndex(idx)
        self.cmb_fft.setToolTip(
            "Frequency resolution of the spectrum and the waterfall: one bin "
            "is the span divided by this.  4096 suits a 2 MSps span; larger "
            "sizes pay for themselves on a zoomed view")
        lay.addWidget(self._pair("FFT", self.cmb_fft))

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep)

        # The signal meter that used to be here has moved to the row above the
        # picture, and its setup -- S9 at, Peak, Face -- to the look row: what
        # is left is the receiver's own gain and resolution, which is what this
        # row is for.
        return w

    def _build_waterfall_row(self) -> QWidget:
        w, lay = self._row()

        self.chk_wfauto = QCheckBox("Auto levels")
        self.chk_wfauto.setChecked(True)
        self.chk_wfauto.setToolTip(
            "Track the noise floor for you; untick to set Floor and Ceiling by hand")
        lay.addWidget(self.chk_wfauto)

        self.spn_wflo = QDoubleSpinBox()
        self.spn_wflo.setRange(-160.0, 20.0)
        self.spn_wflo.setValue(-110.0)
        self.spn_wflo.setSuffix(" dB")
        self.spn_wflo.setToolTip("Signal level drawn as black")
        self.spn_wflo.setEnabled(False)
        lay.addWidget(self._pair("Floor", self.spn_wflo))

        self.spn_wfhi = QDoubleSpinBox()
        self.spn_wfhi.setRange(-160.0, 40.0)
        self.spn_wfhi.setValue(-40.0)
        self.spn_wfhi.setSuffix(" dB")
        self.spn_wfhi.setToolTip("Signal level drawn in the brightest colour")
        self.spn_wfhi.setEnabled(False)
        lay.addWidget(self._pair("Ceiling", self.spn_wfhi))

        self.spn_contrast = QDoubleSpinBox()
        self.spn_contrast.setRange(6.0, 90.0)
        self.spn_contrast.setValue(30.0)
        self.spn_contrast.setSuffix(" dB")
        self.spn_contrast.setToolTip(
            "With auto levels: how far above the noise floor reaches the "
            "brightest colour")
        lay.addWidget(self._pair("Contrast", self.spn_contrast))

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep)

        self.cmb_cmap = QComboBox()
        for name, key in COLORMAPS:
            self.cmb_cmap.addItem(name, key)
        self.chk_cmrev = QCheckBox("Reverse")
        self.chk_cmrev.setToolTip("Swap the dark and bright ends of the colour map")
        lay.addWidget(self._pair("Colour", self.cmb_cmap, self.chk_cmrev))

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep)

        # The waterfall's frequency detail.  It is a list rather than a spin
        # box because the useful values are a handful of round numbers, and the
        # choice has to be readable at a glance next to the picture it changes.
        self.cmb_wfdetail = QComboBox()
        for name, cols in WF_DETAILS:
            self.cmb_wfdetail.addItem(name, cols)
        self.cmb_wfdetail.setToolTip(
            "How many frequency columns a waterfall line keeps.  Screen "
            "matches the plot's own width, so the picture is 1:1 with the "
            "display and nothing is stretched; the fixed sizes buy a smoother "
            "decimation of a large FFT at the cost of a heavier repaint.  A row "
            "is never stretched beyond the data behind it")
        lay.addWidget(self._pair("Detail", self.cmb_wfdetail))

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep)

        self.cmb_zoom = QComboBox()
        for name, val in ZOOMS:
            self.cmb_zoom.addItem(name, val)
        self.cmb_zoom.setToolTip(
            "Magnify both plots about the receiver centre; retuning keeps the "
            "signal in view.\n"
            "The ladder runs to 256x, which on a 2 MSps span is a 7.8 kHz "
            "window.  How much of it the FFT can fill is the FFT size divided "
            "by the zoom: the picture stops getting finer once that is down to "
            "one bin per column, and the status line says what a column is "
            "worth in Hz.")

        # Buttons as well as the list: the list says what the magnification is,
        # the buttons are how you change it without having to open anything.
        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_out.setFixedWidth(28)
        self.btn_zoom_out.setToolTip("Zoom out — show a wider span")
        self.btn_zoom_out.clicked.connect(lambda: self._zoom_step(-1))
        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.setFixedWidth(28)
        self.btn_zoom_in.setToolTip("Zoom in — magnify a narrower span")
        self.btn_zoom_in.clicked.connect(lambda: self._zoom_step(1))
        self.btn_zoom_fit = QPushButton("Fit")
        self.btn_zoom_fit.setToolTip("Show the whole receiver span (zoom 1x)")
        self.btn_zoom_fit.clicked.connect(self._zoom_fit)
        self.lbl_zoom = QLabel()
        self.lbl_zoom.setFont(mono_font(9))
        self._tint(self.lbl_zoom, "muted")
        self.lbl_zoom.setMinimumWidth(66)
        self.lbl_zoom.setToolTip("The frequency width currently on screen")
        lay.addWidget(self._pair("Zoom", self.btn_zoom_out, self.cmb_zoom,
                                 self.btn_zoom_in, self.btn_zoom_fit,
                                 self.lbl_zoom))
        self._update_zoom_readout()

        self.btn_clear = QPushButton("Clear peak")
        self.btn_clear.setToolTip("Forget the spectrum's peak-hold trace")
        lay.addWidget(self.btn_clear)

        return w

    def _build_ft8_panel(self) -> QWidget:
        """The FT8 strip: band presets, the slot clock, and the decode list."""
        w = QFrame()
        w.setObjectName("ft8panel")
        w.setStyleSheet(self._panel_style("ft8panel"))
        lay = QVBoxLayout(w)
        lay.setContentsMargins(7, 5, 7, 5)
        lay.setSpacing(4)

        top, row = self._row()

        self.cmb_ft8band = QComboBox()
        for b in ft8.FT8_BANDS:
            label = f"{b.name}  {b.dial/1e6:.3f} MHz"
            self.cmb_ft8band.addItem(
                label + (f"  ({b.note})" if b.note else ""), b.name)
        self.cmb_ft8band.setMinimumWidth(210)
        self.cmb_ft8band.setToolTip(
            "The standard FT8 dial frequency for a band (WSJT-X's convention): "
            "the receiver is tuned there and the signals sit above it in the "
            "audio passband, so a signal on 1000 Hz is 14.075 MHz")
        row.addWidget(self._pair("FT8 band", self.cmb_ft8band))

        self.chk_ft8every = QCheckBox("Every slot")
        self.chk_ft8every.setChecked(True)
        self.chk_ft8every.setToolTip(
            "Decode each 15-second slot as it passes, or untick to decode the "
            "next one and stop")
        row.addWidget(self.chk_ft8every)

        self.btn_ft8clear = QPushButton("Clear")
        self.btn_ft8clear.setToolTip("Forget the decoded messages listed below")
        row.addWidget(self.btn_ft8clear)

        self.lbl_ft8 = QLabel("")
        self.lbl_ft8.setFont(mono_font(10))
        self._tint(self.lbl_ft8, "accent")
        self.lbl_ft8.setToolTip("Slot clock, decoder state, and totals")
        row.addWidget(self.lbl_ft8)
        lay.addWidget(top)

        self.tbl_ft8 = QTableWidget(0, len(FT8_COLS))
        self.tbl_ft8.setHorizontalHeaderLabels([name for name, _ in FT8_COLS])
        self.tbl_ft8.verticalHeader().setVisible(False)
        self.tbl_ft8.setShowGrid(False)
        self.tbl_ft8.setFont(mono_font(10))
        self.tbl_ft8.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tbl_ft8.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.tbl_ft8.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection)
        # No focus, so the arrow keys and the wheel still belong to the
        # frequency box and the plots rather than scrolling this list.
        self.tbl_ft8.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self.tbl_ft8.horizontalHeader()
        for i, (_, px) in enumerate(FT8_COLS):
            if px:
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
                self.tbl_ft8.setColumnWidth(i, px)
            else:
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
        # As tall as six decodes, and able to shrink to three: the strip is
        # worth less than the waterfall when the window is at its smallest.
        self.tbl_ft8.setMinimumHeight(24 + FT8_MIN_ROWS * FT8_ROW_PX)
        self.tbl_ft8.setMaximumHeight(24 + FT8_ROWS * FT8_ROW_PX)
        self.tbl_ft8.setToolTip(
            "One line per decoded message.  Click a line to tune to the signal "
            "that sent it")
        lay.addWidget(self.tbl_ft8)

        w.setVisible(False)
        return w

    # ------------------------------------------------------- wiring
    def _connect(self):
        self.btn_start.clicked.connect(self._toggle)
        self.cmb_rate.currentIndexChanged.connect(self._rate_changed)
        self.spn_freq.valueChanged.connect(self._freq_changed)
        self.spn_center.valueChanged.connect(self._center_changed)
        self.cmb_step.currentIndexChanged.connect(self._step_changed)
        self.btn_recentre.clicked.connect(self._recentre)
        self.cmb_band.currentIndexChanged.connect(self._band_selected)
        self.cmb_preset.currentIndexChanged.connect(self._preset_selected)

        for key, btn in self.mode_buttons.items():
            btn.clicked.connect(lambda _=False, k=key: self._mode_selected(k))

        self.cmb_bw.currentIndexChanged.connect(self._bw_changed)
        self.spn_pitch.valueChanged.connect(
            lambda v: self._apply(cw_pitch=float(v)))

        self.sld_vol.valueChanged.connect(self._volume_changed)
        self.chk_mute.toggled.connect(self._mute_changed)
        self.chk_aagc.toggled.connect(lambda v: self._apply(audio_agc=bool(v)))
        self.chk_nr.toggled.connect(self._nr_toggled)
        self.sld_nr.valueChanged.connect(self._nr_level_changed)
        self.spn_sq.valueChanged.connect(
            lambda v: self._apply(squelch_db=float(v)))

        self.btn_mem_recall.clicked.connect(self._mem_recall)
        self.btn_mem_save.clicked.connect(self._mem_save)
        self.btn_mem_update.clicked.connect(self._mem_update)
        self.btn_mem_delete.clicked.connect(self._mem_delete)
        view = self.cmb_mem.view()
        if view is not None:            # double-click a memory to recall it
            view.doubleClicked.connect(lambda _idx: self._mem_recall())

        self.chk_agc.toggled.connect(self._agc_changed)
        self.sld_if.valueChanged.connect(self._if_gain_changed)
        self.cmb_rf.currentIndexChanged.connect(
            lambda i: self._apply(rf_gain=int(self.cmb_rf.itemData(i))))
        self.cmb_tbw.currentIndexChanged.connect(
            lambda i: self._apply(tuner_bw=int(self.cmb_tbw.itemData(i))))
        self.cmb_fft.currentIndexChanged.connect(
            lambda i: self._apply(n_fft=int(self.cmb_fft.itemData(i))))

        self.spn_sref.valueChanged.connect(self._smeter_ref_changed)
        self.chk_speak.toggled.connect(self._smeter_peak_changed)
        self.cmb_sface.currentIndexChanged.connect(self._smeter_face_changed)

        self.cmb_theme.currentIndexChanged.connect(self._theme_changed)
        self.settings_pane.folded.connect(self._settings_folded)
        # A key for it as well as the button: the point of folding the controls
        # away is to see more of the picture, and a pointer trip down to that
        # row and back is most of what there was to avoid.
        self._fold_shortcut = QShortcut(QKeySequence("Ctrl+H"), self)
        self._fold_shortcut.activated.connect(self._fold_settings)

        self.cmb_ft8band.currentIndexChanged.connect(self._ft8_band_selected)
        self.chk_ft8every.toggled.connect(self._ft8_every_changed)
        self.btn_ft8clear.clicked.connect(self._ft8_clear)
        self.tbl_ft8.cellClicked.connect(self._ft8_tune_to_row)
        self.bridge.ft8_decodes.connect(self._ft8_add_decodes)
        self.bridge.ft8_status.connect(self._ft8_spotter_says)
        self.bridge.ft8_error.connect(self._ft8_error)

        self.opener.opened.connect(self._device_opened)
        self.opener.waiting.connect(self._device_waiting)
        self.opener.gave_up.connect(self._device_gave_up)
        self.bridge.lost.connect(self._device_lost)

        self.spectrum.tuned.connect(self._tune)
        self.waterfall.tuned.connect(self._tune)
        self.spectrum.settled.connect(self._tune_settled)
        self.waterfall.settled.connect(self._tune_settled)
        self.waterfall.zoom.connect(self._zoom_step)
        self.spectrum.recentre.connect(self._recentre_to)
        self.spectrum.stepped.connect(self._step_tune)
        self.spectrum.cursor.connect(
            lambda f: self.lbl_cursor.setText(
                f"cursor  {f/1e6:11.6f} MHz   {bands.format_hz(f)} Hz"))

        self.chk_wfauto.toggled.connect(self._wf_settings_changed)
        self.spn_contrast.valueChanged.connect(self._wf_settings_changed)
        self.spn_wflo.valueChanged.connect(self._wf_settings_changed)
        self.spn_wfhi.valueChanged.connect(self._wf_settings_changed)
        self.cmb_cmap.currentIndexChanged.connect(self._wf_colour_changed)
        self.chk_cmrev.toggled.connect(self._wf_colour_changed)
        self.cmb_wfdetail.currentIndexChanged.connect(self._wf_detail_changed)
        self.cmb_zoom.currentIndexChanged.connect(self._zoom_changed)
        self.btn_clear.clicked.connect(lambda: setattr(self.spectrum, "peak", None))

        # Apply the starting state of the level/colour controls.
        self._wf_settings_changed()
        self._wf_colour_changed()

        self.bridge.spectrum.connect(self._on_spectrum)
        self.bridge.status.connect(self._on_status)
        self.bridge.error.connect(self._on_error)

    # ------------------------------------------------- remembering
    # What the window was set to last time, so that nothing has to be set up
    # twice.  The receiver settings come from the settings file through
    # prefs.settings_from (the CLI and the file both feed the Settings the
    # window is built with); what is left here is the window's own state -- how
    # far it is zoomed, what the picture looks like, which FT8 band -- and the
    # geometry of the window itself.

    # ------------------------------------------------------- appearance
    def _panel_style(self, name: str) -> str:
        """A raised panel's own style sheet, in the current theme's colours."""
        p = self.theme
        return (f"#{name}{{background:{p['panel']};border:1px solid "
                f"{p['border']};border-radius:4px}}")

    def _start_button_style(self, running: bool) -> str:
        """The Start / Stop button: green to go, red to stop, in-theme.

        On the button rather than in the application's style sheet, which
        every other QPushButton follows: this one carries a meaning, and it has
        to keep it under a scheme that would otherwise paint it like the rest.
        """
        key, hover = (("stop", "stop_hover") if running
                      else ("run", "run_hover"))
        return (f"QPushButton{{background:{self.theme[key]};color:white;"
                f"font-weight:bold;padding:6px 10px;border-radius:4px}}"
                f"QPushButton:hover{{background:{self.theme[hover]}}}")

    def _use_theme(self, name: str, *, remember: bool = True):
        """Put a colour scheme on the window, and redraw what carries one."""
        self.theme_name = name if name in theme.names() else theme.DEFAULT
        self.theme = theme.palette(self.theme_name)
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, self.theme_name)
        self._recolour()
        if remember:
            self._remember_soon()

    def _recolour(self):
        """Repaint everything whose colour is not simply the palette's.

        The palette and the style sheet are the application's business (see
        :func:`apply_theme`).  What is left over is here for three reasons:
        the plots are painted item by item, so their backgrounds, their pens
        and the passband wash have to be set again; a style sheet on a label
        beats the application's, which is what the tinted readouts use; and the
        FT8 strip is a frame whose panel colour is its own style sheet.
        """
        p = self.theme
        for plot in (self.spectrum, self.waterfall):
            plot.setBackground(p["plot"])
            for side in ("left", "bottom"):
                axis = plot.getAxis(side)
                axis.setPen(pg.mkPen(p["grid"], width=1.0))
                axis.setTextPen(pg.mkPen(p["muted"]))
        # The grid's colour follows the axis pen; this is only how strongly it
        # is drawn, and a light scheme needs more of it to be seen at all.
        self.spectrum.showGrid(x=True, y=True, alpha=float(p["grid_alpha"]))
        self.spectrum.curve.setPen(pg.mkPen(p["accent"], width=1.2))
        self.spectrum.peak_curve.setPen(pg.mkPen(p["peak"], width=1.0))
        self.spectrum.marker.setPen(pg.mkPen(p["marker"], width=1.6))
        self.spectrum.band.setBrush(pg.mkBrush(*theme.rgb(p["band"]), 40))
        for label, key in self._tinted:
            label.setStyleSheet(f"color:{p[key]}")
        self.ft8_panel.setStyleSheet(self._panel_style("ft8panel"))
        self.btn_start.setStyleSheet(self._start_button_style(self._ui_running))
        # The FT8 readout says more than one thing in more than one colour, so
        # its own handler is what colours it: run it again for the new scheme.
        if self.ft8_panel.isVisible():
            self._ft8_tick()

    def _theme_changed(self, *_):
        self._use_theme(str(self.cmb_theme.currentData()))

    # -- folding the controls away -----------------------------------
    def _fold_settings(self):
        """Ctrl+H: fold the controls away, or bring them back."""
        self.settings_pane.toggle()

    def _settings_folded(self, _folded: bool):
        """The pane was folded or unfolded: the plots take the height either
        way, and the window may not need to be as tall as it just was."""
        self._update_floor()
        self._remember_soon()

    def _update_floor(self):
        """The shortest this window may be, given what it is showing now.

        With the controls folded away that is the small fixed floor the window
        has always had.  With them on show it is what they actually need: the
        rows are height-for-width, so their height depends on the width the
        window has (they wrap onto more lines when it is narrow), and the
        answer is the sum of those heights plus a minimum for the picture.

        Qt will not work this out on its own, because every control row has a
        small minimum size -- one control per line is the worst case the
        wrapping layout reports -- so a window smaller than its rows is within
        Qt's rules and the rows are simply cut off by the bottom edge.  A
        control nobody can see, with nothing to say it is there, is worse than
        a window that will not shrink any further.
        """
        if not hasattr(self, "settings_pane"):
            return
        if self.settings_pane.is_folded():
            floor = FOLDED_MIN_HEIGHT
        else:
            lay = self.centralWidget().layout()
            margins = lay.contentsMargins()
            floor = (margins.top() + margins.bottom()
                     + lay.spacing() * max(0, lay.count() - 1))
            width = max(self.width(), MIN_WIDTH)
            for i in range(lay.count()):
                wdg = lay.itemAt(i).widget()
                if wdg is None or wdg.isHidden():
                    continue            # a hidden FT8 strip takes no height
                if wdg is self.split:
                    floor += PLOT_MIN_HEIGHT
                elif wdg.hasHeightForWidth():
                    floor += wdg.heightForWidth(width)
                else:
                    floor += wdg.sizeHint().height()
        floor = max(FOLDED_MIN_HEIGHT, int(floor))
        if floor != self.minimumHeight() or self.minimumWidth() != MIN_WIDTH:
            self.setMinimumSize(MIN_WIDTH, floor)

    def resizeEvent(self, ev):                 # noqa: N802
        """A narrower window wraps its rows, so its floor is higher.

        Recomputed here rather than only when the pane is folded, because the
        rows' height-for-width is a function of the width and the floor has to
        follow it: a window dragged narrow enough that the rows no longer fit
        stops there instead of cutting them off.
        """
        super().resizeEvent(ev)
        self._update_floor()

    def _select_data(self, combo, value, default_index: int = 0) -> None:
        """Select the entry holding ``value``, or fall back to the default.

        Anything the list does not offer is ignored rather than added: a
        remembered value out of range is not a value the user can have chosen
        here (the receiver's rate list does add one, because a rate can come
        from the command line; a zoom of 5x cannot come from anywhere).
        """
        idx = combo.findData(value)
        combo.setCurrentIndex(idx if idx >= 0 else default_index)

    def _apply_saved_ui(self):
        """Bring the window's own controls into line with the stored state."""
        p = self.prefs
        if p is None:
            return
        # The color scheme is settled in __init__ -- the command line has to
        # have its say before a single row is built -- so this is only the
        # list being brought into line with the scheme already in use.
        self._use_theme(self.theme_name, remember=False)
        self._select_data(self.cmb_theme, self.theme_name)
        self.settings_pane.set_folded(
            p.value("ui", "settings_folded", "flag", (), False), notify=False)
        self._select_data(self.cmb_step, p.value("ui", "step", "int",
                                                 (1, 1_000_000), self._step))
        self._select_data(self.cmb_zoom, p.value("ui", "zoom", "int",
                                                 (1, ZOOM_MAX), self._zoom))
        self.chk_wfauto.setChecked(p.value("ui", "wf_auto", "flag", (), True))
        self.spn_wflo.setValue(p.value("ui", "wf_floor", "number",
                                       (-160.0, 20.0), self.spn_wflo.value()))
        self.spn_wfhi.setValue(p.value("ui", "wf_ceiling", "number",
                                       (-160.0, 40.0), self.spn_wfhi.value()))
        self.spn_contrast.setValue(p.value("ui", "wf_contrast", "number",
                                           (6.0, 90.0),
                                           self.spn_contrast.value()))
        self._select_data(self.cmb_cmap,
                          p.value("ui", "wf_cmap", "text", (24,), None))
        self.chk_cmrev.setChecked(p.value("ui", "wf_reverse", "flag", (), False))
        detail = p.value("ui", "wf_detail", "int", (0, 100_000), None)
        if detail is not None:
            self._select_data(self.cmb_wfdetail, detail)
        # Quietly: the band's dial was remembered as well, and the frequency is
        # the better record of where the user actually was -- a band name is a
        # label for a preset, and re-selecting it out loud would tune the box to
        # the preset's dial over the frequency they left the receiver on.
        self.spn_sref.setValue(p.value("ui", "s_meter_ref", "number",
                                       (-140.0, 0.0), smeter.DEFAULT_REF_DBFS))
        self.chk_speak.setChecked(p.value("ui", "s_meter_peak", "flag", (), True))
        self._select_data(self.cmb_sface,
                          p.value("ui", "s_meter_face", "choice",
                                  smeter.FACE_NAMES, None))

        band = p.value("ui", "ft8_band", "text", (12,), None)
        if band is not None:
            idx = self.cmb_ft8band.findData(band)
            if idx >= 0:
                self.cmb_ft8band.blockSignals(True)
                self.cmb_ft8band.setCurrentIndex(idx)
                self.cmb_ft8band.blockSignals(False)
        self.chk_ft8every.setChecked(p.value("ui", "ft8_every", "flag", (), True))
        # The levels have just been set by hand; with auto on, the picture now
        # tracks the noise floor and takes the spinners over (see
        # _sync_wf_levels), which is the state the user left it in.
        self._wf_settings_changed()

    def _restore_window_state(self):
        """Put the window back where and how big it was, if that still exists.

        The place and the size are decided separately.  A remembered *place* is
        only used when it lands on a screen that is connected *now*: monitors
        are unplugged and resolutions change, and a window restored off the edge
        of the world is worse than one that opens in the middle of the display.
        The size is the window's own and does not depend on the monitor, so it
        comes back either way -- never larger than the screen it has to fit on,
        or a rectangle remembered from a bigger display would open with its
        controls off the edges.
        """
        p = self.prefs
        if p is None:
            return
        self.wants_maximised = p.value("window", "maximised", "flag", (), False)
        w = p.value("window", "w", "int", (400, 20_000), None)
        h = p.value("window", "h", "int", (300, 20_000), None)
        x = p.value("window", "x", "int", (-20_000, 20_000), None)
        y = p.value("window", "y", "int", (-20_000, 20_000), None)
        if w is not None and h is not None:
            area = max((s.availableGeometry() for s in QApplication.screens()),
                       key=lambda r: r.width() * r.height(), default=None)
            if area is not None:
                w, h = min(w, area.width()), min(h, area.height())
            self.resize(max(int(w), self.minimumWidth()),
                        max(int(h), self.minimumHeight()))
        if None in (x, y, w, h):
            return
        # x, y are the frame's corner and w, h the client size, which is how
        # they were written down (see _remember).
        want = QRect(x, y, w, h)
        for screen in QApplication.screens():
            seen = screen.availableGeometry().intersected(want)
            if seen.width() >= 120 and seen.height() >= 60:
                self.move(x, y)
                return
        # Nowhere on screen: keep the centred default from _size_to_screen.

    def showEvent(self, ev):
        """Come back maximised, if that is how the window was left.

        Here rather than in __init__ because whether a window is maximised is
        the window manager's business and only means anything once the window
        is on screen -- and here rather than in the caller because every caller
        (the program, a test, the screenshot run) then gets the same window.
        """
        super().showEvent(ev)
        if self._shown or not self.wants_maximised:
            return
        self._shown = True              # before the call: it shows us again
        self.showMaximized()

    def _restore_split(self):
        """Put the divider between the spectrum and the waterfall back."""
        if self.prefs is None:
            return
        sizes = [self.prefs.value("ui", "split_spectrum", "int", (20, 20_000),
                                  None),
                 self.prefs.value("ui", "split_waterfall", "int", (20, 20_000),
                                  None)]
        if None not in sizes:
            self.split.setSizes(sizes)

    def _remember_soon(self):
        """Remember the current state, a moment after it stops changing."""
        if self.prefs is None or self._building:
            return
        self._prefs_timer.start()

    def _remember(self):
        """Write everything worth remembering.  Called on change, and on close."""
        p = self.prefs
        if p is None:
            return
        prefs.remember_settings(p, self._settings_as_meant())
        p.put("ui", "step", int(self._step))
        p.put("ui", "zoom", int(self._zoom))
        p.put("ui", "wf_auto", bool(self.chk_wfauto.isChecked()))
        p.put("ui", "wf_floor", float(self.spn_wflo.value()))
        p.put("ui", "wf_ceiling", float(self.spn_wfhi.value()))
        p.put("ui", "wf_contrast", float(self.spn_contrast.value()))
        p.put("ui", "wf_cmap", str(self.cmb_cmap.currentData()))
        p.put("ui", "wf_reverse", bool(self.chk_cmrev.isChecked()))
        p.put("ui", "wf_detail", int(self.cmb_wfdetail.currentData()))
        p.put("ui", "ft8_band", str(self.cmb_ft8band.currentData() or ""))
        p.put("ui", "s_meter_ref", float(self.spn_sref.value()))
        p.put("ui", "s_meter_peak", bool(self.chk_speak.isChecked()))
        p.put("ui", "s_meter_face", str(self.cmb_sface.currentData()))
        p.put("ui", "theme", str(self.theme_name))
        p.put("ui", "settings_folded", bool(self.settings_pane.is_folded()))
        p.put("ui", "ft8_every", bool(self.chk_ft8every.isChecked()))
        sizes = self.split.sizes()
        if len(sizes) == 2:
            p.put("ui", "split_spectrum", int(sizes[0]))
            p.put("ui", "split_waterfall", int(sizes[1]))
        # The *normal* geometry, not the maximised one: the size to come back to
        # is the one the user chose, and maximised is remembered separately.
        # And the position is the *frame's*, the size the client's, because that
        # is what the two calls on the way back in take: move() places a
        # top-level window's frame, resize() sets the client area.  Saving the
        # client position instead -- which is geometry() and sits a title bar
        # below the frame -- walked the window down the screen by the height of
        # its own decoration on every restart.
        frame, rect = self.frameGeometry(), self.geometry()
        dx, dy = frame.x() - rect.x(), frame.y() - rect.y()
        if self.isMaximized() or self.isFullScreen():
            rect = self.normalGeometry()
        for key, value in (("x", rect.x() + dx), ("y", rect.y() + dy),
                           ("w", rect.width()), ("h", rect.height())):
            p.put("window", key, int(value))
        p.put("window", "maximised", bool(self.isMaximized()))
        try:
            p.save()
        except OSError as exc:
            # Worth saying, never worth interrupting for: the settings file is a
            # convenience, and the tuning in front of the user is not.
            self._on_error(f"could not write {p.path}: {exc}")

    def _settings_as_meant(self) -> Settings:
        """The receiver settings as the *user* left them, not as FT8 borrows them.

        FT8 switches noise reduction, audio AGC and the squelch off while it
        listens (see :meth:`_ft8_prepare`), and those are three of the settings
        that get written down.  Writing the borrowed values would mean closing
        the window while in FT8 quietly cost the user their listening settings
        for every other mode -- so a borrowed value that is still exactly what
        FT8 forced is written down as the value it was borrowed from.  One the
        user has changed since is theirs, and is written as it stands.
        """
        settings = self.settings
        for name, (before, forced) in self._ft8_borrowed.items():
            if float(getattr(settings, name)) != float(forced):
                continue
            settings = settings.copy_with(**{name: before})
        return settings

    # ------------------------------------------------------ actions
    def _device_label(self) -> str:
        devs = list_devices()
        if not devs:
            return "no SDR found"
        return devs[0]["label"]

    def _refresh_devices(self):
        try:
            self.devices = list_devices()
        except Exception as exc:
            self.devices = []
            self._on_error(str(exc))

    def _current_mode(self) -> str:
        for key, btn in self.mode_buttons.items():
            if btn.isChecked():
                return key
        return self.settings.mode

    def _toggle(self):
        if self.radio.opened:
            self.opener.cancel()
            self.radio.close()
            self._set_running_ui(False)
        else:
            self.start_radio()

    def start_radio(self) -> bool:
        """Open the stream if it is not already running.

        A device that is merely busy is waited for rather than reported: the
        opener keeps asking, so the window starts by itself as soon as whatever
        was holding the radio lets go (see DeviceOpener).
        """
        if self.radio.opened:
            return True
        ok = self.opener.start()
        self._set_running_ui(self.radio.opened)
        return ok

    def _device_opened(self):
        """The opener got the device -- after a retry, or on the first try."""
        self._refresh_devices()
        self.lbl_device.setText(self._device_label())
        self._set_running_ui(True)
        # Only shown until the first status readout, which takes this line over
        # within a frame.  It has to be written, though: otherwise a start that
        # succeeded on the fourth retry still had "trying again in 5 s" on the
        # status bar, and the bar was the one place that said it had not worked.
        self._on_status_line("streaming")

    def _device_waiting(self, why: str, delay: float):
        self.lbl_device.setText("SDRplay not available -- waiting")
        self._on_status_line(
            f"the SDRplay device is busy or unplugged ({why}) -- trying again in "
            f"{delay:.0f} s")

    def _device_gave_up(self, why: str):
        self._on_error(why)
        self._on_status_line(
            f"could not open the RSP1: {why} -- press Start to try again")

    def _device_lost(self, why: str):
        """The stream stopped mid-session: unplugged, or the driver died.

        Radio._lost has already stopped both of its threads and left the device
        for this thread to release.  The receiver is closed and then looked for
        again with the same retry used at startup, so an RSP1 that was unplugged
        -- or whose service restarted -- comes back on its own when it is
        available again, instead of leaving a frozen waterfall and a Stop button
        that stops nothing.
        """
        if self._closing or not self.radio.opened:
            return
        self.opener.cancel()
        self.radio.close()
        self._set_running_ui(False)
        self.lbl_device.setText("SDRplay gone -- looking for it again")
        self._on_status_line(
            f"the RSP1 stopped streaming ({why}) -- closing it and looking again")
        self.opener.start()

    def autostart(self):
        """Open the radio as soon as the window is up."""
        if self.start_radio() or self.opener.waiting_for_device:
            return
        self._on_status_line(
            "could not open the RSP1 -- check it is plugged in and the "
            "sdrplay service is running, then press Start")

    def _set_running_ui(self, running: bool):
        self._ui_running = bool(running)
        self.btn_start.setText("■  Stop" if running else "▶  Start")
        self.btn_start.setStyleSheet(self._start_button_style(running))

    def _apply(self, **kw):
        if self._building:
            return
        self.settings = self.settings.copy_with(**kw)
        try:
            self.radio.apply(**kw)
        except Exception as exc:
            # Changing the rate or the device arguments restarts the stream, and
            # that can fail if another program took the device meanwhile.  Same
            # recovery as startup, instead of a traceback out of a Qt slot.
            self._set_running_ui(False)
            self._on_error(str(exc))
            self.opener.start()
        # Every receiver setting the user can change comes through here, so this
        # is the one place that has to remember them as they change.
        self._remember_soon()

    def _rate_changed(self):
        rate = int(self.cmb_rate.currentData())
        self._apply(sample_rate=rate)
        # The visible window is the sample rate divided by the zoom, so the
        # readout has to follow a rate change too.
        self._update_zoom_readout()
        if self.radio.opened:
            self._on_status_line("restarting at new rate…")

    def _freq_changed(self, mhz: float):
        if self._building:
            return
        f = mhz * 1e6
        self.radio.set_tuned(f)
        self.settings = self.settings.copy_with(tuned_freq=f)
        self._maybe_recentre(f)

    def _center_changed(self, mhz: float):
        if self._building:
            return
        # Keep our own mirror in step with the hardware, so the settings the
        # window reports (and saves into a memory channel) are the real ones.
        self.settings = self.settings.copy_with(center_freq=mhz * 1e6)
        self.radio.set_center(mhz * 1e6)

    def _step_changed(self):
        self._step = int(self.cmb_step.currentData())
        self.spn_freq.setSingleStep(self._step / 1e6)
        self._remember_soon()

    def _tune(self, freq: float):
        """Follow the pointer: a click or drag on one of the plots."""
        self.spn_freq.setValue(freq / 1e6)
        self._maybe_recentre(freq)

    def _tune_settled(self, freq: float):
        """The gesture is over -- only now may the window follow the dial.

        The plot clears its own dragging flag before emitting this, so the
        recentre that _tune deliberately skipped while the pointer was down
        happens once, at the end, instead of fighting the drag.
        """
        self.spn_freq.setValue(freq / 1e6)
        self._maybe_recentre(freq)

    def _pointer_down(self) -> bool:
        """Is a click or drag in progress on either plot?"""
        return bool(self.spectrum._dragging or self.waterfall._dragging)

    def _maybe_recentre(self, freq: float):
        # Never mid-gesture: the picture moves when the receiver recentres, so
        # the frequency under the pointer would change while the user is still
        # holding it.  A drag across the recentre threshold used to tune in
        # steps of half a window; now the window moves once, on release.
        if self._pointer_down():
            return
        # The zoom can make the visible window far narrower than the receiver
        # span, so the limit follows the window, not the span.
        #
        # Compare against the centre we last *asked* the receiver for, which
        # set_center() records at once, and not against status.center -- that
        # is the acknowledged centre and the DSP thread only republishes it
        # every 0.4 s.  Deciding on it is how a signal ends up outside the
        # visible window: in a burst of tunes (clicking through memories) it
        # is several retunes out of date, so a frequency that is really far
        # away looks like it is already in view and the view never follows.
        #
        # The drawn window lags a command for one frame, which is harmless:
        # the wait is the retune reaching the reader thread (~ms) and the next
        # repaint (~45 ms).
        half = float(self.settings.sample_rate) / (2.0 * max(1, self._zoom))
        centre = self.radio.settings.center_freq
        if abs(freq - centre) > 0.9 * half:
            self.spn_center.setValue(freq / 1e6)

    def _recentre(self):
        self.spn_center.setValue(self.settings.tuned_freq / 1e6)

    def _recentre_to(self, freq: float):
        self.spn_center.setValue(freq / 1e6)

    def _step_tune(self, direction: int):
        f = self.settings.tuned_freq + direction * self._step
        self.spn_freq.setValue(max(f, 10000.0) / 1e6)

    def _band_selected(self, idx: int):
        bd = self.cmb_band.itemData(idx)
        if bd is None:
            return
        self.spn_freq.setValue(bd.centre / 1e6)
        self.spn_center.setValue(bd.centre / 1e6)
        self._select_mode(bd.mode)
        self.cmb_band.setCurrentIndex(0)

    def _preset_selected(self, idx: int):
        p = self.cmb_preset.itemData(idx)
        if p is None:
            return
        self.spn_freq.setValue(p.freq / 1e6)
        self.spn_center.setValue(p.freq / 1e6)
        self._select_mode(p.mode)
        self.cmb_preset.setCurrentIndex(0)

    def _select_mode(self, key: str):
        for k, btn in self.mode_buttons.items():
            btn.setChecked(k == key)
        self._select_bw(MODE_BW.get(key, 2400.0))
        # The demodulator is never told "ft8" -- it is USB that is heard, with
        # the decode behind it -- but the *window* remembers the mode as FT8,
        # so that a memory channel saved in this mode comes back in it.
        self._apply(mode=ft8.receiver_mode(key),
                    filter_bw=float(self.cmb_bw.currentData()))
        self.settings = self.settings.copy_with(mode=key)
        self._ft8_mode(key == "ft8")

    def _mode_selected(self, key: str):
        self._select_mode(key)

    def _select_bw(self, bw: float):
        best = 0
        for i in range(self.cmb_bw.count()):
            if abs(float(self.cmb_bw.itemData(i)) - bw) < abs(
                    float(self.cmb_bw.itemData(best)) - bw):
                best = i
        self.cmb_bw.setCurrentIndex(best)

    def _bw_changed(self):
        self._apply(filter_bw=float(self.cmb_bw.currentData()))

    def _volume_changed(self, v: int):
        if self.radio.audio is not None:
            self.radio.audio.volume = v / 100.0
        self._apply(volume=v / 100.0)

    def _mute_changed(self, m: bool):
        if self.radio.audio is not None:
            self.radio.audio.muted = m

    def _agc_changed(self, on: bool):
        self.sld_if.setEnabled(not on)
        self._apply(rf_agc=bool(on))

    def _if_gain_changed(self, v: int):
        self.lbl_if.setText(f"{v} dB")
        self._apply(if_gain=float(v))

    # -- the signal meter --------------------------------------------
    def _update_meter(self):
        """Give the meter the level the receiver is measuring.

        Read straight from the demodulator rather than from the status signal: a
        needle needs about twenty samples a second, and the status bar's own
        readout is published every 0.4 s.  The value is one float written by
        the DSP thread once per hop and simply read here, which needs no lock --
        and the passband level, not the audio, so the needle does not follow the
        volume control and does not need the sound card to be working.
        """
        demod = getattr(self.radio, "demod", None)
        now = time.perf_counter()
        dt = 0.0 if self._meter_t is None else min(now - self._meter_t, 0.5)
        self._meter_t = now
        # A stopped receiver feeds the needle *nothing* rather than the last
        # level it measured.  The demodulator survives being closed, so its
        # passband level stands at whatever was being heard when the stream
        # stopped -- and a needle frozen at the last signal is worse than no
        # reading at all: it looks like the receiver is still listening.  Fed
        # silence instead, the needle falls away and settles at its rest, which
        # is what the meter of a set that has been switched off does.
        dbfs = (demod.passband_db
                if demod is not None and self.radio.opened else -200.0)
        self.smeter.feed(dbfs, dt)
        reading = self.smeter.reading_text()
        if reading != self._meter_text:
            self._meter_text = reading
            level = self.smeter.level_dbm()
            self.lbl_smeter.setText(
                f"{reading}   {level:.0f} dBm\n{self.smeter.level_dbfs:.1f} dBFS"
                if reading != "--" else "--   no signal reading\n(radio stopped)")

    def _smeter_ref_changed(self, v: float):
        self.smeter.set_ref(float(v))
        self._remember_soon()

    def _smeter_peak_changed(self, on: bool):
        self.smeter.set_peak(bool(on))
        self._remember_soon()

    def _smeter_face_changed(self, *_):
        self.smeter.set_face(str(self.cmb_sface.currentData()))
        self._remember_soon()

    def _wf_settings_changed(self, *_):
        auto = self.chk_wfauto.isChecked()
        self.spn_contrast.setEnabled(auto)
        self._remember_soon()
        self.spn_wflo.setEnabled(not auto)
        self.spn_wfhi.setEnabled(not auto)
        self.waterfall.set_levels(auto, float(self.spn_contrast.value()),
                                  float(self.spn_wflo.value()),
                                  float(self.spn_wfhi.value()))

    def _wf_colour_changed(self, *_):
        key = self.cmb_cmap.currentData()
        self.waterfall.set_colormap(str(key), self.chk_cmrev.isChecked())
        self._remember_soon()

    def _wf_detail_changed(self, *_):
        self.waterfall.set_detail(int(self.cmb_wfdetail.currentData()))
        self._wf_detail_readout()
        self._remember_soon()

    def _wf_detail_readout(self):
        """Say what the picture's resolution actually is, in Hz per column.

        A column count on its own does not tell anyone anything: what matters
        is whether the FFT offers more detail than the picture can hold, which
        is the ratio of bins on offer to columns drawn.  SDR++ shows the same
        thing beside its FFT-size control.
        """
        wf = self.waterfall
        span = float(self.settings.sample_rate) / max(1, self._zoom)
        per_col = span / max(wf.cols(), 1)
        if wf.bins() > wf.cols():
            detail = f", {wf.bins():,} bins on offer"
        else:
            # Fewer bins than the plot has pixels: one column is one bin, and
            # the picture is as fine as the transform can make it.  That is
            # what a deep zoom looks like until the FFT size goes up, so it is
            # worth saying rather than leaving the columns to look coarse for
            # no reason.
            detail = ", one column per FFT bin"
        self._on_status_line(
            f"waterfall detail {self.cmb_wfdetail.currentText()} -- "
            f"{wf.cols():,} columns x {wf.rows()} rows, "
            f"{per_col/1e3:.2f} kHz per column{detail}")

    def _zoom_step(self, direction: int):
        """One step along the zoom ladder -- the + / - buttons and the wheel."""
        i = self.cmb_zoom.currentIndex() + direction
        i = max(0, min(i, self.cmb_zoom.count() - 1))
        if i == self.cmb_zoom.currentIndex():
            # Already at the end of the ladder; say so rather than doing
            # nothing, or the button looks broken.
            self._on_status_line("zoom %dx is the limit" % self._zoom)
            return
        self.cmb_zoom.setCurrentIndex(i)

    def _zoom_fit(self):
        self.cmb_zoom.setCurrentIndex(0)

    def _update_zoom_readout(self):
        """How much frequency is across the window, at the zoom in force.

        Three units, because the ladder now spans three orders of magnitude:
        megahertz at the wide end, kilohertz through the middle, and hertz once
        the window is narrower than a filter -- at the deepest step and the
        lowest rate that is 244 Hz, which "0 kHz" would have called nothing.
        """
        span = float(self.settings.sample_rate) / max(1, self._zoom)
        if span >= 1e6:
            text = f"{span/1e6:g} MHz"
        elif span >= 1e4:
            text = f"{span/1e3:,.0f} kHz"
        elif span >= 1e3:
            text = f"{span/1e3:.2f} kHz"
        else:
            text = f"{span:.0f} Hz"
        self.lbl_zoom.setText(text)

    def _zoom_changed(self, *_):
        self._zoom = int(self.cmb_zoom.currentData())
        # Rows already on screen were pooled for a different span, so they no
        # longer mean what the level scale says; start the picture again.
        self.waterfall.clear_history()
        self._update_zoom_readout()
        self._maybe_recentre(self.settings.tuned_freq)
        self._on_status_line(f"zoom {self._zoom}x -- " +
                             self.lbl_zoom.text() + " across the window")
        self._remember_soon()

    def _sync_wf_levels(self):
        """Show the tracked levels while auto is on, so hand-over is seamless."""
        if not self.chk_wfauto.isChecked():
            return
        lo, hi = self.waterfall.levels()
        for spin, val in ((self.spn_wflo, lo), (self.spn_wfhi, hi)):
            if abs(spin.value() - val) > 0.4:
                spin.blockSignals(True)
                spin.setValue(round(val, 1))
                spin.blockSignals(False)

    def _nr_toggled(self, on: bool):
        self.sld_nr.setEnabled(on)
        self._apply(nr=bool(on))

    def _nr_level_changed(self, v: int):
        self.lbl_nr.setText(f"{v}%")
        self._apply(nr_level=float(v))

    # ----------------------------------------------------------------- FT8
    # The decoder is armed by selecting the mode and disarmed by leaving it;
    # everything below reads what it reports.  Two things about its shape are
    # worth knowing.  The audio is the slot clock -- the spotter measures 15 s
    # of samples, not 15 s of wall time -- so the countdown here is a display of
    # the spotter's own state, never a second source of truth for it.  And a
    # decode is a row that is also a tuning button, because the frequency a
    # message was heard on is the only thing about it worth acting on.

    def _ft8_mode(self, on: bool):
        """Entering or leaving FT8: show the strip, set the radio up, arm."""
        self.ft8_panel.setVisible(on)
        self._update_floor()        # the strip is part of the window's height
        if not on:
            self._ft8_timer.stop()
            self.spotter.disarm()
            self._ft8_restore()
            return
        self.spotter.set_dial(self.settings.tuned_freq)
        self._ft8_prepare()
        self.spotter.arm()
        self._ft8_timer.start()
        self._ft8_tick()
        if not self.spotter.decoder.available:
            self._ft8_error(self.spotter.decoder.describe())

    def _ft8_prepare(self):
        """The receiver settings a decoder wants, applied on entering FT8.

        Noise reduction, audio AGC and a squelch are all subtract-from-the-
        signal operations, and each of them damages a Costas sync or the soft
        bits beside it.  jt9 estimates its own noise floor, so none of them has
        anything to offer a decoder; what it wants is the passband left alone.
        These are exactly the settings a user would have to remember to switch
        off, which is why selecting the mode does it for them -- and why they are
        put back on the way out (:meth:`_ft8_restore`): borrowing a setting must
        not be the same thing as silently changing it for good.
        """
        self._ft8_borrowed = {
            "nr": (self.chk_nr.isChecked(), False),
            "audio_agc": (self.chk_aagc.isChecked(), False),
            "squelch_db": (self.spn_sq.value(), FT8_SQUELCH_DB),
        }
        if self.chk_nr.isChecked():
            self.chk_nr.setChecked(False)       # → _nr_toggled → _apply(nr=False)
        if self.chk_aagc.isChecked():
            self.chk_aagc.setChecked(False)
        if self.spn_sq.value() > FT8_SQUELCH_DB:
            self.spn_sq.setValue(FT8_SQUELCH_DB)
        self._select_bw(ft8.FILTER_BW)          # → _bw_changed → _apply(filter_bw)

    def _ft8_restore(self):
        """Put back the listening settings FT8 switched off while it listened.

        Only the ones still sitting at what FT8 forced: a value the user changed
        *while* in the mode is theirs now, and restoring over the top of it would
        undo the last thing they did.  A setting that was already where FT8
        wanted it is recorded as such and restored to the same value, so this is
        the same statement either way.
        """
        borrowed, self._ft8_borrowed = self._ft8_borrowed, {}
        widgets = {"nr": self.chk_nr, "audio_agc": self.chk_aagc,
                   "squelch_db": self.spn_sq}
        for name, (before, forced) in borrowed.items():
            widget = widgets[name]
            now = (widget.value() if name == "squelch_db"
                   else widget.isChecked())
            if float(now) != float(forced):
                continue
            if name == "squelch_db":
                widget.setValue(float(before))
            else:
                widget.setChecked(bool(before))

    def _ft8_band_selected(self):
        """A preset band was chosen: tune the receiver to its dial frequency."""
        name = self.cmb_ft8band.currentData()
        dial = ft8.dial_for(name) if name else None
        if dial is None:
            return
        # Through the frequency box rather than straight at the radio, so the
        # readout, the recentring and the receiver all move together, and a
        # memory saved now is the band the user is actually on.  The *audio*
        # of the band sits above this dial, which is the whole convention.
        self.spn_freq.setValue(dial / 1e6)
        self.spotter.set_dial(dial)
        self._remember_soon()
        self._on_status_line(
            f"FT8 {name}: dial {dial/1e6:.3f} MHz, signals "
            f"{ft8.LOW_HZ/1000:.1f}-{ft8.HIGH_HZ/1000:.1f} kHz above it")

    def _ft8_every_changed(self, on: bool):
        """Decode every slot, or just the next one."""
        if not on and self.spotter.armed:
            self._ft8_once = True
        self._ft8_tick()
        self._remember_soon()

    def _ft8_clear(self):
        self.tbl_ft8.setRowCount(0)
        self._ft8_tick()

    def _ft8_tune_to_row(self, row: int, _col: int):
        """Click a decode: tune to the frequency it was heard on."""
        item = self.tbl_ft8.item(row, len(FT8_COLS) - 1)
        d = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if d is None:
            return
        self.spn_freq.setValue(d.freq / 1e6)
        self._on_status_line(f"{d.message}   {d.freq/1e6:.6f} MHz   "
                             f"{d.snr:+d} dB   dt {d.dt:+.1f} s")

    def _ft8_add_decodes(self, decodes, _when):
        """A slot has been decoded.  Arrives on the GUI thread, via the bridge."""
        for d in sorted(decodes, key=lambda x: x.audio_hz):
            row = self.tbl_ft8.rowCount()
            self.tbl_ft8.insertRow(row)
            fields = (d.time, f"{d.snr:+d}", f"{d.dt:+.1f}",
                      str(d.audio_hz), d.message)
            for col, text in enumerate(fields):
                item = QTableWidgetItem(text)
                if col < len(FT8_COLS) - 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                else:
                    item.setData(Qt.ItemDataRole.UserRole, d)
                    item.setToolTip(
                        f"{d.freq/1e6:.6f} MHz -- {d.audio_hz} Hz above the "
                        f"{d.dial_hz/1e6:.3f} MHz dial\n"
                        "Click to tune to it")
                self.tbl_ft8.setItem(row, col, item)
        extra = self.tbl_ft8.rowCount() - FT8_KEEP
        if extra > 0:
            # An operator's log, not a recording: the oldest lines go, so a
            # night's listening cannot grow the window's memory without bound.
            for _ in range(extra):
                self.tbl_ft8.removeRow(0)
        if decodes:
            self.tbl_ft8.scrollToBottom()
            first = decodes[0]
            self._on_status_line(
                f"FT8 {len(decodes)} decode(s) at {first.time} -- "
                f"{first.message} ({first.freq/1e6:.6f} MHz)")
        if self._ft8_once:
            # One slot was asked for, and it has been decoded and shown.
            self._ft8_once = False
            self.spotter.disarm()
        self._ft8_tick()

    def _ft8_spotter_says(self, text: str):
        """The decoder's own account of itself: a slot, totals, a failure."""
        self._ft8_status_text = text
        self._ft8_tick()

    def _ft8_error(self, text: str):
        self._ft8_status_text = text
        self._on_status_line(f"FT8: {text}")
        self._ft8_tick()

    def _ft8_tick(self):
        """Redraw the strip: the slot clock, the state, and the totals."""
        if not self.ft8_panel.isVisible():
            return
        st = self.spotter.stats()
        filling, secs = self.spotter.progress()
        if not self.spotter.armed:
            clock = "idle"
        elif st["queued"]:
            clock = "decoding…"
        elif filling:
            start = self.spotter.slot_start()
            clock = (f"slot {ft8.slot_label(start)}  "
                     f"{secs:4.1f}/{ft8.SLOT_SECONDS:.0f} s")
        elif self.spotter.idle_seconds() > 2.0:
            # Armed, but nothing is arriving: the receiver is stopped, or the
            # device was lost (see _device_lost).  Saying so beats a countdown
            # that never gets anywhere.
            clock = "waiting for the receiver"
        else:
            clock = f"next slot in {ft8.slots_until(time.time()):4.1f} s"
        bits = [clock]
        if self.spotter.armed:
            bits.append("this slot only" if self._ft8_once else "every slot")
        if st["slots"]:
            bits.append(f"{st['slots']} slot(s)")
        # The messages *shown*, not the messages ever decoded: the list can have
        # been cleared, and a total that survived the Clear button would read as
        # a stuck readout.
        rows = self.tbl_ft8.rowCount()
        if rows:
            bits.append(f"{rows} message(s)")
        if st["dropped"]:
            bits.append(f"{st['dropped']} dropped")
        if st["error"]:
            bits.append(st["error"])
        text = "   ".join(bits)
        if text != self.lbl_ft8.text():
            self.lbl_ft8.setText(text)
        # The colour follows what the readout is saying, so it is not in the
        # theme's tint list: an error is the theme's error colour, and a
        # healthy clock its accent.  _recolour puts this back by hand.
        self.lbl_ft8.setStyleSheet(
            "color:" + (self.theme["error"] if st["error"]
                        else self.theme["accent"]))
        self.lbl_ft8.setToolTip(self._ft8_status_text
                                or "Slot clock, decoder state, and totals")

    def _on_status_line(self, text: str):
        self.lbl_status.setText(text)

    # --------------------------------------------------- memory channels
    def _mem_refresh(self, select_name: str | None = None):
        """Rebuild the memory list from the store."""
        self.cmb_mem.blockSignals(True)
        try:
            self.cmb_mem.clear()
            for ch in self.memory:
                self.cmb_mem.addItem(
                    f"{ch.name}   {ch.freq/1e6:,.6f}  {ch.mode.upper()}", ch)
            if select_name is not None:
                for i in range(self.cmb_mem.count()):
                    if getattr(self.cmb_mem.itemData(i), "name", None) == select_name:
                        self.cmb_mem.setCurrentIndex(i)
                        break
        finally:
            self.cmb_mem.blockSignals(False)

        n = len(self.memory)
        self.lbl_mem.setText(f"{n} saved" if n else "none saved yet")
        self.cmb_mem.setEnabled(n > 0)
        for b in (self.btn_mem_recall, self.btn_mem_update, self.btn_mem_delete):
            b.setEnabled(n > 0)

    def _current_channel(self) -> Channel:
        """The settings on screen right now, as a channel."""
        return Channel(name="", freq=self.settings.tuned_freq,
                       mode=self._current_mode(),
                       filter_bw=float(self.cmb_bw.currentData()),
                       cw_pitch=float(self.spn_pitch.value()))

    def _mem_default_name(self) -> str:
        freq = self.settings.tuned_freq
        bd = bands.find_band(freq)
        tail = f"{freq/1e6:.3f}"
        return f"{bd.name} {tail}" if bd else tail

    def _mem_save(self):
        name, ok = QInputDialog.getText(self, "Save memory channel", "Name:",
                                        text=self._mem_default_name())
        if not ok or not name.strip():
            return
        ch = self._current_channel()
        ch.name = name.strip()
        if not self._mem_write(ch):
            return
        self._on_status_line(f"saved memory “{ch.name}”  "
                             f"{ch.freq/1e6:.6f} MHz {ch.mode.upper()}")

    def _mem_update(self):
        old = self.cmb_mem.currentData()
        if old is None:
            return
        ch = self._current_channel()
        ch.name = old.name
        if not self._mem_write(ch):
            return
        self._on_status_line(f"updated memory “{ch.name}”")

    def _mem_write(self, ch: Channel) -> bool:
        try:
            self.memory.save_channel(ch)
        except OSError as exc:
            self._on_error(f"could not write {self.memory.path}: {exc}")
            return False
        self._mem_refresh(select_name=ch.name)
        return True

    def _mem_delete(self):
        ch = self.cmb_mem.currentData()
        if ch is None:
            return
        idx = self.cmb_mem.currentIndex()
        answer = QMessageBox.question(self, "Delete memory",
                                      f"Delete “{ch.name}”?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.memory.remove(idx)
        except (IndexError, OSError) as exc:
            self._on_error(f"could not delete: {exc}")
            return
        self._mem_refresh()
        self._on_status_line(f"deleted memory “{ch.name}”")

    def _mem_recall(self):
        ch = self.cmb_mem.currentData()
        if ch is None:
            return
        # Order matters: _select_mode picks the default filter width for the
        # mode, so the channel's own width has to be applied afterwards.
        self.spn_pitch.setValue(ch.cw_pitch)
        self._select_mode(ch.mode)
        self._select_bw(ch.filter_bw)
        self.spn_freq.setValue(ch.freq / 1e6)
        self.spn_center.setValue(ch.freq / 1e6)
        self._on_status_line(f"recalled “{ch.name}”  "
                             f"{ch.freq/1e6:.6f} MHz {ch.mode.upper()}")

    # --------------------------------------------------- radio data
    def _on_spectrum(self, center: float, db, span: float):
        # Keep only the newest frame so a slow GUI can never build a backlog.
        self._latest = (center, db, span)

    def _view(self, center: float, span: float) -> tuple[float, float]:
        """The visible frequency window: the receiver span divided by the zoom."""
        half = span / (2.0 * max(1, self._zoom))
        return center - half, center + half

    def _render(self):
        # Before the spectrum check: the meter is fed from the DSP rather than
        # from the display, and a frame the window has nothing new to draw is no
        # reason for the needle to stop moving.
        self._update_meter()
        if self._latest is None:
            return
        center, db, span = self._latest
        self._latest = None
        t0 = time.perf_counter()
        lo, hi = self._view(center, span)
        self.spectrum.update_spectrum(center, db, span, lo, hi)
        self.waterfall.push(center, db, span, lo, hi)
        self.spectrum.set_view(lo, hi)
        self.waterfall.set_view(lo, hi)
        self.spectrum.set_marker(self.settings.tuned_freq, self.settings.filter_bw,
                                 self.settings.mode, self.settings.cw_pitch)
        self._sync_wf_levels()
        dt = time.perf_counter() - t0
        self.render_seconds = 0.9 * self.render_seconds + 0.1 * dt
        self.frames_drawn += 1

    def _on_status(self, st):
        # Follow the radio's own state rather than only spotting the "stopped"
        # one.  Changing the sample rate restarts the stream, and the close()
        # half of that restart told the button the radio was down while the
        # open() half never told it the radio was back -- so the button offered
        # to "Start" a receiver that was already streaming.
        if bool(getattr(st, "running", False)) != self._ui_running:
            self._set_running_ui(bool(st.running))
        self._on_status_line(
            f"{st.sample_rate/1e6:g} MSps   "
            f"centre {st.center/1e6:.6f}   "
            f"tuned {st.tuned/1e6:.6f} MHz   "
            f"RF {st.rx_dbm:6.1f} dB   audio {st.audio_db:6.1f} dB   "
            f"cpu {st.cpu_load*100:4.1f}%   "
            f"lag {st.lag*1000:3.0f} ms   "
            f"{'dropped ' + str(st.dropped_chunks) if st.dropped_chunks else 'clean'}"
            + (f"   [{st.error}]" if st.error else ""))
    def _on_error(self, msg: str):
        self._on_status_line("error: " + msg)

    def closeEvent(self, ev):
        # Before anything else: a device that fails while the window is going
        # away must not start looking for it again.
        self._closing = True
        self._timer.stop()
        self._ft8_timer.stop()
        # Whatever changed in the last second and a half is written now: the
        # debounce must not outlive the window it was measuring.
        self._prefs_timer.stop()
        self._remember()
        # Stop the decoder before the radio: disarm() stops it queueing any
        # more slots, and the worker is a daemon thread whose jt9 call, if one
        # is running, is left to finish rather than waited on -- a window that
        # hangs on closing because a decoder is chewing through a slot would be
        # a poor trade for not having to reap it.
        self.spotter.disarm()
        self.opener.cancel()
        self.radio.close()
        super().closeEvent(ev)
