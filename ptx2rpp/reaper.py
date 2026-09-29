"""REAPER project serialization helpers."""

import hashlib

from typing import List, Optional

from .models import MidiNote
from .timing import ptticks_to_rpp_ppq


def stable_guid(key: str) -> str:
    """Generate a deterministic REAPER-style GUID from a stable text key."""
    digest = hashlib.md5(
        key.encode(
            "utf-8",
            "replace",
        )
    ).hexdigest()

    return (
        "{"
        + digest[0:8]
        + "-"
        + digest[8:12]
        + "-"
        + digest[12:16]
        + "-"
        + digest[16:20]
        + "-"
        + digest[20:32]
        + "}"
    ).upper()


def _quote_rpp_string(value: str) -> str:
    return (
        '"'
        + value.replace(
            '"',
            "'",
        )
        + '"'
    )


def _midi_source_events(
    notes: List[MidiNote],
    source_end_ppq: Optional[int] = None,
):
    """
    Serialize MIDI events and, when known, terminate the MIDI source.

    REAPER-created MIDI sources include a final CC123 (all notes off) event at
    the source boundary. Supplying it makes the source's own length explicit
    instead of leaving REAPER to infer the source boundary from the last note.
    """
    events = []

    for note in notes:
        start = ptticks_to_rpp_ppq(
            note.pos
        )

        end = ptticks_to_rpp_ppq(
            note.pos + note.length
        )

        velocity = max(
            1,
            min(
                127,
                note.velocity,
            ),
        )

        pitch = max(
            0,
            min(
                127,
                note.note,
            ),
        )

        events.append(
            (
                start,
                0x90,
                pitch,
                velocity,
                1,
            )
        )

        events.append(
            (
                end,
                0x80,
                pitch,
                0,
                0,
            )
        )

    events.sort(
        key=lambda item: (
            item[0],
            item[4],
        )
    )

    last = 0
    output = []

    for (
        ppq,
        status,
        data_1,
        data_2,
        _,
    ) in events:
        delta = max(
            0,
            ppq - last,
        )

        output.append(
            f"E {delta} "
            f"{status:02x} "
            f"{data_1:02x} "
            f"{data_2:02x}"
        )

        last = ppq

    if source_end_ppq is not None:
        source_end_ppq = max(
            last,
            int(source_end_ppq),
        )

        output.append(
            f"E {source_end_ppq - last} b0 7b 00"
        )

    return output