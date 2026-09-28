"""Pro Tools MIDI parsing and placement resolution."""
import re

from typing import Dict, List, Tuple

from .models import MidiNote, MidiPlacement, MidiRegionData
from .audio import read_pt_string

from .ptx import parse_three_point, r4, r5
from .timing import PT_MIDI_TICKS_PER_QN, ZERO_TICKS

def _walk_blocks(blocks):
    """Yield every parsed PTX block recursively."""
    for block in blocks:
        yield block
        yield from _walk_blocks(block[4])

def _safe_pt_string(
    data: bytes,
    pos: int,
):
    try:
        value, end = read_pt_string(
            data,
            pos,
        )
        return value, end

    except Exception:
        return "", pos


def extract_midi_event_chunks(
    data: bytes,
    top: list,
) -> List[MidiRegionData]:
    """
    Decode MdNLB event chunks using the same 5-byte event representation used
    by libptformat. In this PTX the event data uses 960,000 PT ticks per QN.

    The decoded lists are later linked to PT MIDI regions through the trailing
    u32 in each 0x2633 wrapper.
    """
    chunks = []

    all_blocks = list(
        _walk_blocks(top)
    )

    for block in [
        item
        for item in all_blocks
        if item[1] == 0x2000
    ]:
        start = block[3]
        end = min(
            len(data),
            block[3] + block[2],
        )

        k = start

        while k + 35 < end:
            p = data.find(
                b"MdNLB",
                k,
                end,
            )

            if p < 0:
                break

            q = p + 11

            if q + 9 > end:
                break

            n_events = r4(
                data,
                q,
            )

            q += 4

            zero_ticks = r5(
                data,
                q,
            )

            ep = q
            notes = []

            for _ in range(n_events):
                if ep + 18 > end:
                    break

                raw_pos = r5(
                    data,
                    ep,
                )

                pos = raw_pos - zero_ticks

                if pos < 0:
                    pos = 0

                note = data[ep + 8]

                length = r5(
                    data,
                    ep + 9,
                )

                velocity = data[ep + 17]

                if (
                    0 <= note <= 127
                    and 0 <= velocity <= 127
                    and length >= 0
                ):
                    notes.append(
                        MidiNote(
                            pos,
                            note,
                            length,
                            velocity,
                        )
                    )

                ep += 35

            chunks.append(
                MidiRegionData(
                    len(chunks),
                    f"MIDI Region {len(chunks)}",
                    notes,
                )
            )

            k = max(
                ep,
                p + 5,
            )

    return chunks

def extract_midi_placements(
    data: bytes,
    top: list,
) -> Tuple[Dict[str, List[MidiPlacement]], int]:
    """
    Decode the active 0x1058 -> 0x1057 -> 0x1056 -> 0x104F MIDI playlist map.

    Some PTX sessions repeat the same complete MIDI placement map many times.
    Greyscale contains 188 copies of each logical MIDI placement. These are
    structurally identical references, not 188 intentional stacked clips.

    Collapse only exact duplicates on the same PT track:
        (track_name, region_index, timeline_ticks)

    This is deliberately conservative: different regions or different timeline
    positions remain separate even when their note data happens to match.
    """
    result = {}
    seen_by_track = {}
    duplicate_refs_skipped = 0

    all_blocks = list(
        _walk_blocks(top)
    )

    for midi_block in [
        block
        for block in all_blocks
        if block[1] == 0x1058
    ]:
        for track_block in [
            child
            for child in midi_block[4]
            if child[1] == 0x1057
        ]:
            track_name, _ = _safe_pt_string(
                data,
                track_block[3] + 2,
            )

            if not track_name:
                continue

            output = result.setdefault(
                track_name,
                [],
            )

            seen = seen_by_track.setdefault(
                track_name,
                set(),
            )

            for playlist_block in track_block[4]:
                if playlist_block[1] != 0x1056:
                    continue

                for region_ref in playlist_block[4]:
                    if region_ref[1] != 0x104F:
                        continue

                    j = region_ref[3] + 4

                    if j + 10 > len(data):
                        continue

                    region_index = r4(
                        data,
                        j,
                    )

                    raw_start = r5(
                        data,
                        j + 5,
                    )

                    timeline = (
                        raw_start - ZERO_TICKS
                    )

                    if timeline < 0:
                        timeline = -timeline

                    identity = (
                        region_index,
                        timeline,
                    )

                    if identity in seen:
                        duplicate_refs_skipped += 1
                        continue

                    seen.add(identity)

                    output.append(
                        MidiPlacement(
                            track_name,
                            region_index,
                            timeline,
                        )
                    )

    for placements in result.values():
        placements.sort(
            key=lambda placement: placement.timeline_ticks
        )

    return result, duplicate_refs_skipped

