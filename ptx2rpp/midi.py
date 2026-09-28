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

def _scan_mdnlb_catalogue(
    data: bytes,
    top: list,
):
    """
    Evidence-first MdNLB scanner.

    Unlike the MidiRegionData object used by older converter code, this keeps
    the raw 5-byte absolute zero tick read immediately after the event count.

    Layout established from the existing parser:
        'MdNLB' + 6 bytes
        u32 event_count
        u40 zero_tick
        event records (35 bytes each)
    """
    found = []
    seen_offsets = set()

    for block in _walk_blocks(top):
        if block[1] != 0x2000:
            continue

        block_start = block[3]

        block_end = min(
            len(data),
            block_start + block[2],
        )

        pos = block_start

        while True:
            p = data.find(
                b"MdNLB",
                pos,
                block_end,
            )

            if p < 0:
                break

            pos = p + 5

            if p in seen_offsets:
                continue

            seen_offsets.add(p)

            q = p + 11

            if q + 9 > len(data):
                continue

            count = r4(
                data,
                q,
            )

            zero = r5(
                data,
                q + 4,
            )

            # Validate enough bytes exist for the advertised event list.
            event_start = q + 9
            required_end = (
                event_start
                + count * 35
            )

            if (
                count > 1_000_000
                or required_end > len(data)
            ):
                continue

            notes = []

            for note_index in range(count):
                event_pos = (
                    event_start
                    + note_index * 35
                )

                raw_pos = r5(
                    data,
                    event_pos,
                )

                pitch = data[
                    event_pos + 8
                ]

                length = r5(
                    data,
                    event_pos + 9,
                )

                velocity = data[
                    event_pos + 17
                ]

                relative_pos = (
                    raw_pos - zero
                    if raw_pos >= zero
                    else raw_pos
                )

                notes.append(
                    (
                        relative_pos,
                        pitch,
                        length,
                        velocity,
                        raw_pos,
                    )
                )

            found.append(
                {
                    "index": len(found),
                    "offset": p,
                    "count": count,
                    "zero": zero,
                    "zero_rel": (
                        zero - ZERO_TICKS
                        if zero >= ZERO_TICKS
                        else zero
                    ),
                    "notes": notes,
                }
            )

    return found

def build_midi_chunk_windows(
    data: bytes,
    top: list,
):
    """
    Pair the known-working MIDI event decoder with the raw MdNLB zero ticks.

    The older working decoder correctly yields note pitch/velocity/duration.
    _scan_mdnlb_catalogue() is used ONLY for each list's absolute zero tick;
    its older raw-event interpretation is deliberately ignored.
    """
    chunks = extract_midi_event_chunks(
        data,
        top,
    )

    raw = _scan_mdnlb_catalogue(
        data,
        top,
    )

    infos = []

    for index, chunk in enumerate(chunks):
        if index >= len(raw):
            break

        zero_relative = int(
            raw[index]["zero_rel"]
        )

        if chunk.notes:
            relative_start = min(
                note.pos
                for note in chunk.notes
            )

            relative_end = max(
                note.pos + note.length
                for note in chunk.notes
            )

        else:
            relative_start = 0
            relative_end = 0

        infos.append(
            {
                "index": index,
                "chunk": chunk,
                "zero_rel": zero_relative,
                "abs_start": (
                    zero_relative
                    + relative_start
                ),
                "abs_end": (
                    zero_relative
                    + relative_end
                ),
                "span": max(
                    0,
                    relative_end
                    - relative_start,
                ),
                "count": len(
                    chunk.notes
                ),
            }
        )

    return infos

def _collect_direct_midi_region_blocks(top):
    """
    Return direct PT MIDI region table as:
      [(region_index, region_0x2633, child_0x2628), ...]
    """
    all_blocks = list(_walk_blocks(top))
    output = []

    for parent in [
        block
        for block in all_blocks
        if block[1] == 0x2634
    ]:
        direct_regions = [
            child
            for child in parent[4]
            if child[1] == 0x2633
        ]

        for region_block in direct_regions:
            child = next(
                (
                    item
                    for item in region_block[4]
                    if item[1] == 0x2628
                ),
                None,
            )

            output.append(
                (
                    len(output),
                    region_block,
                    child,
                )
            )

    return output

