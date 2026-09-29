"""REAPER project serialization helpers."""

import hashlib
import time

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

def project_header_lines(
    tempo_bpm: float,
    sample_rate: int,
):
    """Build the opening REAPER project lines."""
    return [
        f'<REAPER_PROJECT 0.1 "7.0/win64" {int(time.time())}>',
        f"TEMPO {tempo_bpm:.10f} 4 4",
        f"SAMPLERATE {sample_rate} 0 0",
        "LOOP 0",
    ]


def memory_location_marker_lines(
    memory_locations,
    origin_seconds: float,
):
    """
    Convert Pro Tools point Memory Locations into REAPER MARKER lines.
    """
    lines = []

    used_marker_ids = set()
    next_marker_id = 1

    for marker in memory_locations or []:
        requested_id = int(
            marker.get("index", 0) or 0
        )

        if (
            requested_id > 0
            and requested_id not in used_marker_ids
        ):
            marker_id = requested_id
        else:
            while next_marker_id in used_marker_ids:
                next_marker_id += 1

            marker_id = next_marker_id

        used_marker_ids.add(
            marker_id
        )

        next_marker_id = max(
            next_marker_id,
            marker_id + 1,
        )

        marker_pos = (
            float(
                marker["position_seconds"]
            )
            - origin_seconds
        )

        marker_name = (
            str(
                marker.get("name", "")
            )
            .replace('"', "'")
            .strip()
        )

        marker_key = (
            f"PT_MARKER|{marker_id}|"
            f"{marker_name}|"
            f"{marker_pos:.12f}"
        )

        lines.append(
            f'MARKER {marker_id} '
            f'{marker_pos:.12f} '
            f'"{marker_name}" 0 0 1 R '
            f'{stable_guid(marker_key)} 0'
        )

    return lines


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