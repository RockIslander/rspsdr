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
    | spectrum plot                                                 |
    +--------------------------------------------------------------+
    | waterfall                                                     |
    +--------------------------------------------------------------+
    | rf row: gain, tuner bandwidth, fft                            |
    +--------------------------------------------------------------+
    | waterfall row: levels, colour, zoom                           |
    +--------------------------------------------------------------+
    | status bar                                                    |
---------------------------------------------------------------

The control rows use :class:`FlowLayout`, so they wrap onto another line when
the window is narrower than their contents.  With an ordinary QHBoxLayout each
row imposes a hard minimum width equal to the sum of its controls -- 1469 px
for the tuning row -- which is wider than many screens, and a window whose
minimum exceeds the screen cannot be maximised or fitted at all.
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

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLayout, QMainWindow, QMessageBox, QPushButton,
    QSlider, QSplitter, QVBoxLayout, QWidget,
)

from . import bands, logo
from .memory import Channel, MemoryStore
from .radio import TUNER_BANDWIDTHS, Radio, Settings, list_devices

MODES = [
    ("AM", "am", 6000.0), ("SAM", "sam", 6000.0), ("USB", "usb", 2400.0),
    ("LSB", "lsb", 2400.0), ("CW", "cw", 500.0), ("NFM", "nfm", 12500.0),
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
ZOOMS = [("1x", 1), ("2x", 2), ("4x", 4), ("8x", 8), ("16x", 16)]

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

REPAINT_MS = 45



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

    def _freq_at(self, pos) -> float | None:
        if self._freqs is None:
            return None
        p = self.plotItem.vb.mapSceneToView(pos)
        if self._freqs.size < 2:
            return None
        step = self._freqs[1] - self._freqs[0]
        k = int(round((p.x() * 1e6 - self._freqs[0]) / step))
        k = max(0, min(k, self._freqs.size - 1))
        return float(self._freqs[k])

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            f = self._freq_at(ev.position())
            if f is not None:
                self._dragging = True
                self.tuned.emit(f)
                ev.accept()
                return
        elif ev.button() == Qt.MouseButton.RightButton:
            f = self._freq_at(ev.position())
            if f is not None:
                self.recentre.emit(f)
                ev.accept()
                return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if self._dragging:
            f = self._freq_at(pos)
            if f is not None:
                self.tuned.emit(f)
            ev.accept()
            return
        f = self._freq_at(pos)
        if f is not None:
            self.cursor.emit(f)
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._dragging = False
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
        self._apply_rect()
        self.img.setImage(self._rgba, autoLevels=False, levels=None)

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
        self._apply_rect()
        self.img.setImage(self._rgba, autoLevels=False, levels=None)

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

    def _freq_at(self, pos) -> float | None:
        if self._hi_freq <= self._lo_freq:
            return None
        p = self.plotItem.vb.mapSceneToView(pos)
        f = p.x() * 1e6
        return float(min(max(f, self._lo_freq), self._hi_freq))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            f = self._freq_at(ev.position())
            if f is not None:
                self._dragging = True
                self.tuned.emit(f)
                ev.accept()
                return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._dragging:
            f = self._freq_at(ev.position())
            if f is not None:
                self.tuned.emit(f)
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._dragging = False
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


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None,
                 memory_path: str | None = None):
        super().__init__()
        self.setWindowTitle(f"{logo.APP_NAME} -- {logo.APP_TAGLINE}")
        # The task-bar entry, set here as well as in __main__ so that a window
        # built by a test or by --screenshot carries the mark too.
        self.setWindowIcon(logo.logo_icon())
        self._size_to_screen()

        self.settings = settings or Settings()
        self.memory = MemoryStore(memory_path)
        self.bridge = Bridge()
        self.radio = Radio(self.settings,
                           on_spectrum=lambda c, db, sr: self.bridge.spectrum.emit(c, db, sr),
                           on_status=lambda st: self.bridge.status.emit(st),
                           on_error=lambda m: self.bridge.error.emit(m))
        self._latest: tuple | None = None
        self._step = 1000
        self._zoom = 1
        self._building = True
        self._ui_running = False       # mirrors what the Start button shows
        self.frames_drawn = 0
        self.render_seconds = 0.0

        self._build_ui()
        self._connect()
        self._building = False

        self._timer = QTimer(self)
        self._timer.setInterval(REPAINT_MS)
        self._timer.timeout.connect(self._render)
        self._timer.start()

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

        self.spectrum = SpectrumPlot()
        self.waterfall = WaterfallPlot()
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.spectrum)
        split.addWidget(self.waterfall)
        split.setSizes([300, 340])
        root.addWidget(split, 1)

        root.addWidget(self._build_rf_row())
        root.addWidget(self._build_waterfall_row())
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
        # the control rows have wrapped.
        self.setMinimumSize(780, 560)
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
        name.setStyleSheet("color:#e9f5ff")
        h.addWidget(name)

        tag = QLabel(logo.APP_TAGLINE)
        tag.setStyleSheet("color:#7f97ab")
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

    def _build_tuning_row(self) -> QWidget:
        w, lay = self._row()

        self.btn_start = QPushButton("▶  Start")
        self.btn_start.setMinimumWidth(96)
        self.btn_start.setStyleSheet(
            "QPushButton{background:#1d7a46;color:white;font-weight:bold;"
            "padding:6px 10px;border-radius:4px}"
            "QPushButton:hover{background:#249155}")
        lay.addWidget(self.btn_start)

        self.lbl_device = QLabel("no device")
        self.lbl_device.setStyleSheet("color:#9fb0c0")
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
        self.lbl_band.setStyleSheet("color:#7fe7ff")
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
        self.spn_sq.setValue(-140.0)
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
        self.lbl_mem.setStyleSheet("color:#9fb0c0")
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
        self.lbl_if.setMinimumWidth(48)
        lay.addWidget(self._pair("IF gain", self.sld_if, self.lbl_if))

        self.cmb_rf = QComboBox()
        for i in range(4):
            self.cmb_rf.addItem(str(i), i)
        lay.addWidget(self._pair("RF gain", self.cmb_rf))

        self.cmb_tbw = QComboBox()
        self.cmb_tbw.addItem("Auto", 0)
        for bw in TUNER_BANDWIDTHS:
            self.cmb_tbw.addItem(f"{bw/1e6:g} MHz", bw)
        lay.addWidget(self._pair("Tuner BW", self.cmb_tbw))

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep)

        # The FFT size is where the waterfall's resolution ultimately comes
        # from: a bin is one span/n_fft of spectrum, and the waterfall shows the
        # visible slice of those bins, so doubling the FFT halves the width of
        # the finest thing that can be drawn.  It costs DSP time in proportion
        # -- which is why the top of this list only became affordable once the
        # transform moved to scipy.fft (measured on an i5-4590T: a 16384-point
        # transform of two million samples a second went from 5.2 ms to 2.1 ms
        # a frame, so 32768 and 65536 now cost what 8192 and 16384 used to).
        self.cmb_fft = QComboBox()
        for n in (1024, 2048, 4096, 8192, 16384, 32768, 65536):
            self.cmb_fft.addItem(str(n), n)
        self.cmb_fft.setCurrentText("4096")
        self.cmb_fft.setToolTip(
            "Frequency resolution of the spectrum and the waterfall: one bin "
            "is the span divided by this.  4096 suits a 2 MSps span; larger "
            "sizes pay for themselves on a zoomed view")
        lay.addWidget(self._pair("FFT", self.cmb_fft))

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
            "signal in view")

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
        self.lbl_zoom.setStyleSheet("color:#9fb0c0")
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

        self.spectrum.tuned.connect(self._tune)
        self.waterfall.tuned.connect(self._tune)
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
            self.radio.close()
            self._set_running_ui(False)
        else:
            self.start_radio()

    def start_radio(self) -> bool:
        """Open the stream if it is not already running."""
        if self.radio.opened:
            return True
        try:
            self.radio.open()
            self._set_running_ui(True)
            return True
        except Exception as exc:
            self.radio.close()
            self._set_running_ui(False)
            self._on_error(str(exc))
            return False

    def autostart(self):
        """Open the radio as soon as the window is up."""
        if not self.start_radio():
            self._on_status_line(
                "could not open the RSP1 -- check it is plugged in and the "
                "sdrplay service is running, then press Start")

    def _set_running_ui(self, running: bool):
        self._ui_running = bool(running)
        self.btn_start.setText("■  Stop" if running else "▶  Start")
        self.btn_start.setStyleSheet(
            "QPushButton{background:#a03030;color:white;font-weight:bold;"
            "padding:6px 10px;border-radius:4px}"
            "QPushButton:hover{background:#c03a3a}" if running else
            "QPushButton{background:#1d7a46;color:white;font-weight:bold;"
            "padding:6px 10px;border-radius:4px}"
            "QPushButton:hover{background:#249155}")

    def _apply(self, **kw):
        if self._building:
            return
        self.settings = self.settings.copy_with(**kw)
        self.radio.apply(**kw)

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

    def _tune(self, freq: float):
        self.spn_freq.setValue(freq / 1e6)
        self._maybe_recentre(freq)

    def _maybe_recentre(self, freq: float):
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
        self._apply(mode=key, filter_bw=float(self.cmb_bw.currentData()))

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

    def _wf_settings_changed(self, *_):
        auto = self.chk_wfauto.isChecked()
        self.spn_contrast.setEnabled(auto)
        self.spn_wflo.setEnabled(not auto)
        self.spn_wfhi.setEnabled(not auto)
        self.waterfall.set_levels(auto, float(self.spn_contrast.value()),
                                  float(self.spn_wflo.value()),
                                  float(self.spn_wfhi.value()))

    def _wf_colour_changed(self, *_):
        key = self.cmb_cmap.currentData()
        self.waterfall.set_colormap(str(key), self.chk_cmrev.isChecked())

    def _wf_detail_changed(self, *_):
        self.waterfall.set_detail(int(self.cmb_wfdetail.currentData()))
        self._wf_detail_readout()

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
        offer = ("" if wf.bins() <= wf.cols()
                 else f", {wf.bins():,} bins on offer")
        self._on_status_line(
            f"waterfall detail {self.cmb_wfdetail.currentText()} -- "
            f"{wf.cols():,} columns x {wf.rows()} rows, "
            f"{per_col/1e3:.2f} kHz per column{offer}")

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
        span = float(self.settings.sample_rate) / max(1, self._zoom)
        self.lbl_zoom.setText(f"{span/1e3:,.0f} kHz" if span < 1e6
                              else f"{span/1e6:g} MHz")

    def _zoom_changed(self, *_):
        self._zoom = int(self.cmb_zoom.currentData())
        # Rows already on screen were pooled for a different span, so they no
        # longer mean what the level scale says; start the picture again.
        self.waterfall.clear_history()
        self._update_zoom_readout()
        self._maybe_recentre(self.settings.tuned_freq)
        self._on_status_line(f"zoom {self._zoom}x -- " +
                             self.lbl_zoom.text() + " across the window")

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
        self._timer.stop()
        self.radio.close()
        super().closeEvent(ev)
