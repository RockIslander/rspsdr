"""Settings the window remembers between runs.

Everything the user changes -- the frequency, the mode and filter, gain, volume,
noise reduction, what the waterfall looks like, how far it is zoomed, which FT8
band, the size of the window -- is written to ``settings.json`` in the user's
config directory when it changes and read back at the next start.  Nothing in
the program should have to be set up twice.

Three things about how that is done are worth knowing.

* **The file is never allowed to break the program.**  Every value is read back
  through the table below, which says what kind of thing it is and what range or
  set of values is usable.  Anything that fails is *ignored* and the built-in
  default kept; a hand-edited or half-written file costs you one setting, not a
  window that will not open.  The same table is used to write the file, so a
  field cannot be saved and then not understood.
* **The command line wins, but only where it was typed.**  Saved settings are the
  *base* the window is built from, and an argument the user actually gave
  overrides that one field -- so ``--freq 14074000`` goes where it is told while
  the volume and the waterfall colours still come back remembered.  See
  ``_make_settings`` in ``__main__``.
* **Written atomically**, like the memory channels: temporary file, ``fsync``,
  rename.  An interrupted write leaves the previous settings in place rather
  than a truncated file that reads as "no settings at all".
"""

from __future__ import annotations

import json
import os
import tempfile

from . import ft8
from .memory import config_dir
from .radio import RECEIVER_MODES, Settings

FORMAT_VERSION = 1
FILE_NAME = "settings.json"
SECTIONS = ("radio", "ui", "window")

# How each remembered field is read back.  ``kind`` decides, ``spec`` carries
# whatever else that kind needs:
#
#   text    (limit,)                a non-empty string, at most ``limit`` long
#   int     (lo, hi)                an integer inside the range
#   number  (lo, hi)                a float inside the range
#   flag    ()                      true or false
#   choice  (a, b, ...)             one of these strings
#   mode    ()                      a mode the receiver or the FT8 decoder knows
#   fft     (lo, hi)                a power-of-two transform size in the range
RADIO_FIELDS: dict[str, tuple[str, tuple]] = {
    "device_args": ("text", (120,)),
    "sample_rate": ("int", (50_000, 20_000_000)),
    "center_freq": ("number", (10_000.0, 2_000_000_000.0)),
    "tuned_freq": ("number", (10_000.0, 2_000_000_000.0)),
    "mode": ("mode", ()),
    "filter_bw": ("number", (100.0, 20_000.0)),
    "cw_pitch": ("number", (200.0, 1500.0)),
    "rf_agc": ("flag", ()),
    "if_gain": ("number", (0.0, 100.0)),        # the device clamps to its own
    "rf_gain": ("int", (0, 63)),
    "tuner_bw": ("int", (0, 20_000_000)),
    "audio_agc": ("flag", ()),
    "nr": ("flag", ()),
    "nr_level": ("number", (0.0, 100.0)),
    "volume": ("number", (0.0, 2.0)),
    "squelch_db": ("number", (-200.0, 0.0)),
    "n_fft": ("fft", (256, 1_048_576)),
    "spec_avg": ("number", (0.0, 0.999)),
    "audio_backend": ("choice", ("auto", "pipe", "qt")),
}


def default_path() -> str:
    """Where the remembered settings live for this user."""
    return os.path.join(config_dir(), FILE_NAME)


def read_value(kind: str, raw, spec: tuple = ()):
    """One remembered value, or ``None`` if it cannot be used.

    ``None`` is the answer to every question here that is not an explicit yes:
    the wrong type, out of range, not one of the choices, a mode the receiver
    does not have.  The caller keeps its default, which is what makes a damaged
    file harmless.
    """
    try:
        if kind == "flag":
            return bool(raw) if isinstance(raw, (bool, int)) else None
        if kind == "int":
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return None
            value = int(raw)
            return value if spec[0] <= value <= spec[1] else None
        if kind == "number":
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return None
            value = float(raw)
            return value if spec[0] <= value <= spec[1] else None
        if kind == "fft":
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return None
            value = int(raw)
            if not (spec[0] <= value <= spec[1]) or value & (value - 1):
                return None
            return value
        if kind == "text":
            if not isinstance(raw, str):
                return None
            value = raw.strip()
            return value if 0 < len(value) <= spec[0] else None
        if kind == "choice":
            if not isinstance(raw, str):
                return None
            value = raw.strip().lower()
            return value if value in spec else None
        if kind == "mode":
            if not isinstance(raw, str):
                return None
            value = raw.strip().lower()
            # The mapping is the point: "ft8" is a mode the *window* offers and
            # the radio has never heard of, and an unknown name would raise
            # inside Demodulator() while the stream was starting.
            return value if ft8.receiver_mode(value) in RECEIVER_MODES else None
    except (TypeError, ValueError, IndexError):
        return None
    return None


class PrefsStore:
    """The remembered settings, as a small JSON document on disk.

    ``read=False`` starts from nothing and is what ``--reset-settings`` uses:
    the file is not consulted, and the next save overwrites it with whatever the
    window is set to, which is how a user gets back to a known state.
    """

    def __init__(self, path: str | None = None, read: bool = True):
        self.path = path or default_path()
        self.data: dict[str, dict] = {name: {} for name in SECTIONS}
        if read:
            self.load()

    # -- persistence ------------------------------------------------
    def load(self) -> None:
        """Read the file; anything unusable is left out, nothing is raised."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for name in SECTIONS:
            section = raw.get(name)
            if isinstance(section, dict):
                # Unknown keys are kept: a newer version may have written them,
                # and dropping them would lose a setting on a downgrade.
                self.data[name].update(section)

    def save(self) -> None:
        """Write atomically, so an interrupted write cannot corrupt the file."""
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        payload = {"version": FORMAT_VERSION, **self.data}
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings-",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- reading and writing ----------------------------------------
    def value(self, section: str, key: str, kind: str, spec: tuple = (),
              default=None):
        """A remembered value, or ``default`` if it is missing or unusable."""
        found = read_value(kind, self.data.get(section, {}).get(key), spec)
        return default if found is None else found

    def put(self, section: str, key: str, value) -> None:
        self.data.setdefault(section, {})[key] = value


def settings_from(prefs: PrefsStore, base: Settings | None = None) -> Settings:
    """The remembered receiver settings, on top of ``base``.

    ``base`` is what the caller would have used without any remembering -- the
    built-in defaults, or defaults already overridden by command-line arguments.
    Only the fields the file has, and that pass validation, are applied.
    """
    remembered = {
        name: prefs.value("radio", name, kind, spec)
        for name, (kind, spec) in RADIO_FIELDS.items()
    }
    remembered = {k: v for k, v in remembered.items() if v is not None}
    return (base or Settings()).copy_with(**remembered)


def remember_settings(prefs: PrefsStore, settings: Settings) -> None:
    """Write the receiver settings that are worth remembering."""
    for name in RADIO_FIELDS:
        prefs.put("radio", name, getattr(settings, name))
