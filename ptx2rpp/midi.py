"""Pro Tools MIDI parsing and placement resolution."""

from typing import Dict, List, Tuple

from .models import MidiNote, MidiPlacement, MidiRegionData
from .ptx import r4, r5
from .audio import read_pt_string
from .timing import ZERO_TICKS

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