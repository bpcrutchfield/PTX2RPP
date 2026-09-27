"""Shared Pro Tools and REAPER timing constants and conversions."""

ZERO_TICKS = 0xE8D4A51000
PT_MIDI_TICKS_PER_QN = 960000
RPP_PPQ = 960


def ptticks_to_seconds(value: int, bpm: float) -> float:
    """Convert Pro Tools musical ticks to seconds."""
    return (value / PT_MIDI_TICKS_PER_QN) * (60.0 / bpm)


def ptticks_to_rpp_ppq(value: int) -> int:
    """Convert Pro Tools musical ticks to REAPER PPQ."""
    return int(round(value * RPP_PPQ / PT_MIDI_TICKS_PER_QN))
