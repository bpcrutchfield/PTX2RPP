"""Tests for Pro Tools and REAPER timing conversions."""

import pytest

from ptx2rpp.timing import (
    PT_MIDI_TICKS_PER_QN,
    RPP_PPQ,
    ptticks_to_rpp_ppq,
    ptticks_to_seconds,
)


def test_one_quarter_note_to_seconds_at_120_bpm() -> None:
    """One quarter note at 120 BPM should last 0.5 seconds."""

    result = ptticks_to_seconds(PT_MIDI_TICKS_PER_QN, 120.0)

    assert result == pytest.approx(0.5)


def test_one_quarter_note_to_seconds_at_60_bpm() -> None:
    """One quarter note at 60 BPM should last exactly one second."""

    result = ptticks_to_seconds(PT_MIDI_TICKS_PER_QN, 60.0)

    assert result == pytest.approx(1.0)


def test_two_quarter_notes_to_seconds() -> None:
    """Two quarter notes should produce twice the quarter-note duration."""

    result = ptticks_to_seconds(PT_MIDI_TICKS_PER_QN * 2, 120.0)

    assert result == pytest.approx(1.0)


def test_zero_ticks_to_seconds() -> None:
    assert ptticks_to_seconds(0, 120.0) == pytest.approx(0.0)


def test_one_pt_quarter_note_to_reaper_ppq() -> None:
    """One Pro Tools quarter note should equal one REAPER quarter note."""

    result = ptticks_to_rpp_ppq(PT_MIDI_TICKS_PER_QN)

    assert result == RPP_PPQ


def test_two_pt_quarter_notes_to_reaper_ppq() -> None:
    result = ptticks_to_rpp_ppq(PT_MIDI_TICKS_PER_QN * 2)

    assert result == RPP_PPQ * 2


def test_half_quarter_note_to_reaper_ppq() -> None:
    result = ptticks_to_rpp_ppq(PT_MIDI_TICKS_PER_QN // 2)

    assert result == RPP_PPQ // 2


def test_zero_ticks_to_reaper_ppq() -> None:
    assert ptticks_to_rpp_ppq(0) == 0
    