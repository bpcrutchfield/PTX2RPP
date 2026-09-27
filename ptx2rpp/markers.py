"""Pro Tools Memory Location parsing."""

from typing import List

from .ptx import r2, r4, r5


ZERO_TICKS = 0xE8D4A51000
PT_MIDI_TICKS_PER_QN = 960000


def _ptticks_to_seconds(value: int, bpm: float) -> float:
    """Convert Pro Tools musical ticks to seconds."""
    return (value / PT_MIDI_TICKS_PER_QN) * (60.0 / bpm)


def extract_memory_locations(
    data: bytes,
    sample_rate: int,
    tempo_bpm: float,
    verbose: bool = False,
) -> List[dict]:
    """
    Extract verified Pro Tools point Memory Locations from 0x2077 blocks.

    Two timestamp encodings are present in the PTX corpus used by PTX2RPP:

      03 09 00 00
        Absolute UInt64 sample position.
        Verified in Greyscale and Getting Away With Murder.

      01 09 00 00
        Five-byte Pro Tools musical-tick position, using ZERO_TICKS.
        Verified in Headrush and Silent Longing.

    Pro Tools stores the marker catalogue twice in the tested sessions, so
    exact (index, name, position) duplicates are collapsed.

    Selection/range Memory Locations and unknown 0x2077 layouts are deliberately
    ignored until their structure is verified.
    """
    found = []
    seen = set()

    # Marker blocks can live inside a PTX envelope that the generic tree parser
    # does not always expose cleanly. Use the same strict raw-envelope strategy
    # as the session-timecode-origin reader.
    for pos in range(0x14, len(data) - 24):
        if data[pos] != 0x5A:
            continue

        bt = r2(data, pos + 1)
        bs = r4(data, pos + 3)
        ct = r2(data, pos + 7)

        if ct != 0x2077 or bt != 0x000C:
            continue

        if bs < 24 or bs > 0x10000:
            continue

        if pos + 7 + bs > len(data):
            continue

        q = pos + 9  # payload immediately after the 0x2077 content type

        if q + 10 > len(data):
            continue

        marker_index = r2(data, q)
        layout = data[q + 2:q + 6]
        name_len = r4(data, q + 6)

        if marker_index <= 0 or marker_index > 0xFFFF:
            continue

        if name_len > 4096:
            continue

        name_start = q + 10
        name_end = name_start + name_len

        if name_end + 8 > pos + 7 + bs:
            continue

        try:
            name = data[name_start:name_end].decode("utf-8")
        except UnicodeDecodeError:
            name = data[name_start:name_end].decode("utf-8", "replace")

        time_pos = name_end

        if layout == b"\x03\x09\x00\x00":
            # Sample-based Memory Location.
            raw_samples = int.from_bytes(
                data[time_pos:time_pos + 8],
                byteorder="little",
                signed=True,
            )

            if raw_samples < 0:
                continue

            position_seconds = raw_samples / float(sample_rate)
            timebase = "samples"

        elif layout == b"\x01\x09\x00\x00":
            # Musical-tick Memory Location. The meaningful timestamp is the
            # first five bytes; the remaining bytes belong to the PT marker
            # record rather than an ordinary UInt64 sample count.
            raw_ticks = r5(data, time_pos)

            timeline_ticks = (
                raw_ticks - ZERO_TICKS
                if raw_ticks >= ZERO_TICKS
                else raw_ticks
            )

            position_seconds = _ptticks_to_seconds(
                timeline_ticks,
                tempo_bpm,
            )

            timebase = "ticks"

        else:
            if verbose:
                print(
                    f"  [marker] ignored unverified 0x2077 layout "
                    f"{layout.hex(' ')} at 0x{pos:X}"
                )
            continue

        identity = (
            marker_index,
            name,
            round(position_seconds, 9),
        )

        if identity in seen:
            continue

        seen.add(identity)

        found.append(
            {
                "index": marker_index,
                "name": name,
                "position_seconds": position_seconds,
                "timebase": timebase,
                "block_offset": pos,
            }
        )

    found.sort(
        key=lambda marker: (
            marker["position_seconds"],
            marker["index"],
            marker["name"],
        )
    )

    return found
