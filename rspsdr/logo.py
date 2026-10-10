"""The RSPSDR mark.

One piece of artwork, drawn rather than loaded.

The mark has to appear in three places -- the header of the main window, the
title-bar / task-bar icon and the desktop menu entry -- and Qt's SVG module
(``PyQt6.QtSvg``) is a separate distribution package that is not installed on
the target machine, so loading a file and rendering it would mean either a new
dependency or a bitmap that goes soft at every size but one.  Painting it from
the same arc description gives a crisp result at 16 px and at 512 px from the
same code, and costs nothing at import.

``rspsdr.svg`` is that same shape in vector form for the desktop environment,
which does have an SVG renderer.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QIcon, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPixmap)
from PyQt6.QtWidgets import QWidget

APP_NAME = "RSPSDR"
APP_TAGLINE = "HF / shortwave SDR receiver"

# The design grid every number below is expressed in.  Painting scales it, so
# the mark is resolution independent without a vector rasteriser.
GRID = 64.0

# Wavelengths fanning out of the mast, in grid units: (radius, pen width).
_WAVES = ((8.5, 2.6), (14.5, 2.2), (20.5, 1.8))
# The arc sweeps from just above the horizon on the left to just above it on
# the right, leaving the mast inside it clear.
_ARC_FROM, _ARC_SPAN = 32.0, 116.0     # degrees, Qt convention: 0 = 3 o'clock
_MAST_TOP, _MAST_BOTTOM = 20.0, 40.0


def _paint_mark(p: QPainter, size: float) -> None:
    """Draw the whole mark into a ``size`` x ``size`` square at the origin."""
    k = size / GRID
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    rect = QRectF(1.5 * k, 1.5 * k, (GRID - 3.0) * k, (GRID - 3.0) * k)
    path = QPainterPath()
    path.addRoundedRect(rect, 14.0 * k, 14.0 * k)

    grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
    grad.setColorAt(0.0, QColor("#153f66"))
    grad.setColorAt(0.55, QColor("#0d1b2c"))
    grad.setColorAt(1.0, QColor("#090b0f"))
    p.fillPath(path, QBrush(grad))

    edge = QPen(QColor("#3f88ad"))
    edge.setWidthF(max(1.0, 2.0 * k))
    p.setPen(edge)
    p.drawPath(path)

    cx = GRID / 2.0
    base = _MAST_BOTTOM

    # The mast itself, then the waves coming off it.
    stem = QPen(QColor("#7fe7ff"))
    stem.setWidthF(max(1.2, 2.4 * k))
    stem.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(stem)
    p.drawLine(QPointF(cx * k, _MAST_TOP * k), QPointF(cx * k, base * k))

    for radius, width in _WAVES:
        wave = QPen(QColor("#7fe7ff"))
        wave.setWidthF(max(1.0, width * k))
        wave.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(wave)
        p.drawArc(QRectF((cx - radius) * k, (base - radius) * k,
                         2.0 * radius * k, 2.0 * radius * k),
                  int(_ARC_FROM * 16), int(_ARC_SPAN * 16))

    # The transmitter itself, at the foot of the mast.
    dot = QPainterPath()
    dot.addEllipse(QPointF(cx * k, base * k), 3.4 * k, 3.4 * k)
    p.fillPath(dot, QBrush(QColor("#ffd166")))


def logo_pixmap(size: int = 64, ratio: float = 1.0) -> QPixmap:
    """The mark as a pixmap, drawn at full resolution for the given ratio."""
    px = max(1, int(round(size * ratio)))
    pix = QPixmap(px, px)
    pix.setDevicePixelRatio(ratio)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    try:
        _paint_mark(p, float(px))
    finally:
        p.end()
    return pix


def logo_icon() -> QIcon:
    """The mark as a window / task-bar icon.

    Several sizes are supplied explicitly so the window manager can pick one
    close to what it needs instead of scaling a single bitmap.
    """
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(logo_pixmap(size))
    return icon


class Logo(QWidget):
    """The mark as a widget, for the header of the main window."""

    def __init__(self, size: int = 30, parent: QWidget | None = None):
        super().__init__(parent)
        self._size = int(size)
        self.setFixedSize(self._size, self._size)
        self.setToolTip(f"{APP_NAME} -- {APP_TAGLINE}")

    def sizeHint(self):                     # noqa: N802
        from PyQt6.QtCore import QSize
        return QSize(self._size, self._size)

    def paintEvent(self, _ev):              # noqa: N802
        p = QPainter(self)
        try:
            _paint_mark(p, float(min(self.width(), self.height())))
        finally:
            p.end()
