"""Saved memory channels.

A memory channel is everything you need to go back to a signal: the frequency
plus the demodulator settings that suit it.  Channels are stored as JSON under
the user's config directory and written atomically (temporary file + rename) so
that an interrupted write cannot leave a corrupt file behind.  A missing or
damaged file is treated as "no memories yet" rather than as a fatal error --
losing your channel list because one byte went bad would be far worse than
starting empty.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass

FORMAT_VERSION = 1

# Where the channel file lived before the application was renamed.  Kept only
# so that an existing install can be brought forward once -- see
# :func:`adopt_previous`.
PREVIOUS_DIR_NAME = "rsp1_listener"


def config_dir() -> str:
    """Per-user config directory, honouring ``XDG_CONFIG_HOME``."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "rspsdr")


def default_path() -> str:
    return os.path.join(config_dir(), "memories.json")


def previous_path() -> str:
    """Where the channel file would live had the app never been renamed."""
    return os.path.join(os.path.dirname(config_dir()), PREVIOUS_DIR_NAME,
                        "memories.json")


def adopt_previous(path: str) -> bool:
    """Bring a pre-rename channel file forward, once.

    Renaming the application moved its config directory, and a rename is not a
    good enough reason for someone's saved channels to vanish.  The old file is
    *copied*, not moved, so it stays behind as a fallback and nothing is
    destroyed if the new name is ever rolled back.

    Only called for the default location; a caller that passes an explicit path
    (a test, or a second profile) means exactly that path.
    """
    if os.path.exists(path):
        return False
    old = previous_path()
    if not os.path.exists(old):
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(old, path)
    except OSError:
        return False
    return True


@dataclass
class Channel:
    """One saved frequency with the settings to listen to it."""

    name: str
    freq: float                 # Hz
    mode: str = "am"
    filter_bw: float = 2400.0
    cw_pitch: float = 700.0
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Channel":
        """Build from JSON, ignoring anything malformed.

        Raises ``ValueError``/``TypeError`` if the entry cannot describe a
        usable channel, so the caller can skip it.
        """
        if not isinstance(d, dict):
            raise TypeError("channel must be an object")
        freq = float(d["freq"])
        if not (freq > 0.0):
            raise ValueError("frequency must be positive")
        mode = str(d.get("mode", "am")).lower()
        if not mode:
            raise ValueError("empty mode")
        return cls(
            name=str(d.get("name") or "unnamed"),
            freq=freq,
            mode=mode,
            filter_bw=float(d.get("filter_bw", 2400.0)),
            cw_pitch=float(d.get("cw_pitch", 700.0)),
            note=str(d.get("note") or ""),
        )

    def key(self) -> str:
        return self.name.strip().casefold()


class MemoryStore:
    """An ordered, frequency-sorted list of :class:`Channel` on disk."""

    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        if path is None:
            adopt_previous(self.path)
        self.channels: list[Channel] = []
        self.load()

    # -- persistence ------------------------------------------------
    def load(self) -> None:
        """Read the file; a missing or unreadable file yields an empty list."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            self.channels = []
            return
        items = raw.get("channels") if isinstance(raw, dict) else raw
        out: list[Channel] = []
        if isinstance(items, list):
            for entry in items:
                try:
                    ch = Channel.from_dict(entry)
                except (TypeError, ValueError, KeyError):
                    continue
                if ch.key() in [c.key() for c in out]:
                    continue
                out.append(ch)
        self.channels = out
        self.sort()

    def save(self) -> None:
        """Write atomically, so a crash mid-write cannot corrupt the file."""
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        payload = {"version": FORMAT_VERSION,
                   "channels": [asdict(c) for c in self.channels]}
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".memories-",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- editing ----------------------------------------------------
    def sort(self) -> None:
        self.channels.sort(key=lambda c: c.freq)

    def find(self, name: str) -> int | None:
        key = name.strip().casefold()
        for i, ch in enumerate(self.channels):
            if ch.key() == key:
                return i
        return None

    def save_channel(self, channel: Channel) -> int:
        """Add, or replace the channel with the same name.  Returns its index."""
        idx = self.find(channel.name)
        if idx is None:
            self.channels.append(channel)
        else:
            self.channels[idx] = channel
        self.sort()
        self.save()
        return self.channels.index(channel)

    def update(self, index: int, channel: Channel) -> int:
        self.channels[index] = channel
        self.sort()
        self.save()
        return self.channels.index(channel)

    def remove(self, index: int) -> Channel:
        ch = self.channels.pop(index)
        self.save()
        return ch

    def clear(self) -> None:
        self.channels = []
        self.save()

    # -- container sugar --------------------------------------------
    def __len__(self) -> int:
        return len(self.channels)

    def __iter__(self):
        return iter(self.channels)

    def __getitem__(self, index: int) -> Channel:
        return self.channels[index]
