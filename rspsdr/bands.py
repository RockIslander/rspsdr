"""Band plan and tuning presets for HF, shortwave, VHF and UHF.

Ham segments follow the IARU Region 2 plan (the Americas), which is what is
relevant for a receiver in North America.  Shortwave broadcast bands are the
standard metre bands.  The VHF blocks name the service a listener finds there
rather than listing every channel, which no band plan could keep up with.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    name: str
    low: float          # Hz
    high: float         # Hz
    mode: str           # default demodulator
    note: str = ""

    @property
    def centre(self) -> float:
        return 0.5 * (self.low + self.high)


# --- amateur bands (IARU Region 2) -------------------------------------
HAM_BANDS: list[Band] = [
    Band("160m", 1_800_000, 2_000_000, "lsb", "top band, night"),
    Band("80m", 3_500_000, 4_000_000, "lsb", "night"),
    Band("60m", 5_330_500, 5_403_500, "usb", "channels"),
    Band("40m", 7_000_000, 7_300_000, "lsb", "day & night"),
    Band("30m", 10_100_000, 10_150_000, "cw", "CW/data only"),
    Band("20m", 14_000_000, 14_350_000, "usb", "day"),
    Band("17m", 18_068_000, 18_168_000, "usb", "day"),
    Band("15m", 21_000_000, 21_450_000, "usb", "day"),
    Band("12m", 24_890_000, 24_990_000, "usb", "day"),
    Band("10m", 28_000_000, 29_700_000, "usb", "day, Es"),
    Band("6m", 50_000_000, 54_000_000, "usb", "sporadic E"),
    Band("2m", 144_000_000, 148_000_000, "nfm", "FM & SSB"),
    Band("70cm", 430_000_000, 440_000_000, "nfm", "FM & SSB"),
]

# --- shortwave broadcast (metre bands) ---------------------------------
SW_BANDS: list[Band] = [
    Band("120m", 2_300_000, 2_495_000, "am"),
    Band("90m", 3_200_000, 3_400_000, "am"),
    Band("75m", 3_900_000, 4_000_000, "am"),
    Band("60m", 4_750_000, 5_060_000, "am"),
    Band("49m", 5_900_000, 6_200_000, "am"),
    Band("41m", 7_200_000, 7_450_000, "am"),
    Band("31m", 9_400_000, 9_900_000, "am"),
    Band("25m", 11_600_000, 12_100_000, "am"),
    Band("22m", 13_570_000, 13_870_000, "am"),
    Band("19m", 15_100_000, 15_800_000, "am"),
    Band("16m", 17_480_000, 17_900_000, "am"),
    Band("13m", 21_450_000, 21_850_000, "am"),
    Band("11m", 25_670_000, 26_100_000, "am"),
]

# --- civil aviation VHF: the airband -----------------------------------
# 108-118 MHz is the navigation block, VOR and ILS localiser carriers, and
# 118-137 MHz is voice: tower, ground, approach, ATIS and VOLMET.  Both are
# AM, on 25 kHz channels (8.33 kHz in Europe).
AIR_BANDS: list[Band] = [
    Band("Air nav", 108_000_000, 118_000_000, "am", "VOR/ILS"),
    Band("Air band", 118_000_000, 137_000_000, "am", "voice"),
]

# --- the rest of VHF: 137-174 MHz -------------------------------------
# Just above the airband are the weather satellites' APT downlinks, NOAA and
# Meteor on 137.1-137.9; 144-148 is the 2 m amateur band, which sits in
# HAM_BANDS with the others.  The rest is land mobile, and the blocks follow
# what is really there rather than one name per service: the marine channels
# are two blocks with the business pool between them, and the federal pool
# runs across 162.05-174 with the seven NOAA weather channels inside it, so
# it is split at the channel edges either side of them.  A block stops at its
# last channel, which leaves the narrow gaps between blocks you would expect
# from the spacing, and means no block is named for traffic it does not carry.
VHF_BANDS: list[Band] = [
    Band("Weather sat", 137_000_000, 138_000_000, "nfm", "NOAA APT"),
    Band("Public safety", 138_000_000, 144_000_000, "nfm",
         "police, fire, business"),
    Band("Land mobile", 148_000_000, 156_000_000, "nfm", "12.5 kHz"),
    Band("Marine ship", 156_000_000, 157_425_000, "nfm",
         "ch 16 = 156.800"),
    Band("Business", 157_450_000, 160_600_000, "nfm", "12.5 kHz"),
    Band("Marine coast", 160_600_000, 162_037_500, "nfm",
         "duplex pairs"),
    Band("Government", 162_050_000, 162_387_500, "nfm",
         "federal, 162.05-162.39"),
    Band("NOAA weather", 162_400_000, 162_550_000, "nfm", "7 channels"),
    Band("Government", 162_562_500, 173_400_000, "nfm",
         "federal, 162.56-173.4"),
]

# --- UHF: the amateur band and the land mobile blocks above it ---------
# 430-440 is the 70 cm amateur band, which sits in HAM_BANDS with the
# others.  Above it the land mobile services are in duplex pairs: the mobile
# side transmits low in the band and the base side high, so a listener hears
# the base side and finds the mobiles on the other half.  700 MHz is the
# public safety and LTE band, ending at the guard above its uplink.  The
# cellular blocks between the 800 MHz halves, and the television allocations
# below 700 MHz, are left unnamed: they are not what this receiver is for,
# and a gap is more honest than a name over traffic you cannot hear.
UHF_BANDS: list[Band] = [
    Band("UHF land mobile", 450_000_000, 470_000_000, "nfm",
         "business, public safety"),
    Band("700 MHz mobile", 698_000_000, 805_000_000, "nfm",
         "public safety, LTE"),
    Band("800 MHz mobile", 806_000_000, 824_000_000, "nfm",
         "repeater inputs"),
    Band("800 MHz base", 851_000_000, 869_000_000, "nfm",
         "repeater outputs"),
    Band("900 MHz mobile", 896_000_000, 901_000_000, "nfm", "business"),
    Band("900 MHz base", 935_000_000, 940_000_000, "nfm", "business"),
]

# Everything the two lookups below search, in one place.
ALL_BANDS: list[Band] = (HAM_BANDS + SW_BANDS + AIR_BANDS + VHF_BANDS
                         + UHF_BANDS)

# The 8.33 kHz channels of the European airband are each 25 kHz channel cut in
# three, so they sit 25000/3 Hz apart: 118.000, 118.008333, 118.016667,
# 118.025 ...  Only every third one is a whole number of hertz, so a step is
# taken from that grid and rounded once at the end: a rounded 8333 Hz added
# three times lands a hertz low, on 118.024999.
NARROW_STEP_HZ = 8_333          # the step, as the window remembers it
AIRBAND_NARROW_HZ = 25_000 / 3


def next_narrow_channel(freq: float, direction: int) -> int:
    """The next 8.33 kHz channel up or down from ``freq``, in whole hertz."""
    k = round(freq / AIRBAND_NARROW_HZ) + direction
    return round(k * AIRBAND_NARROW_HZ)

# --- quick-jump "stations worth finding" -------------------------------
# The list is long enough to need finding aids, so every preset carries the
# section of the menu it belongs in, and the sections are declared here in
# reading order: amateur activity first, then the standards and broadcasts a
# listener checks an antenna against, then the services above 100 MHz.  The
# group is the heading a preset is listed under, not a frequency range.
PRESET_GROUPS: tuple[str, ...] = (
    "Ham", "Time", "Broadcast", "Utility", "Aviation", "Marine", "Weather")


@dataclass(frozen=True)
class Preset:
    name: str
    freq: float
    mode: str
    note: str = ""
    group: str = ""


PRESETS: list[Preset] = [
    # Amateur calling / activity, from HF through 10 m and the 2 m channels
    # that mean the same thing wherever you are.  10m FM and the 2 m pair are
    # amateur channels, so they are listed here rather than with the services
    # above the airband.
    Preset("80m CW", 3_550_000, "cw", "CW calling", group="Ham"),
    Preset("40m CW", 7_030_000, "cw", "CW calling", group="Ham"),
    Preset("40m SSB", 7_200_000, "lsb", "USA phone", group="Ham"),
    Preset("40m FT8", 7_074_000, "usb", "FT8, 3 kHz wide", group="Ham"),
    Preset("30m FT8", 10_136_000, "usb", "FT8", group="Ham"),
    Preset("20m SSB", 14_250_000, "usb", "USA phone", group="Ham"),
    Preset("20m CW", 14_030_000, "cw", "CW calling", group="Ham"),
    Preset("20m FT8", 14_074_000, "usb", "FT8", group="Ham"),
    Preset("17m SSB", 18_130_000, "usb", "SSB", group="Ham"),
    Preset("15m SSB", 21_300_000, "usb", "SSB", group="Ham"),
    Preset("17m WSPR", 18_104_600, "usb", "WSPR dial", group="Ham"),
    Preset("10m FM", 29_600_000, "nfm", "10m FM simplex", group="Ham"),
    Preset("2m SSB", 144_200_000, "usb", "SSB calling", group="Ham"),
    Preset("2m FM", 146_520_000, "nfm", "FM simplex", group="Ham"),
    # Time / standards -- excellent for checking your setup
    Preset("WWV 2.5", 2_500_000, "am", "NIST time, AM", group="Time"),
    Preset("WWV 5", 5_000_000, "am", "NIST time, AM", group="Time"),
    Preset("WWV 10", 10_000_000, "am", "NIST time, AM", group="Time"),
    Preset("WWV 15", 15_000_000, "am", "NIST time, AM", group="Time"),
    Preset("WWV 20", 20_000_000, "am", "NIST time, AM", group="Time"),
    Preset("WWVH 10", 10_000_000, "am", "Hawaii, same channel",
           group="Time"),
    Preset("CHU 3.33", 3_330_000, "am", "Canada time, USB", group="Time"),
    Preset("CHU 7.85", 7_850_000, "am", "Canada time", group="Time"),
    Preset("CHU 14.67", 14_670_000, "am", "Canada time", group="Time"),
    # Broadcasts
    Preset("R. Nacional", 6_180_000, "am", "49m, Amazonia",
           group="Broadcast"),
    Preset("BBC WS", 9_410_000, "am", "31m (varies)", group="Broadcast"),
    Preset("VOA", 15_580_000, "am", "19m (varies)", group="Broadcast"),
    Preset("WWCR", 13_845_000, "am", "22m Nashville", group="Broadcast"),
    # Utility -- the aviation and shipping services on HF.
    Preset("Volmet 5", 5_598_000, "usb", "HF VOLMET USB", group="Utility"),
    Preset("HFDL", 10_081_000, "usb", "aircraft HFDL", group="Utility"),
    # Airband -- civil aviation, AM, on 25 kHz channels.  Airport frequencies
    # differ from field to field, so these are the channels that mean the same
    # thing wherever you are; 121.500 is the one to check an antenna against.
    Preset("Air guard", 121_500_000, "am", "121.5 emergency",
           group="Aviation"),
    Preset("Air UNICOM", 122_800_000, "am", "UNICOM", group="Aviation"),
    Preset("Air air-to-air", 123_450_000, "am", "air-to-air",
           group="Aviation"),
    # Marine and weather -- the channels that mean the same thing wherever you
    # are, and the satellites above the airband.
    Preset("Marine 16", 156_800_000, "nfm", "distress & calling",
           group="Marine"),
    Preset("Weather APT", 137_100_000, "nfm", "NOAA/Meteor APT",
           group="Weather"),
    Preset("NOAA WX-1", 162_550_000, "nfm", "weather, most areas",
           group="Weather"),
]


def presets_by_group() -> list[tuple[str, list[Preset]]]:
    """The presets as the menu's sections, in reading order.

    A section with nothing in it is left out rather than drawn as an empty
    heading: every line in the menu should be something to tune to.
    """
    sections: list[tuple[str, list[Preset]]] = []
    for name in PRESET_GROUPS:
        items = [p for p in PRESETS if p.group == name]
        if items:
            sections.append((name, items))
    return sections


def bands_in_view(lo: float, hi: float) -> list[Band]:
    """Every band that overlaps the window [lo, hi] Hz, lowest first.

    Bands are marked where they are, not only where they start, so a window
    that shows the top of 40 m and the bottom of 41 m gets both -- they do
    overlap, and each is worth a name.  Ordering by the lower limit puts the
    labels on the picture in the order the bands are read; on a tie the
    wider band comes first, so the narrower one is the label that is dropped
    when the two would collide.
    """
    found = [b for b in ALL_BANDS if b.low <= hi and b.high >= lo]
    found.sort(key=lambda b: (b.low, -(b.high - b.low)))
    return found


def find_band(freq: float) -> Band | None:
    for b in ALL_BANDS:
        if b.low <= freq <= b.high:
            return b
    return None


def format_hz(freq: float, decimals: int = 3) -> str:
    """14_070_000 -> '14.070.000' (kHz groups) -- the usual dial readout."""
    hz = int(round(freq))
    khz = hz // 1000
    rest = hz % 1000
    s = f"{khz:,}".replace(",", ".")
    return f"{s}.{rest:03d}" if decimals == 3 else f"{freq/1e6:.{decimals}f}"


def format_mhz(freq: float) -> str:
    return f"{freq / 1e6:.6f}"
