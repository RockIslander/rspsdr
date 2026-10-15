"""The window's colour schemes.

Five of them, in one table, so that a colour is never written down twice: the
palette the widgets are filled from, the stylesheet the controls are styled
with, the plots' own pens and backgrounds, and the handful of labels that used
to carry a hard-coded hex value each.  ``rspsdr/ui.py`` is what puts one on the
application (see :func:`rspsdr.ui.apply_theme`); this module is the table.

Two things are deliberate here.

* **Nothing imports Qt.**  A theme is data and the stylesheet is text, so the
  suite can check that every scheme is complete, that every colour parses, and
  that the stylesheet built from it is balanced and mentions every colour,
  without a display, a style or a QApplication -- the same reason
  ``rspsdr/smeter.py`` is separate from the widget that draws its needle.
* **A theme is always complete.**  :func:`palette` fills anything a scheme does
  not state from the default, so a half-written or unknown name still gives a
  window that can be drawn rather than a KeyError out of a paint event.
"""

from __future__ import annotations

# Every field a scheme is made of.  Anything drawn from a theme reads one of
# these, which is what keeps "add a colour" a one-line change in each scheme
# instead of a hunt through the window.
KEYS = (
    "window",        # the window behind everything
    "panel",         # a raised area: the FT8 strip, a button, a header row
    "field",         # inputs: combo boxes, spin boxes, the decode table
    "text",          # ordinary text
    "muted",         # secondary text: the tagline, the device, the readouts
    "bright",        # the loudest text there is: the application's name
    "accent",        # the spectrum trace, the band label, current state
    "peak",          # the spectrum's peak-hold trace
    "marker",        # the tuned-frequency marker and the passband edge
    "band",          # the passband the demodulator is using, as a wash
    "grid",          # plot grid, axis lines and tick marks
    "grid_alpha",    # how strongly that grid is drawn (a number, not a colour)
    "plot",          # the plots' own background
    "border",        # outlines and separators
    "run",           # the Start button, and the Stop hover
    "run_hover",
    "stop",
    "stop_hover",
    "error",         # a message that is a complaint
    "highlight",     # selection and pressed state
    "highlight_text",
)

