"""Band plan and tuning presets for HF / shortwave listening.

Ham segments follow the IARU Region 2 plan (the Americas), which is what is
relevant for a receiver in North America.  Shortwave broadcast bands are the
standard metre bands.
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

# --- quick-jump "stations worth finding" -------------------------------
@dataclass(frozen=True)
class Preset:
    name: str
    freq: float
    mode: str
    note: str = ""


PRESETS: list[Preset] = [
    # Amateur calling / activity
    Preset("80m CW", 3_550_000, "cw", "CW calling"),
    Preset("40m CW", 7_030_000, "cw", "CW calling"),
    Preset("40m SSB", 7_200_000, "lsb", "USA phone"),
    Preset("40m FT8", 7_074_000, "usb", "FT8, 3 kHz wide"),
    Preset("30m FT8", 10_136_000, "usb", "FT8"),
    Preset("20m SSB", 14_250_000, "usb", "USA phone"),
    Preset("20m CW", 14_030_000, "cw", "CW calling"),
    Preset("20m FT8", 14_074_000, "usb", "FT8"),
    Preset("17m SSB", 18_130_000, "usb", "SSB"),
    Preset("15m SSB", 21_300_000, "usb", "SSB"),
    Preset("17m WSPR", 18_104_600, "usb", "WSPR dial"),
    Preset("10m FM", 29_600_000, "nfm", "10m FM simplex"),
    # Time / standards -- excellent for checking your setup
    Preset("WWV 2.5", 2_500_000, "am", "NIST time, AM"),
    Preset("WWV 5", 5_000_000, "am", "NIST time, AM"),
    Preset("WWV 10", 10_000_000, "am", "NIST time, AM"),
    Preset("WWV 15", 15_000_000, "am", "NIST time, AM"),
    Preset("WWV 20", 20_000_000, "am", "NIST time, AM"),
    Preset("WWVH 10", 10_000_000, "am", "Hawaii, same channel"),
    Preset("CHU 3.33", 3_330_000, "am", "Canada time, USB"),
    Preset("CHU 7.85", 7_850_000, "am", "Canada time"),
    Preset("CHU 14.67", 14_670_000, "am", "Canada time"),
    # Broadcasts
    Preset("R. Nacional", 6_180_000, "am", "49m, Amazonia"),
    Preset("BBC WS", 9_410_000, "am", "31m (varies)"),
    Preset("VOA", 15_580_000, "am", "19m (varies)"),
    Preset("WWCR", 13_845_000, "am", "22m Nashville"),
    # Utility / marine
    Preset("Volmet 5", 5_598_000, "usb", "HF VOLMET USB"),
    Preset("HFDL", 10_081_000, "usb", "aircraft HFDL"),
]


def find_band(freq: float) -> Band | None:
    for b in HAM_BANDS:
        if b.low <= freq <= b.high:
            return b
    for b in SW_BANDS:
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