def extract_midi_region_mdnlb_links(
    data: bytes,
    top: list,
) -> dict:
    """
    Direct PT MIDI region -> MdNLB linkage.

    Proven from Silent Longing training mappings:
      r19/r20/r21 -> 6
      r22         -> 13
      r23         -> 2
      r24         -> 14

    In every 0x2633 MIDI-region wrapper, the first 4 bytes immediately AFTER
    the direct 0x2628 child contain the MdNLB list index as little-endian u32.

    Example wrapper-only tails:
      r019 ... 06 00 00 00  -> MdNLB[06]
      r022 ... 0d 00 00 00  -> MdNLB[13]
      r023 ... 02 00 00 00  -> MdNLB[02]
      r024 ... 0e 00 00 00  -> MdNLB[14]

    Returns:
        region_index -> mdnlb_index
    """
    links = {}

    regions = _collect_direct_midi_region_blocks(
        top
    )

    for (
        region_index,
        region_block,
        child,
    ) in regions:
        if child is None:
            continue

        region_end = min(
            len(data),
            region_block[3]
            + region_block[2],
        )

        child_end = (
            child[3]
            + child[2]
        )

        if (
            child_end + 4 > region_end
            or child_end + 4 > len(data)
        ):
            continue

        mdnlb_index = r4(
            data,
            child_end,
        )

        links[region_index] = int(
            mdnlb_index
        )

    return links

def _slice_chunk_to_region(
    chunk_info,
    region_meta,
):
    """
    Crop the underlying MdNLB events to the PT region's source window and
    shift them so the REAPER MIDI item begins at tick zero.
    """
    region_start = region_meta[
        "source_start"
    ]

    region_end = (
        region_start
        + region_meta["length"]
    )

    zero = chunk_info[
        "zero_rel"
    ]

    output = []

    for note in chunk_info["chunk"].notes:
        note_start = (
            zero
            + note.pos
        )

        note_end = (
            note_start
            + note.length
        )

        if (
            note_end <= region_start
            or note_start >= region_end
        ):
            continue

        clipped_start = max(
            note_start,
            region_start,
        )

        clipped_end = min(
            note_end,
            region_end,
        )

        if clipped_end <= clipped_start:
            continue

        output.append(
            MidiNote(
                clipped_start
                - region_start,
                note.note,
                clipped_end
                - clipped_start,
                note.velocity,
            )
        )

    return output

def resolve_midi_placements_direct(
    data: bytes,
    top: list,
):
    """
    Resolve active PT MIDI placements through the proven structural linkage:

        0x2633 MIDI region
          ├─ 0x2628 : region name + source/edit window
          └─ trailing little-endian u32 : exact MdNLB index

    The linked MdNLB list is then cropped to the region source window.
    """
    (
        placements,
        duplicate_refs_skipped,
    ) = extract_midi_placements(
        data,
        top,
    )

    region_meta = extract_midi_region_windows(
        data,
        top,
    )

    chunk_infos = build_midi_chunk_windows(
        data,
        top,
    )

    direct_links = extract_midi_region_mdnlb_links(
        data,
        top,
    )

    chunks_by_index = {
        chunk_info["index"]: chunk_info
        for chunk_info in chunk_infos
    }

    resolved = {}
    unresolved = []

    for (
        track_name,
        track_placements,
    ) in placements.items():

        rows = []

        for placement in track_placements:
            region_index = (
                placement.region_index
            )

            meta = region_meta.get(
                region_index
            )

            mdnlb_index = direct_links.get(
                region_index
            )

            chunk_info = (
                chunks_by_index.get(
                    mdnlb_index
                )
                if mdnlb_index is not None
                else None
            )

            if meta is None:
                unresolved.append(
                    (
                        track_name,
                        region_index,
                        placement.timeline_ticks,
                        "no decoded PT MIDI region window",
                    )
                )
                continue

            if mdnlb_index is None:
                unresolved.append(
                    (
                        track_name,
                        region_index,
                        placement.timeline_ticks,
                        "no trailing MdNLB index in 0x2633",
                    )
                )
                continue

            if chunk_info is None:
                unresolved.append(
                    (
                        track_name,
                        region_index,
                        placement.timeline_ticks,
                        (
                            f"MdNLB index "
                            f"{mdnlb_index} out of range"
                        ),
                    )
                )
                continue

            notes = _slice_chunk_to_region(
                chunk_info,
                meta,
            )

            if not notes:
                unresolved.append(
                    (
                        track_name,
                        region_index,
                        placement.timeline_ticks,
                        (
                            f"MdNLB[{mdnlb_index}] "
                            f"has no notes in region "
                            f"source window"
                        ),
                    )
                )
                continue

            rows.append(
                (
                    placement,
                    chunk_info["chunk"],
                    meta,
                    notes,
                )
            )

        resolved[track_name] = rows

    return (
        placements,
        resolved,
        unresolved,
        duplicate_refs_skipped,
    )