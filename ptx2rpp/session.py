"""
PTX session-level metadata extraction.

Handles session properties such as sample rate, tempo, and timecode origin.
"""

from __future__ import annotations

import struct

from typing import Optional, Tuple

from .ptx import (
    find_by_ct,
    r2,
    r4,
)


DEFAULT_SAMPLE_RATE = 44100


def detect_session_sample_rate(data: bytes, top: list) -> int:
    """Detect the Pro Tools session sample rate."""
    sample_rate = DEFAULT_SAMPLE_RATE

    for block in find_by_ct(top, 0x1028):
        if block[3] + 8 <= len(data):
            candidate = r4(data, block[3] + 4)

            if 8000 <= candidate <= 768000:
                sample_rate = candidate

    return sample_rate

def _walk_blocks(blocks):
    """Recursively yield PTX blocks and their children."""
    for block in blocks:
        yield block
        yield from _walk_blocks(block[4])


def extract_session_tempo(
    data: bytes,
    top: list,
    debug_fn=None,
) -> float:
    """
    Read the constant session tempo from the Pro Tools tempo-map data.

    In the known 86 BPM control session the tempo lives in a 0x2028 block,
    stored as a little-endian IEEE-754 float64. Prefer candidates from
    0x2028 blocks that also contain PT tempo-map markers such as TMS/Const.
    """

    def debug(*args, **kwargs):
        if debug_fn is not None:
            debug_fn(*args, **kwargs)

    candidates = []

    for block in _walk_blocks(top):
        if block[1] != 0x2028:
            continue

        start = block[3]
        end = min(
            len(data),
            start + block[2],
        )

        raw = data[start:end]

        marker_score = 0

        if b"TMS" in raw:
            marker_score += 2

        if b"Const" in raw:
            marker_score += 2

        # Search every byte offset because PT structures are not guaranteed
        # to align doubles on an 8-byte boundary.
        for off in range(
            start,
            max(start, end - 7),
        ):
            try:
                bpm = struct.unpack_from(
                    "<d",
                    data,
                    off,
                )[0]
            except struct.error:
                continue

            if 20.0 <= bpm <= 300.0 and bpm == bpm:
                # Strongly prefer ordinary DAW tempo precision.
                precision_score = (
                    2
                    if abs(bpm - round(bpm, 6)) < 1e-8
                    else 0
                )

                candidates.append(
                    (
                        marker_score + precision_score,
                        bpm,
                        block[3],
                        off,
                    )
                )

    if not candidates:
        debug(
            "  [tempo] No 0x2028 tempo candidate found; "
            "falling back to 120 BPM."
        )
        return 120.0

    # Group identical/near-identical values. Duplicated PT tempo-map
    # structures are common, so repetition is positive evidence.
    grouped = {}

    for score, bpm, block_off, value_off in candidates:
        key = round(bpm, 6)

        group = grouped.setdefault(
            key,
            {
                "score": 0,
                "hits": [],
            },
        )

        group["score"] += score
        group["hits"].append(
            (
                block_off,
                value_off,
            )
        )

    ranked = sorted(
        grouped.items(),
        key=lambda item: (
            item[1]["score"],
            len(item[1]["hits"]),
        ),
        reverse=True,
    )

    bpm, info = ranked[0]

    debug(
        f"  [tempo] Detected constant tempo: {bpm:.6f} BPM "
        f"({len(info['hits'])} matching 0x2028 candidate(s))"
    )

    for block_off, value_off in info["hits"][:4]:
        debug(
            f"          block=0x{block_off:08X} "
            f"value=0x{value_off:08X}"
        )

    return float(bpm)

def extract_session_timecode_origin_samples(
    data: bytes,
    top: list,
    session_rate: int,
    debug_fn=None,
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Decode the PT session timecode origin from the 0x204D timing block.

    Greyscale proved that the unique 0x204D block stores:
      content+2  : UInt32 frame-rate enum (0x02 = 25 fps in this session)
      content+11 : UInt32 session-start frame count

    Important: 0x204D is not always represented in the generic parsed block
    tree, even though it is present in the decrypted PTX. Therefore this
    routine first uses the parsed tree and then falls back to a strict raw
    block-envelope scan for:
        5A <bt> <size> 4D 20

    This is structural scanning, not a search for the numeric value 3600.
    """

    def debug(*args, **kwargs):
        if debug_fn is not None:
            debug_fn(*args, **kwargs)

    blocks = find_by_ct(top, 0x204D)

    content_positions = []

    for block in blocks:
        p = block[3]  # points at the two-byte content type

        if p + 15 <= len(data):
            content_positions.append(p)

    if not content_positions:
        # Strict raw fallback. parse_block() can miss this session-level
        # timing block because of the surrounding PTX envelope hierarchy.
        for pos in range(0x14, len(data) - 15):
            if data[pos] != 0x5A:
                continue

            bt = r2(data, pos + 1)
            bs = r4(data, pos + 3)
            ct = r2(data, pos + 7)

            if ct != 0x204D:
                continue

            if bt & 0xFF00:
                continue

            if bs < 15 or bs > 0x10000:
                continue

            block_end = pos + 7 + bs

            if block_end > len(data):
                continue

            content_positions.append(pos + 7)

    # De-duplicate in case parsed + raw discovery both found the same block.
    content_positions = sorted(set(content_positions))

    if len(content_positions) != 1:
        debug(
            f"  Session TC origin: expected one 0x204D, "
            f"found {len(content_positions)}"
        )
        return None, None, None

    p = content_positions[0]

    frame_rate_enum = r4(data, p + 2)
    origin_frames = r4(data, p + 11)

    if origin_frames == 0:
        debug(
            f"  Session TC origin: enum=0x{frame_rate_enum:02X}, "
            f"frames=0 -> 0 samples"
        )
        return 0, frame_rate_enum, origin_frames

    # Verified from Greyscale:
    #   enum 0x02 + 90,000 frames = 25 fps * 3,600 s = 01:00:00:00.
    if frame_rate_enum == 0x02:
        origin_samples = int(
            round(origin_frames * session_rate / 25.0)
        )

        debug(
            f"  Session TC origin: enum=0x02 (25 fps), "
            f"frames={origin_frames} -> {origin_samples} samples "
            f"({origin_samples / session_rate:.6f}s)"
        )

        return origin_samples, frame_rate_enum, origin_frames

    # Zero origins are safe above. For a non-zero origin at an as-yet
    # unverified enum, leave TC40 correction disabled rather than guessing.
    debug(
        f"  Session TC origin: non-zero origin at unverified frame-rate "
        f"enum=0x{frame_rate_enum:02X}, frames={origin_frames}; "
        f"TC40 correction disabled for safety"
    )

    return None, frame_rate_enum, origin_frames