THEMES: dict[str, dict[str, str | float]] = {
    # The default, and what the window looked like before there was a choice:
    # near-black blue with a cyan trace.  Everything it paints was either in
    # the source already or one shade of it.
    "night": {
        "window": "#0b0e13", "panel": "#151d27", "field": "#0f151d",
        "text": "#dbe6f0", "muted": "#7f97ab", "bright": "#e9f5ff",
        "accent": "#7fe7ff", "peak": "#ffd166", "marker": "#ff5f6d",
        "band": "#28c878", "grid": "#5b7b93", "grid_alpha": 0.18,
        "plot": "#0a0c10", "border": "#2a3644",
        "run": "#1d7a46", "run_hover": "#249155",
        "stop": "#a03030", "stop_hover": "#c03a3a",
        "error": "#ffb0a0", "highlight": "#1f5d7a",
        "highlight_text": "#eaf6ff",
    },
    # An old valve set: a black dial with amber markings, which is also the
    # colour of the meter face it goes with.  The Start and Stop buttons keep
    # their green and red -- they say what they do -- but dimmed to sit under
    # the amber.
    "amber": {
        "window": "#120d08", "panel": "#1d1610", "field": "#171009",
        "text": "#f2dcb2", "muted": "#b08d55", "bright": "#ffeccb",
        "accent": "#ffb144", "peak": "#ffe08a", "marker": "#ff6a3d",
        "band": "#d99a3a", "grid": "#8a6a3a", "grid_alpha": 0.20,
        "plot": "#0a0705", "border": "#4a3823",
        "run": "#2f6b28", "run_hover": "#3d8734",
        "stop": "#a03020", "stop_hover": "#c03a28",
        "error": "#ff9a70", "highlight": "#5a3c12",
        "highlight_text": "#fff2d8",
    },
    # A green-phosphor display.  The only scheme whose trace is bright enough
    # to be read as glow rather than as a line.
    "phosphor": {
        "window": "#04100a", "panel": "#0a1c12", "field": "#06150e",
        "text": "#bdf5cd", "muted": "#5f9a72", "bright": "#dcffe6",
        "accent": "#4dff88", "peak": "#d9ff7a", "marker": "#ff5f6d",
        "band": "#34c46a", "grid": "#2e6b45", "grid_alpha": 0.20,
        "plot": "#030a06", "border": "#1d4a2c",
        "run": "#218a46", "run_hover": "#2aa957",
        "stop": "#8f3230", "stop_hover": "#ad3d3a",
        "error": "#ffb0a0", "highlight": "#17512f",
        "highlight_text": "#e6ffee",
    },
    # Neutral grey with one soft blue accent: the least amount of colour that
    # still separates the trace from the grid, for a long evening or a colour
    # display that is already loud.
    "slate": {
        "window": "#16181b", "panel": "#1f2327", "field": "#191d21",
        "text": "#e0e4e8", "muted": "#8a939c", "bright": "#f3f6f9",
        "accent": "#8fc7e8", "peak": "#e6c88f", "marker": "#e0705f",
        "band": "#6fae8f", "grid": "#6b757e", "grid_alpha": 0.20,
        "plot": "#111417", "border": "#343b42",
        "run": "#2f6b46", "run_hover": "#3a8154",
        "stop": "#8a3a34", "stop_hover": "#a5483f",
        "error": "#f0a094", "highlight": "#38505f",
        "highlight_text": "#eef3f7",
    },
    # Light, for a bright room, a laptop outdoors or a projector.  The trace
    # is a saturated blue rather than the pale cyan of the dark schemes: on a
    # white plot area a pale trace is the one thing that cannot be read.
    "daylight": {
        "window": "#eef1f5", "panel": "#e2e8ee", "field": "#ffffff",
        "text": "#1c2430", "muted": "#5b6b7c", "bright": "#0b3c78",
        "accent": "#1060c0", "peak": "#b3541e", "marker": "#c62828",
        "band": "#2f8f5f", "grid": "#7b8b9c", "grid_alpha": 0.30,
        "plot": "#f7f9fb", "border": "#b9c4d0",
        "run": "#1d7a46", "run_hover": "#249155",
        "stop": "#a03030", "stop_hover": "#c03a3a",
        "error": "#b3261e", "highlight": "#c3daf7",
        "highlight_text": "#0e2038",
    },
}

# The order they are offered in: the default first, then alike-to-unlike, dark
# before light.
ORDER = ("night", "amber", "phosphor", "slate", "daylight")
DEFAULT = "night"

# What the list shows: a name, and what to expect from it.
LABELS: dict[str, tuple[str, str]] = {
    "night": ("Night", "near-black blue with a cyan trace"),
    "amber": ("Amber", "a valve set: black dial, amber markings"),
    "phosphor": ("Phosphor", "green cathode-ray glow"),
    "slate": ("Slate", "neutral grey with one soft blue accent"),
    "daylight": ("Daylight", "light -- for a bright room or a projector"),
}


def names() -> tuple[str, ...]:
    """Every scheme on offer, in the order they should be listed."""
    return ORDER


def label(name: str) -> str:
    """The name a user sees for a scheme."""
    found = LABELS.get(name)
    return found[0] if found else name.capitalize()


def note(name: str) -> str:
    """One line describing a scheme, for a tooltip."""
    found = LABELS.get(name)
    return found[1] if found else ""


def palette(name: str) -> dict[str, str | float]:
    """A scheme by name, complete, with the default filled in behind it.

    An unknown name is not an error: it gives the default scheme, which is
    what a settings file written by a newer version, or hand-edited, deserves
    -- one wrong setting rather than a window that will not open.
    """
    base = dict(THEMES[DEFAULT])
    base.update(THEMES.get(name, {}))
    return base