def extract_midi_region_windows(
    data: bytes,
    top: list,
) -> dict:
    """
    Decode PT10+ MIDI region source windows from 0x2634 -> 0x2633 -> 0x2628.

    The region payload contains the same PT 'three point' structure used by
    libptformat: source offset, region length, and original start. MIDI source
    offsets are commonly stored in the absolute ZERO_TICKS domain while the
    original start is already relative musical ticks.

    Returns:
        region_index -> {
            name, source_start, length, original_start, header_offset, block
        }
    """
    all_blocks = list(_walk_blocks(top))
    regions = []

    for parent in [
        block
        for block in all_blocks
        if block[1] == 0x2634
    ]:
        regions.extend(
            [
                child
                for child in parent[4]
                if child[1] == 0x2633
            ]
        )

    result = {}

    for region_index, region in enumerate(regions):
        children = [
            child
            for child in region[4]
            if child[1] == 0x2628
        ]

        if not children:
            continue

        child = children[0]

        lo = child[3]
        hi = min(
            len(data),
            child[3] + child[2],
        )

        candidates = []

        scan_hi = min(
            hi - 10,
            lo + 96,
        )

        for j in range(
            lo,
            max(lo, scan_hi),
        ):
            if j + 10 >= len(data):
                break

            offset_bytes = (
                data[j + 1] & 0xF0
            ) >> 4

            length_bytes = (
                data[j + 2] & 0xF0
            ) >> 4

            start_bytes = (
                data[j + 3] & 0xF0
            ) >> 4

            if not (
                1 <= offset_bytes <= 5
                and 1 <= length_bytes <= 5
                and 1 <= start_bytes <= 5
            ):
                continue

            (
                source_raw,
                length,
                start_raw,
            ) = parse_three_point(
                data,
                j,
            )

            if (
                length <= 0
                or length > 1_000_000_000
            ):
                continue

            source_relative = (
                source_raw - ZERO_TICKS
                if source_raw >= ZERO_TICKS
                else source_raw
            )

            start_relative = (
                start_raw - ZERO_TICKS
                if start_raw >= ZERO_TICKS
                else start_raw
            )

            if not (
                0
                <= source_relative
                <= 2_000_000_000
            ):
                continue

            if not (
                0
                <= start_relative
                <= 2_000_000_000
            ):
                continue

            score = 0

            # PT MIDI source offsets in these sessions are five-byte values.
            if offset_bytes == 5:
                score += 5

            if source_raw >= ZERO_TICKS:
                score += 5

            # Region definitions commonly keep source and original starts close.
            delta = abs(
                source_relative
                - start_relative
            )

            if delta <= 32:
                score += 5

            elif delta <= max(
                length,
                PT_MIDI_TICKS_PER_QN,
            ):
                score += 3

            elif delta <= length * 4:
                score += 1

            # Header byte-count patterns around 5/3-4/4 are common in PT10+.
            if start_bytes == 4:
                score += 2

            if 2 <= length_bytes <= 5:
                score += 1

            candidates.append(
                (
                    score,
                    j,
                    source_relative,
                    int(length),
                    start_relative,
                    source_raw,
                    offset_bytes,
                    length_bytes,
                    start_bytes,
                )
            )

        if not candidates:
            continue

        candidates.sort(
            key=lambda item: (
                -item[0],
                item[1],
            )
        )

        (
            score,
            j,
            source_relative,
            length,
            start_relative,
            source_raw,
            offset_bytes,
            length_bytes,
            start_bytes,
        ) = candidates[0]

        # Pull a readable region name from bytes preceding the three-point
        # header.
        prefix = data[lo:j]

        printable = re.findall(
            rb"[\x20-\x7e]{2,}",
            prefix,
        )

        name = ""

        for piece in printable:
            try:
                text = piece.decode(
                    "utf-8",
                    "replace",
                ).strip("\x00 ").strip()

            except Exception:
                continue

            if (
                text
                and any(
                    character.isalpha()
                    for character in text
                )
            ):
                name = text

        if not name:
            name = (
                f"PT MIDI region "
                f"{region_index}"
            )

        result[region_index] = {
            "name": name,
            "source_start": int(
                source_relative
            ),
            "length": int(length),
            "original_start": int(
                start_relative
            ),
            "header_offset": int(j),
            "header_score": int(score),
            "byte_counts": (
                offset_bytes,
                length_bytes,
                start_bytes,
            ),
            "block": child,
        }

    return result