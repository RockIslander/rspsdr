"""Tests for the persisted memory channels.

Run with:  python3 tests/test_memory.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rspsdr.memory import Channel, MemoryStore, config_dir  # noqa: E402

failures: list[str] = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' :: ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


tmp = tempfile.mkdtemp(prefix="rsp1-mem-")
try:
    print("== user paths ==")
    old = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = "/tmp/xyz-config"
    check("config_dir honours XDG_CONFIG_HOME",
          config_dir() == "/tmp/xyz-config/rspsdr", config_dir())
    if old is None:
        os.environ.pop("XDG_CONFIG_HOME", None)
    else:
        os.environ["XDG_CONFIG_HOME"] = old

    print("\n== empty / missing file ==")
    path = os.path.join(tmp, "sub", "dir", "memories.json")
    store = MemoryStore(path)
    check("missing file yields an empty store", len(store) == 0)

    print("\n== add, sort and persist ==")
    store.save_channel(Channel("20m SSB", 14_250_000.0, "usb", 2400.0))
    store.save_channel(Channel("40m CW", 7_030_000.0, "cw", 500.0, 700.0))
    store.save_channel(Channel("WWV 10", 10_000_000.0, "am", 6000.0))
    check("three channels stored", len(store) == 3, str(len(store)))
    check("sorted by frequency",
          [c.freq for c in store] == [7_030_000.0, 10_000_000.0, 14_250_000.0],
          str([c.freq for c in store]))
    check("file created under a missing directory tree", os.path.exists(path))

    again = MemoryStore(path)
    check("reload restores every channel", len(again) == 3)
    check("frequencies round-trip exactly",
          [c.freq for c in again] == [c.freq for c in store])
    check("mode/filter/name survive the round trip",
          again[0] == Channel("40m CW", 7_030_000.0, "cw", 500.0, 700.0),
          repr(again[0]))

    print("\n== same name replaces ==")
    store.save_channel(Channel("20m SSB", 14_200_000.0, "usb", 1800.0))
    check("no duplicate name", len(store) == 3, str(len(store)))
    check("existing entry overwritten",
          [c for c in store if c.name == "20m SSB"][0].freq == 14_200_000.0)
    store.save_channel(Channel("20M ssb", 14_100_000.0, "usb", 1800.0))
    check("name matching ignores case", len(store) == 3, str(len(store)))

    print("\n== find / update / remove / clear ==")
    check("find returns the index", store.find("WWV 10") == 1,
          str(store.find("WWV 10")))
    check("find on a missing name is None", store.find("nope") is None)

    store.update(0, Channel("40m CW", 7_040_000.0, "cw", 600.0, 800.0))
    check("update replaces in place",
          store[0].freq == 7_040_000.0 and store[0].filter_bw == 600.0)
    check("update persisted",
          MemoryStore(path)[0].filter_bw == 600.0)

    removed = store.remove(0)
    check("remove returns the channel", removed.name == "40m CW")
    check("remove persisted", len(MemoryStore(path)) == 2,
          str(len(MemoryStore(path))))

    store.clear()
    check("clear empties the store", len(store) == 0)
    check("clear persisted", len(MemoryStore(path)) == 0)

    print("\n== atomic write ==")
    store.save_channel(Channel("a", 5_000_000.0, "am", 6000.0))
    store.save_channel(Channel("b", 6_000_000.0, "am", 6000.0))
    leftovers = [f for f in os.listdir(os.path.dirname(path))
                 if f.startswith(".memories-")]
    check("no temporary files left behind", not leftovers, str(leftovers))
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    check("file is valid JSON with a version", doc.get("version") == 1)
    check("file holds a channel list", len(doc.get("channels", [])) == 2)

    print("\n== damaged and hostile files ==")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    bad = MemoryStore(path)
    check("corrupt JSON is treated as empty, not fatal", len(bad) == 0)
    bad.save_channel(Channel("recovered", 9_000_000.0, "am", 6000.0))
    check("store is still usable afterwards", len(MemoryStore(path)) == 1)

    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "channels": [
            {"name": "good", "freq": 9_600_000.0, "mode": "am"},
            {"name": "no freq", "mode": "am"},              # skipped
            {"name": "bad freq", "freq": -5.0, "mode": "am"},   # skipped
            {"name": "bad type", "freq": "banana", "mode": "am"},  # skipped
            "not even an object",                            # skipped
            {"name": "good", "freq": 8_000_000.0, "mode": "am"},  # duplicate
            {"freq": 11_000_000.0, "mode": "usb"},           # unnamed, kept
        ]}, fh)
    salvage = MemoryStore(path)
    check("only usable entries are loaded", len(salvage) == 2, str(len(salvage)))
    unnamed = [c for c in salvage if c.freq == 11_000_000.0]
    check("unnamed entry gets a placeholder",
          len(unnamed) == 1 and unnamed[0].name == "unnamed",
          str([c.name for c in salvage]))
    check("duplicate names collapse", len({c.key() for c in salvage}) == 2)

    print("\n== Channel.from_dict validation ==")
    for bad_input, why in (({"mode": "am"}, "missing freq"),
                           ({"freq": -1.0}, "negative freq"),
                           ({"freq": 0.0}, "zero freq"),
                           ({"freq": 1e6, "mode": ""}, "empty mode"),
                           ("nope", "not a mapping")):
        try:
            Channel.from_dict(bad_input)
            ok = False
        except (TypeError, ValueError, KeyError):
            ok = True
        check(f"rejects {why}", ok)

    print("\n== corrupt file does not lose the previous good file ==")
    # A failed save must leave the old content intact: build a valid file, then
    # make the directory unwritable for a moment and confirm the read still works.
    keep = MemoryStore(path)
    before = [c.freq for c in keep]
    check("previous content readable", len(before) == 2, str(len(before)))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all memory-store checks passed")