def rgb(colour: str) -> tuple[int, int, int]:
    """``"#7fe7ff"`` as the three bytes Qt wants for a brush or a pen."""
    text = colour.lstrip("#")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def stylesheet(pal: dict[str, str | float]) -> str:
    """The application-wide Qt style sheet for a scheme.

    Only the controls are styled here; the window, the panels and the plots
    take their colours from the palette ``ui.apply_theme`` installs, which is
    also what tells a Fusion style to draw the rest of the widgets in the
    right shades.  Two rules are worth spelling out:

    * Nothing matches plain ``QWidget``.  A blanket background rule reaches the
      two plots as well, and pyqtgraph draws its own background: the rule won
      over it and the plots came out the colour of the window with the trace
      and the waterfall on top, which looks like a bug and is one.
    * The checkbox, the slider and the table header are styled because the
      platform styles draw those from their own images rather than from the
      palette -- a themed window with a white tick box in the middle of it is
      worse than no theme at all.
    """
    c = {k: v for k, v in pal.items() if isinstance(v, str)}
    return f"""
QMainWindow, QDialog {{ background: {c['window']}; }}
QStatusBar {{ background: {c['window']}; color: {c['muted']};
              border-top: 1px solid {c['border']}; }}
QStatusBar::item {{ border: none; }}
QLabel {{ background: transparent; color: {c['text']}; }}
QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit {{
    background: {c['field']}; color: {c['text']};
    border: 1px solid {c['border']}; border-radius: 3px; padding: 1px 5px;
    selection-background-color: {c['highlight']};
    selection-color: {c['highlight_text']};
}}
QComboBox:hover, QDoubleSpinBox:hover, QSpinBox:hover {{
    border: 1px solid {c['accent']};
}}
QComboBox:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled {{
    color: {c['muted']};
}}
QComboBox::drop-down {{ border: none; width: 15px; }}
QComboBox QAbstractItemView {{
    background: {c['field']}; color: {c['text']};
    border: 1px solid {c['border']};
    selection-background-color: {c['highlight']};
    selection-color: {c['highlight_text']};
}}
QPushButton {{
    background: {c['panel']}; color: {c['text']};
    border: 1px solid {c['border']}; border-radius: 4px; padding: 3px 9px;
}}
QPushButton:hover {{ border: 1px solid {c['accent']}; }}
QPushButton:checked, QPushButton:pressed {{
    background: {c['highlight']}; color: {c['highlight_text']};
}}
QPushButton:disabled {{
    color: {c['muted']}; border: 1px solid {c['border']};
}}
QCheckBox {{ background: transparent; color: {c['text']}; spacing: 5px; }}
QCheckBox:disabled {{ color: {c['muted']}; }}
QCheckBox::indicator {{
    width: 13px; height: 13px; background: {c['field']};
    border: 1px solid {c['border']}; border-radius: 3px;
}}
QCheckBox::indicator:hover {{ border: 1px solid {c['accent']}; }}
QCheckBox::indicator:checked {{
    background: {c['accent']}; border: 1px solid {c['accent']};
}}
QSlider::groove:horizontal {{
    height: 5px; background: {c['field']};
    border: 1px solid {c['border']}; border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {c['accent']}; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {c['accent']}; width: 11px; margin: -6px 0; border-radius: 5px;
}}
QSlider::handle:horizontal:hover {{ background: {c['bright']}; }}
QHeaderView::section {{
    background: {c['panel']}; color: {c['text']};
    border: none; border-bottom: 1px solid {c['border']}; padding: 2px 5px;
}}
QTableWidget {{
    background: {c['field']}; color: {c['text']};
    border: 1px solid {c['border']}; gridline-color: {c['border']};
}}
QTableWidget::item:selected {{
    background: {c['highlight']}; color: {c['highlight_text']};
}}
QSplitter::handle {{ background: {c['border']}; height: 3px; }}
QScrollBar:vertical {{ background: {c['window']}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {c['border']}; border-radius: 5px; min-height: 24px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QToolTip {{
    background: {c['panel']}; color: {c['text']};
    border: 1px solid {c['border']}; padding: 2px 4px;
}}
"""


def describe() -> str:
    """One line naming the schemes, for ``--check``."""
    return (", ".join(names()) + f" (default {DEFAULT}, "
            f"{len(names())} schemes)")
