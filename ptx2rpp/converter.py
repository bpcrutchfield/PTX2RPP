#!/usr/bin/env python3
"""
PTX2RPP
=======

Production Pro Tools .ptx -> REAPER .rpp converter.

Current supported conversion:
  - PTX decryption for the PT10-12 family used by the tested sessions
  - Audio track names
  - Audio clip positions, lengths and source offsets
  - Registered audio-file mapping
  - Active/inactive PT audio-placement state filtering
  - MIDI track names and active region placements
  - MIDI note pitch, velocity, start and duration
  - Direct PT MIDI region -> MdNLB linkage from the trailing u32 in 0x2633
  - Constant session tempo
  - Pro Tools point Memory Locations / markers
  - REAPER project generation
  - Non-pooled MIDI items for compatibility across REAPER versions

Known current limitations:
  - Stereo PT audio channels are emitted as separate mono REAPER tracks
  - Plugins, sends, routing, automation, fades and mixer state are not converted
  - Tempo-map changes are not yet converted; constant tempo is supported

Usage:
  python -X utf8 ptx_to_reaper.py "C:\\path\\to\\Session.ptx"
  python -X utf8 ptx_to_reaper.py "C:\\path\\to\\Session Folder"
  python -X utf8 ptx_to_reaper.py "C:\\path\\to\\Session Folder" "Session.ptx"

Options:
  --output PATH       Override the .rpp output path
  --audio-dir PATH    Override the Audio Files directory
  --verbose           Print detailed parser diagnostics
  --max-gap-heal-ms N Extend short audio items across gaps up to N ms
  --playlists-to-lanes Convert detected PT playlists to REAPER fixed lanes
  --strict            Exit non-zero if any active audio/MIDI placement cannot be written
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import struct
import sys
import time
import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from .models import (
    AudioTrack,
    ClipPlacement,
    MidiNote,
    MidiPlacement,
    MidiRegionData,
    PlaylistLaneGroup,
    Region,
)
from .ptx import (
    decrypt_ptx,
    find_by_ct,
    find_top,
    parse_three_point,
    r2,
    r4,
    r5,
)
from .markers import extract_memory_locations

from .timing import (
    PT_MIDI_TICKS_PER_QN,
    RPP_PPQ,
    ZERO_TICKS,
    ptticks_to_rpp_ppq,
    ptticks_to_seconds,
)

from .audio import (
    build_registered_media_map,
    build_wav_index,
    extract_audio_files,
    extract_audio_tracks,
    extract_regions,
    match_regions_to_wavs,
    read_pt_string,
)

APP_NAME = "PTX2RPP"
APP_VERSION = "1.2.0-memory-locations"
DEFAULT_SAMPLE_RATE = 44100


TRACK_COLOURS = [
    0x0094FF, 0xFF6B35, 0x00C853, 0xFF1744, 0xAA00FF,
    0x00BCD4, 0xFFAB40, 0x76FF03, 0xF50057, 0x448AFF,
]

VERBOSE = False


def debug(*args, **kwargs) -> None:
    if VERBOSE:
        print(*args, **kwargs)


def stable_guid(key: str) -> str:
    """Generate a deterministic REAPER-style GUID from a stable text key."""
    digest = hashlib.md5(key.encode("utf-8", "replace")).hexdigest()
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


def resolve_session_input(
    input_path: str,
    explicit_ptx_name: Optional[str] = None,
    audio_override: Optional[str] = None,
    output_override: Optional[str] = None,
) -> Tuple[Path, Path, Path]:
    """
    Return (ptx_file, audio_dir, output_rpp).

    Accepts either a .ptx file or a session directory.  If a directory contains
    multiple PTX files, the user must specify the desired filename.
    """
    p = Path(input_path).expanduser()

    if explicit_ptx_name:
        session_dir = p
        ptx_file = session_dir / explicit_ptx_name
    elif p.suffix.lower() == ".ptx":
        ptx_file = p
        session_dir = p.parent
    else:
        session_dir = p
        if not session_dir.exists():
            raise FileNotFoundError(f"Session path does not exist: {session_dir}")

        candidates = sorted(session_dir.glob("*.ptx"))
        if not candidates:
            raise FileNotFoundError(f"No .ptx file found in: {session_dir}")
        if len(candidates) > 1:
            names = "\n  ".join(x.name for x in candidates)
            raise RuntimeError(
                "More than one .ptx file exists in the session folder.\n"
                "Specify the filename as the second positional argument:\n  "
                + names
            )
        ptx_file = candidates[0]

    if not ptx_file.is_file():
        raise FileNotFoundError(f"PTX file not found: {ptx_file}")

    audio_dir = (
        Path(audio_override).expanduser()
        if audio_override
        else session_dir / "Audio Files"
    )
    output_rpp = (
        Path(output_override).expanduser()
        if output_override
        else session_dir / f"{ptx_file.stem}.rpp"
    )

    return ptx_file.resolve(), audio_dir.resolve(), output_rpp.resolve()



    def rle(pos, n):
        if n == 0 or pos + n > len(data):
            return 0
        v = 0
        for k in range(n):
            v |= data[pos+k] << (8*k)
        return v

    src_off = rle(base, offsetbytes)
    length  = rle(base + offsetbytes, lengthbytes)
    start   = rle(base + offsetbytes + lengthbytes, startbytes)
    return src_off, length, start


def extract_session_timecode_origin_samples(
    data: bytes,
    top: list,
    session_rate: int,
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Decode the PT session timecode origin from the 0x204D timing block.

    Greyscale proved that the unique 0x204D block stores:
      content+2  : UInt32 frame-rate enum (0x02 = 25 fps in this session)
      content+11 : UInt32 session-start frame count

    Important: 0x204D is not always represented in the generic parsed block
    tree, even though it is present in the decrypted PTX.  Therefore this
    routine first uses the parsed tree and then falls back to a strict raw
    block-envelope scan for:
        5A <bt> <size> 4D 20
    This is structural scanning, not a search for the numeric value 3600.
    """
    blocks = find_by_ct(top, 0x204D)

    content_positions = []
    for b in blocks:
        p = b[3]  # points at the two-byte content type
        if p + 15 <= len(data):
            content_positions.append(p)

    if not content_positions:
        # Strict raw fallback.  parse_block() can miss this session-level
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
        origin_samples = int(round(origin_frames * session_rate / 25.0))
        debug(
            f"  Session TC origin: enum=0x02 (25 fps), "
            f"frames={origin_frames} -> {origin_samples} samples "
            f"({origin_samples/session_rate:.6f}s)"
        )
        return origin_samples, frame_rate_enum, origin_frames

    # Zero origins are safe above.  For a non-zero origin at an as-yet
    # unverified enum, leave TC40 correction disabled rather than guessing.
    debug(
        f"  Session TC origin: non-zero origin at unverified frame-rate "
        f"enum=0x{frame_rate_enum:02X}, frames={origin_frames}; "
        f"TC40 correction disabled for safety"
    )
    return None, frame_rate_enum, origin_frames

def assign_regions_to_tracks(
    data: bytes,
    top: list,
    tracks: List[AudioTrack],
    regions: List[Region],
    session_rate: int,
    session_timecode_origin_samples: Optional[int] = None,
) -> Tuple[Dict[str, List[ClipPlacement]], int, int]:
    """
    Build actual timeline placements instead of treating Region definitions
    themselves as timeline clips.

    Each PT channel gets its own REAPER track for now.  This is deliberate:
    PT stereo tracks are stored as paired mono channel maps, and keeping the
    channels separate is lossless while we continue reverse-engineering the
    stereo relationship.
    """
    channel_tracks = []
    for logical_index, track in enumerate(tracks):
        for channel_number, channel_id in enumerate(track.channel_ids):
            channel_tracks.append({
                "id": channel_id,
                "name": track.name,
                "logical_index": logical_index,
                "channel_number": channel_number,
            })

    debug(f"  PT track definitions: {len(tracks)}")
    debug(f"  PT channel-track entries: {len(channel_tracks)}")
    for i, t in enumerate(channel_tracks):
        debug(
            f"    [{i:02d}] id={t['id']} logical={t['logical_index']} "
            f"channel={t['channel_number']} name='{t['name']}'"
        )

    # Separate mono channel tracks are the safest representation in REAPER.
    track_dict: Dict[str, List[ClipPlacement]] = {}
    region_by_index = {r.index: r for r in regions}

    full_maps = [b for b in top if b[1] == 0x1054]
    if not full_maps:
        seen = set()
        full_maps = []
        for b in find_by_ct(top, 0x1054):
            if b[3] not in seen:
                seen.add(b[3])
                full_maps.append(b)

    debug(f"  PT8+ 0x1054 maps: {len(full_maps)}")
    total_placements = 0
    inactive_placements_skipped = 0
    timestamp_positions_corrected = 0

    for map_no, full_map in enumerate(full_maps):
        map_entries = [c for c in full_map[4] if c[1] == 0x1052]
        debug(f"  MAP BLOCK #{map_no} offset=0x{full_map[3]:08X}")
        debug(f"    0x1052 entries: {len(map_entries)}")

        for count, map_entry in enumerate(map_entries):
            entry_label, _ = read_pt_string(data, map_entry[3] + 2)

            if count < len(channel_tracks):
                ct = channel_tracks[count]
                logical_name = ct["name"]
                channel_id = ct["id"]
                channel_number = ct["channel_number"]
            else:
                logical_name = entry_label or f"_map_{count}"
                channel_id = -1
                channel_number = count

            suffix = (
                "L" if channel_number == 0
                else "R" if channel_number == 1
                else f"ch{channel_number+1}"
            )

            # 0x1052's own label is the playlist/map identity.  For ordinary
            # tracks it normally matches the logical 0x1014 name; for alternate
            # Pro Tools playlists it carries names such as:
            #   Vocals Chorus.01
            #   Vocals Chorus.02
            # etc.
            #
            # Keep that identity instead of replacing it with the parallel
            # 0x1014 name by array position.
            mapped_name = entry_label.strip() if entry_label else logical_name
            if not mapped_name:
                mapped_name = logical_name or f"_map_{count}"

            output_track = f"{mapped_name} [{suffix}]"
            track_dict.setdefault(output_track, [])

            placements = [d for d in map_entry[4] if d[1] == 0x1050]
            debug(
                f"    ENTRY [{count:02d}] label={entry_label!r} "
                f"-> '{output_track}' placements={len(placements)}"
            )

            for placement_no, placement in enumerate(placements):
                # Keep the old fade test only as a diagnostic.  The earlier
                # +46 byte sometimes points beyond the 0x1050 payload, so v8
                # does not discard a placement on that basis.
                refs = [e for e in placement[4] if e[1] == 0x104F]

                for ref_no, ref in enumerate(refs):
                    j = ref[3] + 4
                    if j + 16 > len(data):
                        continue

                    raw_index = r4(data, j)
                    raw_start5 = r5(data, j + 5)

                    ZERO_TICKS = 0xE8D4A51000
                    timeline_start = (
                        raw_start5 - ZERO_TICKS
                        if raw_start5 >= ZERO_TICKS
                        else raw_start5
                    )

                    region = region_by_index.get(raw_index)
                    if region is None:
                        debug(
                            f"      SKIP p={placement_no:02d} r={ref_no:02d} "
                            f"unknown region index {raw_index}"
                        )
                        continue

                    meta = data[j + 10:j + 16]
                    placement_kind = data[j + 13] if j + 13 < len(data) else None

                    # meta[2] == 0x40 marks the timestamp/original-position
                    # class observed in Greyscale.  For these live placements,
                    # the visible PT position is reconstructed from the region's
                    # absolute media timestamp rather than the ordinary 0x104F
                    # position field:
                    #
                    #   region.start - region.src_offset - session TC origin
                    #
                    # Bass.cm-01.L: 3604.909104 - 0.545458 - 3600
                    #               = 4.363646 s (PT displays 0:04.363).
                    timestamp_positioned = (
                        len(meta) >= 3 and meta[2] == 0x40
                    )
                    if (
                        timestamp_positioned
                        and session_timecode_origin_samples is not None
                    ):
                        timestamp_start = (
                            region.start
                            - region.src_offset
                            - session_timecode_origin_samples
                        )
                        if timestamp_start >= 0:
                            debug(
                                f"      TC40 idx={raw_index:02d} "
                                f"0x104F={timeline_start/session_rate:9.3f}s "
                                f"timestamp={timestamp_start/session_rate:9.3f}s "
                                f"name='{region.name}'"
                            )
                            timeline_start = timestamp_start
                            timestamp_positions_corrected += 1
                        else:
                            debug(
                                f"      TC40 correction rejected idx={raw_index:02d}: "
                                f"derived negative position "
                                f"{timestamp_start/session_rate:.6f}s"
                            )

                    # PT8+ 0x1050 contains the 0x104F region reference plus a
                    # one-byte trailing placement-state value.  Reverse-
                    # engineering across several sessions shows:
                    #
                    #   kind 0x03 + state 0x00 -> live/visible timeline clip
                    #   kind 0x03 + state 0x01 -> stale/inactive edit reference
                    #
                    # The latter is the source of the occasional "rogue audio"
                    # items: the reference can sit structurally under an
                    # unrelated track even though Pro Tools does not present it
                    # as an active clip.
                    #
                    # placement[3] is pos+7 and placement[2] is the block size,
                    # so the final byte of the 0x1050 block payload is:
                    placement_state_pos = placement[3] + placement[2] - 1
                    placement_state = (
                        data[placement_state_pos]
                        if 0 <= placement_state_pos < len(data)
                        else None
                    )

                    # j+13 separates the main live/aux reference classes.
                    if placement_kind != 0x03:
                        debug(
                            f"      AUX  p={placement_no:02d} idx={raw_index:02d} "
                            f"kind=0x{placement_kind:02X} "
                            f"state={placement_state!r} "
                            f"at={timeline_start/session_rate:9.3f}s "
                            f"name='{region.name}'"
                        )
                        continue

                    # A 0x03 reference with trailing state 0x01 is not a live
                    # timeline clip.  This structural test supersedes the
                    # filename/track-name heuristics for this class of rogue
                    # placement.
                    if placement_state == 0x01:
                        inactive_placements_skipped += 1
                        debug(
                            f"      INACTIVE p={placement_no:02d} idx={raw_index:02d} "
                            f"kind=0x03 state=0x01 "
                            f"at={timeline_start/session_rate:9.3f}s "
                            f"name='{region.name}'"
                        )
                        continue

                    cp = ClipPlacement(
                        region=region,
                        track_name=mapped_name,
                        channel_id=channel_id,
                        channel_number=channel_number,
                        timeline_start=timeline_start,
                        raw_start5=raw_start5,
                        meta=meta,
                    )
                    track_dict[output_track].append(cp)
                    total_placements += 1

                    debug(
                        f"      CLIP p={placement_no:02d} idx={raw_index:02d} "
                        f"at={timeline_start/session_rate:9.3f}s "
                        f"len={region.length/session_rate:8.3f}s "
                        f"name='{region.name}'"
                    )

    for placements in track_dict.values():
        placements.sort(key=lambda p: p.timeline_start)

    debug()
    debug(f"  Active timeline placements created: {total_placements}")
    debug(f"  Inactive/stale 0x03 placements skipped: {inactive_placements_skipped}")
    debug("  Region definitions not referenced by the active map are intentionally "
          "not written to the RPP.")

    return track_dict, inactive_placements_skipped, timestamp_positions_corrected


def dedupe_redundant_same_track_audio(
    track_dict: Dict[str, List[ClipPlacement]],
) -> int:
    """
    Collapse redundant same-track audio placements that reproduce the exact
    same source samples from the exact same timeline start.

    Identity:
        registered PT source file + timeline start + source offset

    When several Region/edit definitions share that identity, the longest one
    completely contains the shorter copies and plays the same source samples.
    Keep the longest placement; first structural occurrence wins equal lengths.

    Different source files, source offsets and timeline positions remain
    completely independent.
    """
    removed = 0

    for track_name, placements in list(track_dict.items()):
        if len(placements) < 2:
            continue

        groups = {}

        for order, cp in enumerate(placements):
            r = cp.region

            if r.file_index is not None and r.file_index >= 0:
                source_identity = ("file-index", int(r.file_index))
            elif r.wav_file:
                source_identity = ("wav", str(r.wav_file).lower())
            else:
                # Without source identity, only repeats of the exact Region
                # definition are safe to collapse.
                source_identity = ("region-index", int(r.index))

            key = (
                source_identity,
                int(cp.timeline_start),
                int(r.src_offset),
            )
            groups.setdefault(key, []).append((order, cp))

        kept = []

        for key, members in groups.items():
            if len(members) == 1:
                kept.append(members[0])
                continue

            winner_order, winner = max(
                members,
                key=lambda pair: (int(pair[1].region.length), -pair[0]),
            )
            kept.append((winner_order, winner))
            removed += len(members) - 1

            debug(
                f"  [audio] same-track duplicate group on {track_name!r}: "
                f"kept {winner.region.name!r} "
                f"(len={winner.region.length}), removed {len(members)-1}"
            )

        kept.sort(key=lambda pair: pair[0])
        track_dict[track_name] = [cp for _, cp in kept]

    return removed


def _audio_name_match_score(track_name: str, region: Region) -> int:
    """
    Conservative affinity score used only to arbitrate a region that appears
    on more than one PT audio track.

    This is not used for normal track assignment.
    """
    def norm(x: str) -> str:
        x = x.lower().strip()
        x = re.sub(r"\s+\[[lrc]+\]$", "", x)
        return x

    track = norm(track_name)
    clip = norm(region.name)
    wav = norm(Path(region.wav_file).stem if region.wav_file else "")

    score = 0
    if track and clip.startswith(track):
        score += 4
    if track and wav.startswith(track):
        score += 4
    if track and track in clip:
        score += 2
    if track and track in wav:
        score += 2
    return score


def _audio_region_alias_key(region: Region):
    """
    Return a conservative identity for cross-track alias arbitration.

    Pro Tools can contain duplicate Region definitions with different region
    indexes even though they have the same clip name and registered source
    audio file.  Group those definitions together so the alias-pruner can
    recognise them as siblings.

    The registered PT file_index is deliberately part of the key.  Two clips
    with the same visible name but different source files are therefore NOT
    treated as aliases.

    If the source file index is unavailable, fall back to the original
    region-index identity rather than guessing.
    """
    if region.file_index is None or region.file_index < 0:
        return ("region-index", region.index)

    name = (region.name or "").strip().lower()
    name = re.sub(r"\s+", " ", name)

    if not name:
        return ("region-index", region.index)

    return ("pt-source-name", region.file_index, name)


def prune_spurious_cross_track_audio(
    track_dict: Dict[str, List[ClipPlacement]],
) -> int:
    """
    Remove only high-confidence cross-track alias placements.

    PT 0x1054/0x1052 maps can contain references which look active but belong
    to another source/playlist context. We avoid broad filename filtering.

    A placement is removed only when:
      1. the same PT source/clip identity appears on multiple REAPER tracks;
         this includes duplicate PT Region definitions that have different
         region indexes but the same clip name + registered source file;
      2. exactly one of those tracks has clearly stronger name/source affinity;
      3. the weaker placement overlaps another item already on its own track.

    This keeps legitimate copied clips on otherwise empty/non-overlapping
    tracks while removing high-confidence foreign aliases.

    Important safety rule:
      - If PT source-file identity is unavailable, grouping falls back to the
        exact Region index used by earlier PTX2RPP versions.
    """
    by_identity: Dict[tuple, List[Tuple[str, ClipPlacement]]] = {}

    for output_track, placements in track_dict.items():
        for cp in placements:
            key = _audio_region_alias_key(cp.region)
            by_identity.setdefault(key, []).append((output_track, cp))

    remove_ids = set()

    for identity, occurrences in by_identity.items():
        track_names = {track_name for track_name, _ in occurrences}
        if len(track_names) < 2:
            continue

        scored = [
            (
                _audio_name_match_score(track_name, cp.region),
                track_name,
                cp,
            )
            for track_name, cp in occurrences
        ]

        best_score = max(score for score, _, _ in scored)
        best_tracks = {
            track_name
            for score, track_name, _ in scored
            if score == best_score
        }

        # Require one unique, clearly matching owner.
        if best_score < 4 or len(best_tracks) != 1:
            continue

        owner_track = next(iter(best_tracks))

        for score, track_name, cp in scored:
            if track_name == owner_track or score >= best_score:
                continue

            start = cp.timeline_start
            end = start + max(0, cp.region.length)

            # Preserve legitimate cross-track copies unless this foreign
            # placement actually collides with material already belonging to
            # the destination track.
            overlaps_other = False

            for other in track_dict.get(track_name, []):
                if other is cp:
                    continue

                o_start = other.timeline_start
                o_end = o_start + max(0, other.region.length)

                if start < o_end and o_start < end:
                    overlaps_other = True
                    break

            if not overlaps_other:
                continue

            remove_ids.add(id(cp))

            sibling_indexes = sorted(
                {
                    sibling.region.index
                    for _, sibling in occurrences
                }
            )

            debug(
                f"  [audio] pruned cross-track alias: "
                f"{cp.region.name!r} region={cp.region.index} "
                f"from {track_name!r}; strong owner={owner_track!r}; "
                f"source_file_index={cp.region.file_index}; "
                f"sibling_regions={sibling_indexes}"
            )

    if not remove_ids:
        return 0

    removed = 0

    for track_name in list(track_dict):
        before = len(track_dict[track_name])

        track_dict[track_name] = [
            cp
            for cp in track_dict[track_name]
            if id(cp) not in remove_ids
        ]

        removed += before - len(track_dict[track_name])

    return removed




def extract_midi_event_chunks(data: bytes, top: list) -> List[MidiRegionData]:
    """
    Decode MdNLB event chunks using the same 5-byte event representation used
    by libptformat. In this PTX the event data uses 960,000 PT ticks per QN.
    The decoded lists are later linked to PT MIDI regions through the trailing u32 in each 0x2633 wrapper.
    """
    chunks = []
    all_blocks = list(_walk_blocks(top))
    for b in [x for x in all_blocks if x[1] == 0x2000]:
        start = b[3]
        end = min(len(data), b[3] + b[2])
        k = start
        while k + 35 < end:
            p = data.find(b"MdNLB", k, end)
            if p < 0:
                break
            q = p + 11
            if q + 9 > end:
                break
            n_events = r4(data, q)
            q += 4
            zero_ticks = r5(data, q)
            ep = q
            notes = []
            for _ in range(n_events):
                if ep + 18 > end:
                    break
                raw_pos = r5(data, ep)
                pos = raw_pos - zero_ticks
                if pos < 0:
                    pos = 0
                note = data[ep + 8]
                length = r5(data, ep + 9)
                velocity = data[ep + 17]
                if 0 <= note <= 127 and 0 <= velocity <= 127 and length >= 0:
                    notes.append(MidiNote(pos, note, length, velocity))
                ep += 35
            chunks.append(MidiRegionData(len(chunks), f"MIDI Region {len(chunks)}", notes))
            k = max(ep, p + 5)
    return chunks


def extract_midi_placements(
    data: bytes,
    top: list,
) -> Tuple[Dict[str, List[MidiPlacement]], int]:
    """
    Decode the active 0x1058 -> 0x1057 -> 0x1056 -> 0x104F MIDI playlist map.

    Some PTX sessions repeat the same complete MIDI placement map many times.
    Greyscale contains 188 copies of each logical MIDI placement.  These are
    structurally identical references, not 188 intentional stacked clips.

    Collapse only exact duplicates on the same PT track:
        (track_name, region_index, timeline_ticks)

    This is deliberately conservative: different regions or different timeline
    positions remain separate even when their note data happens to match.
    """
    result = {}
    seen_by_track = {}
    duplicate_refs_skipped = 0

    all_blocks = list(_walk_blocks(top))
    for mb in [b for b in all_blocks if b[1] == 0x1058]:
        for c in [x for x in mb[4] if x[1] == 0x1057]:
            track_name, _ = _safe_pt_string(data, c[3] + 2)
            if not track_name:
                continue

            out = result.setdefault(track_name, [])
            seen = seen_by_track.setdefault(track_name, set())

            for d in c[4]:
                if d[1] != 0x1056:
                    continue
                for e in d[4]:
                    if e[1] != 0x104F:
                        continue
                    j = e[3] + 4
                    if j + 10 > len(data):
                        continue

                    region_index = r4(data, j)
                    raw_start = r5(data, j + 5)
                    timeline = raw_start - ZERO_TICKS
                    if timeline < 0:
                        timeline = -timeline

                    identity = (region_index, timeline)
                    if identity in seen:
                        duplicate_refs_skipped += 1
                        continue

                    seen.add(identity)
                    out.append(
                        MidiPlacement(track_name, region_index, timeline)
                    )

    for ps in result.values():
        ps.sort(key=lambda p: p.timeline_ticks)

    return result, duplicate_refs_skipped


def extract_session_tempo(data: bytes, top: list) -> float:
    """
    PTX constant-tempo reader.

    In our known 86 BPM control session the tempo lives in a 0x2028 block,
    stored as a little-endian IEEE-754 float64.  Prefer candidates from
    0x2028 blocks that also contain PT tempo-map markers such as TMS/Const.
    """
    import struct

    candidates = []
    for b in _walk_blocks(top):
        if b[1] != 0x2028:
            continue
        start = b[3]
        end = min(len(data), start + b[2])
        raw = data[start:end]
        marker_score = 0
        if b"TMS" in raw:
            marker_score += 2
        if b"Const" in raw:
            marker_score += 2

        # Search every byte offset because PT structures are not guaranteed
        # to align doubles on an 8-byte boundary.
        for off in range(start, max(start, end - 7)):
            try:
                bpm = struct.unpack_from("<d", data, off)[0]
            except struct.error:
                continue
            if 20.0 <= bpm <= 300.0 and bpm == bpm:
                # Strongly prefer ordinary DAW tempo precision.
                precision_score = 2 if abs(bpm - round(bpm, 6)) < 1e-8 else 0
                # 48000 etc. are excluded by the BPM range.
                candidates.append(
                    (marker_score + precision_score, bpm, b[3], off)
                )

    if not candidates:
        debug("  [tempo] No 0x2028 tempo candidate found; falling back to 120 BPM.")
        return 120.0

    # Group identical/near-identical values. Duplicated PT tempo-map structures
    # are common, so repetition is positive evidence.
    grouped = {}
    for score, bpm, block_off, value_off in candidates:
        key = round(bpm, 6)
        g = grouped.setdefault(key, {"score": 0, "hits": []})
        g["score"] += score
        g["hits"].append((block_off, value_off))

    ranked = sorted(
        grouped.items(),
        key=lambda kv: (kv[1]["score"], len(kv[1]["hits"])),
        reverse=True,
    )

    bpm, info = ranked[0]
    debug(
        f"  [tempo] Detected constant tempo: {bpm:.6f} BPM "
        f"({len(info['hits'])} matching 0x2028 candidate(s))"
    )
    for block_off, value_off in info["hits"][:4]:
        debug(
            f"          block=0x{block_off:08X} value=0x{value_off:08X}"
        )
    return float(bpm)




def _midi_source_events(
    notes: List[MidiNote],
    source_end_ppq: Optional[int] = None,
):
    """Serialize MIDI events and, when known, terminate the MIDI source.

    REAPER-created MIDI sources include a final CC123 (all notes off) event at
    the source boundary.  Supplying it makes the source's own length explicit
    instead of leaving REAPER to infer the source boundary from the last note.
    """
    events = []
    for n in notes:
        start = ptticks_to_rpp_ppq(n.pos)
        end = ptticks_to_rpp_ppq(n.pos + n.length)
        vel = max(1, min(127, n.velocity))
        pitch = max(0, min(127, n.note))
        events.append((start, 0x90, pitch, vel, 1))
        events.append((end, 0x80, pitch, 0, 0))

    events.sort(key=lambda x: (x[0], x[4]))
    last = 0
    out = []
    for ppq, status, d1, d2, _ in events:
        delta = max(0, ppq - last)
        out.append(f"E {delta} {status:02x} {d1:02x} {d2:02x}")
        last = ppq

    if source_end_ppq is not None:
        source_end_ppq = max(last, int(source_end_ppq))
        out.append(f"E {source_end_ppq - last} b0 7b 00")

    return out


def extract_midi_region_windows(data: bytes, top: list) -> dict:
    """
    Decode PT10+ MIDI region source windows from 0x2634 -> 0x2633 -> 0x2628.

    The region payload contains the same PT 'three point' structure used by
    libptformat: source offset, region length, and original start.  MIDI source
    offsets are commonly stored in the absolute ZERO_TICKS domain while the
    original start is already relative musical ticks.

    Returns:
        region_index -> {
            name, source_start, length, original_start, header_offset, block
        }
    """
    all_blocks = list(_walk_blocks(top))
    regions = []
    for parent in [b for b in all_blocks if b[1] == 0x2634]:
        regions.extend([c for c in parent[4] if c[1] == 0x2633])

    result = {}

    for ri, region in enumerate(regions):
        kids = [c for c in region[4] if c[1] == 0x2628]
        if not kids:
            continue

        c = kids[0]
        lo = c[3]
        hi = min(len(data), c[3] + c[2])

        candidates = []
        scan_hi = min(hi - 10, lo + 96)

        for j in range(lo, max(lo, scan_hi)):
            if j + 10 >= len(data):
                break

            ob = (data[j + 1] & 0xF0) >> 4
            lb = (data[j + 2] & 0xF0) >> 4
            sb = (data[j + 3] & 0xF0) >> 4

            if not (1 <= ob <= 5 and 1 <= lb <= 5 and 1 <= sb <= 5):
                continue

            src_raw, length, start_raw = parse_three_point(data, j)
            if length <= 0 or length > 1_000_000_000:
                continue

            src_rel = src_raw - ZERO_TICKS if src_raw >= ZERO_TICKS else src_raw
            start_rel = start_raw - ZERO_TICKS if start_raw >= ZERO_TICKS else start_raw

            if not (0 <= src_rel <= 2_000_000_000):
                continue
            if not (0 <= start_rel <= 2_000_000_000):
                continue

            score = 0
            # PT MIDI source offsets in these sessions are five-byte values.
            if ob == 5:
                score += 5
            if src_raw >= ZERO_TICKS:
                score += 5
            # Region definitions commonly keep source and original starts close.
            delta = abs(src_rel - start_rel)
            if delta <= 32:
                score += 5
            elif delta <= max(length, PT_MIDI_TICKS_PER_QN):
                score += 3
            elif delta <= length * 4:
                score += 1

            # Header byte-count patterns around 5/3-4/4 are common in PT10+.
            if sb == 4:
                score += 2
            if 2 <= lb <= 5:
                score += 1

            candidates.append(
                (score, j, src_rel, int(length), start_rel, src_raw, ob, lb, sb)
            )

        if not candidates:
            continue

        candidates.sort(key=lambda x: (-x[0], x[1]))
        score, j, src_rel, length, start_rel, src_raw, ob, lb, sb = candidates[0]

        # Pull a readable region name from bytes preceding the three-point header.
        prefix = data[lo:j]
        printable = re.findall(rb"[\x20-\x7e]{2,}", prefix)
        name = ""
        for piece in printable:
            try:
                t = piece.decode("utf-8", "replace").strip("\x00 ").strip()
            except Exception:
                continue
            if t and any(ch.isalpha() for ch in t):
                name = t
        if not name:
            name = f"PT MIDI region {ri}"

        result[ri] = {
            "name": name,
            "source_start": int(src_rel),
            "length": int(length),
            "original_start": int(start_rel),
            "header_offset": int(j),
            "header_score": int(score),
            "byte_counts": (ob, lb, sb),
            "block": c,
        }

    return result


def build_midi_chunk_windows(data: bytes, top: list):
    """
    Pair the known-working MIDI event decoder with the raw MdNLB zero ticks.

    The older working decoder correctly yields note pitch/velocity/duration.
    _scan_mdnlb_catalogue() is used ONLY for each list's absolute zero tick;
    its older raw-event interpretation is deliberately ignored.
    """
    chunks = extract_midi_event_chunks(data, top)
    raw = _scan_mdnlb_catalogue(data, top)

    infos = []
    for i, chunk in enumerate(chunks):
        if i >= len(raw):
            break

        zero_rel = int(raw[i]["zero_rel"])
        if chunk.notes:
            rel_start = min(n.pos for n in chunk.notes)
            rel_end = max(n.pos + n.length for n in chunk.notes)
        else:
            rel_start = 0
            rel_end = 0

        infos.append({
            "index": i,
            "chunk": chunk,
            "zero_rel": zero_rel,
            "abs_start": zero_rel + rel_start,
            "abs_end": zero_rel + rel_end,
            "span": max(0, rel_end - rel_start),
            "count": len(chunk.notes),
        })
    return infos


def _midi_window_score(region_meta, chunk_info):
    """
    Score how well an MdNLB's actual musical extent fits a PT MIDI region's
    source window.  1.0 is approximately an exact source-window match.
    """
    rs = region_meta["source_start"]
    re_ = rs + region_meta["length"]
    cs = chunk_info["abs_start"]
    ce = chunk_info["abs_end"]

    rlen = max(1, re_ - rs)
    clen = max(1, ce - cs)

    overlap = max(0, min(re_, ce) - max(rs, cs))
    if overlap <= 0:
        return 0.0, {
            "overlap": 0,
            "region_cov": 0.0,
            "chunk_cov": 0.0,
            "length_sim": 0.0,
            "start_delta": abs(cs-rs),
            "end_delta": abs(ce-re_),
        }

    region_cov = overlap / rlen
    chunk_cov = overlap / clen
    length_sim = min(rlen, clen) / max(rlen, clen)
    start_delta = abs(cs - rs)
    end_delta = abs(ce - re_)
    scale = max(rlen, clen, PT_MIDI_TICKS_PER_QN)
    start_close = max(0.0, 1.0 - (start_delta / scale))

    score = (
        0.35 * region_cov +
        0.35 * chunk_cov +
        0.20 * length_sim +
        0.10 * start_close
    )

    return score, {
        "overlap": overlap,
        "region_cov": region_cov,
        "chunk_cov": chunk_cov,
        "length_sim": length_sim,
        "start_delta": start_delta,
        "end_delta": end_delta,
    }


def _slice_chunk_to_region(chunk_info, region_meta):
    """
    Crop the underlying MdNLB events to the PT region's source window and
    shift them so the REAPER MIDI item begins at tick zero.
    """
    rs = region_meta["source_start"]
    re_ = rs + region_meta["length"]
    zero = chunk_info["zero_rel"]

    out = []
    for n in chunk_info["chunk"].notes:
        ns = zero + n.pos
        ne = ns + n.length

        if ne <= rs or ns >= re_:
            continue

        clipped_start = max(ns, rs)
        clipped_end = min(ne, re_)
        if clipped_end <= clipped_start:
            continue

        out.append(
            MidiNote(
                clipped_start - rs,
                n.note,
                clipped_end - clipped_start,
                n.velocity,
            )
        )
    return out


def _collect_direct_midi_region_blocks(top):
    """
    Return direct PT MIDI region table as:
      [(region_index, region_0x2633, child_0x2628), ...]
    """
    all_blocks = list(_walk_blocks(top))
    out = []
    for parent in [b for b in all_blocks if b[1] == 0x2634]:
        direct = [c for c in parent[4] if c[1] == 0x2633]
        for rb in direct:
            child = next((c for c in rb[4] if c[1] == 0x2628), None)
            out.append((len(out), rb, child))
    return out


def extract_midi_region_mdnlb_links(data: bytes, top: list) -> dict:
    """
    Direct PT MIDI region -> MdNLB linkage.

    Proven from Silent Longing training mappings in v34:
      r19/r20/r21 -> 6
      r22         -> 13
      r23         -> 2
      r24         -> 14

    In every 0x2633 MIDI-region wrapper, the first 4 bytes immediately AFTER
    the direct 0x2628 child contain the MdNLB list index as little-endian u32.

    Example wrapper-only tails observed in v34:
      r019 ... 06 00 00 00  -> MdNLB[06]
      r022 ... 0d 00 00 00  -> MdNLB[13]
      r023 ... 02 00 00 00  -> MdNLB[02]
      r024 ... 0e 00 00 00  -> MdNLB[14]

    Returns:
        region_index -> mdnlb_index
    """
    links = {}
    regions = _collect_direct_midi_region_blocks(top)

    for ri, rb, child in regions:
        if child is None:
            continue

        rb_end = min(len(data), rb[3] + rb[2])
        child_end = child[3] + child[2]

        if child_end + 4 > rb_end or child_end + 4 > len(data):
            continue

        mdnlb_index = r4(data, child_end)
        links[ri] = int(mdnlb_index)

    return links


def _walk_blocks(blocks):
    for b in blocks:
        yield b
        yield from _walk_blocks(b[4])


def _safe_pt_string(data: bytes, pos: int):
    try:
        s, end = read_pt_string(data, pos)
        return s, end
    except Exception:
        return "", pos


def _scan_mdnlb_catalogue(data: bytes, top: list):
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
        bstart = block[3]
        bend = min(len(data), bstart + block[2])
        pos = bstart
        while True:
            p = data.find(b"MdNLB", pos, bend)
            if p < 0:
                break
            pos = p + 5
            if p in seen_offsets:
                continue
            seen_offsets.add(p)

            q = p + 11
            if q + 9 > len(data):
                continue

            count = r4(data, q)
            zero = r5(data, q + 4)

            # Validate enough bytes exist for the advertised event list.
            ev0 = q + 9
            need = ev0 + count * 35
            if count > 1_000_000 or need > len(data):
                continue

            notes = []
            for n in range(count):
                ep = ev0 + n * 35
                raw_pos = r5(data, ep)
                pitch = data[ep + 8]
                length = r5(data, ep + 9)
                velocity = data[ep + 17]
                rel_pos = raw_pos - zero if raw_pos >= zero else raw_pos
                notes.append((rel_pos, pitch, length, velocity, raw_pos))

            found.append({
                "index": len(found),
                "offset": p,
                "count": count,
                "zero": zero,
                "zero_rel": zero - ZERO_TICKS if zero >= ZERO_TICKS else zero,
                "notes": notes,
            })
    return found


def resolve_midi_placements_direct(data: bytes, top: list):
    """
    Resolve active PT MIDI placements through the proven structural linkage:

        0x2633 MIDI region
          ├─ 0x2628 : region name + source/edit window
          └─ trailing little-endian u32 : exact MdNLB index

    The linked MdNLB list is then cropped to the region source window.
    """
    placements, duplicate_refs_skipped = extract_midi_placements(data, top)
    region_meta = extract_midi_region_windows(data, top)
    chunk_infos = build_midi_chunk_windows(data, top)
    direct_links = extract_midi_region_mdnlb_links(data, top)

    chunks_by_index = {ci["index"]: ci for ci in chunk_infos}
    resolved = {}
    unresolved = []

    for track_name, track_placements in placements.items():
        rows = []

        for placement in track_placements:
            region_index = placement.region_index
            meta = region_meta.get(region_index)
            mdnlb_index = direct_links.get(region_index)
            chunk_info = (
                chunks_by_index.get(mdnlb_index)
                if mdnlb_index is not None
                else None
            )

            if meta is None:
                unresolved.append(
                    (track_name, region_index, placement.timeline_ticks,
                     "no decoded PT MIDI region window")
                )
                continue

            if mdnlb_index is None:
                unresolved.append(
                    (track_name, region_index, placement.timeline_ticks,
                     "no trailing MdNLB index in 0x2633")
                )
                continue

            if chunk_info is None:
                unresolved.append(
                    (track_name, region_index, placement.timeline_ticks,
                     f"MdNLB index {mdnlb_index} out of range")
                )
                continue

            notes = _slice_chunk_to_region(chunk_info, meta)
            if not notes:
                unresolved.append(
                    (
                        track_name,
                        region_index,
                        placement.timeline_ticks,
                        f"MdNLB[{mdnlb_index}] has no notes in region source window",
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

    return placements, resolved, unresolved, duplicate_refs_skipped




def _media_frame_count(path: str) -> Optional[int]:
    """Return PCM frame count for WAV media when available."""
    try:
        with wave.open(path, "rb") as wf:
            return wf.getnframes()
    except Exception:
        return None


def heal_short_audio_item_lengths(
    audio_tracks: Dict[str, List[ClipPlacement]],
    sample_rate: int,
    max_gap_ms: float = 250.0,
) -> Tuple[Dict[int, int], int]:
    """
    Compute conservative timeline lengths for audio placements.

    Some PTX sessions contain region definitions whose stored source length
    ends slightly before the following playlist edit. REAPER then shows a gap.

    Extend only when:
      - a following item exists on the same output track;
      - the current item ends before that following item;
      - the gap is <= max_gap_ms;
      - the underlying WAV has enough source media to cover the extension.
    """
    max_gap_samples = max(0, int(round(max_gap_ms * sample_rate / 1000.0)))
    effective: Dict[int, int] = {}
    media_frames: Dict[str, Optional[int]] = {}
    healed = 0

    for track_name, placements in audio_tracks.items():
        ordered = sorted(placements, key=lambda p: p.timeline_start)

        for i, placement in enumerate(ordered):
            region = placement.region
            base_len = max(0, int(region.length))
            effective[id(placement)] = base_len

            if i + 1 >= len(ordered) or max_gap_samples <= 0:
                continue

            next_placement = ordered[i + 1]
            next_start = int(next_placement.timeline_start)
            current_end = int(placement.timeline_start) + base_len

            if next_start <= current_end:
                continue

            gap = next_start - current_end
            if gap > max_gap_samples:
                continue

            wav_path = region.wav_file
            if not wav_path:
                continue

            if wav_path not in media_frames:
                media_frames[wav_path] = _media_frame_count(wav_path)

            total_frames = media_frames[wav_path]
            if total_frames is None:
                continue

            target_len = next_start - int(placement.timeline_start)
            source_end = int(region.src_offset) + target_len

            if source_end > total_frames:
                continue

            effective[id(placement)] = target_len
            healed += 1

            debug(
                f"  [audio] healed short item on {track_name!r}: "
                f"{region.name!r} +{gap} samples "
                f"({gap / sample_rate * 1000.0:.2f} ms)"
            )

    return effective, healed



def _split_output_track_name(name: str) -> Tuple[str, str]:
    """
    'Vocals Chorus.03 [L]' -> ('Vocals Chorus.03', ' [L]')
    """
    m = re.match(r"^(.*?)(\s+\[(?:L|R|ch\d+)\])$", name)
    if m:
        return m.group(1), m.group(2)
    return name, ""


def _playlist_candidate_roots(stem: str) -> List[str]:
    """
    Return likely parent track names for playlist-looking names.

    Examples:
      Vocals Chorus.03       -> ['Vocals Chorus']
      Vocals Chorus.dup1.03  -> ['Vocals Chorus.dup1', 'Vocals Chorus']

    We only use these as candidates; grouping additionally requires the parent
    track to exist and a family to contain multiple alternate playlists.
    """
    roots = []

    m = re.match(r"^(.*)\.(\d{2,3})$", stem)
    if not m:
        return roots

    pre = m.group(1)
    roots.append(pre)

    mdup = re.match(r"^(.*)\.dup\d+$", pre, re.IGNORECASE)
    if mdup:
        roots.append(mdup.group(1))

    # Prefer the immediate parent first (e.g. Track.dup1), then the
    # root track (Track) only when that immediate parent is absent.
    out = []
    for x in roots:
        if x not in out:
            out.append(x)
    return out


def build_playlist_lane_groups(
    audio_tracks: Dict[str, List[ClipPlacement]],
) -> Tuple[List[PlaylistLaneGroup], set]:
    """
    Conservatively identify Pro Tools playlist families from mapped audio tracks.

    A family is created only when:
      - an unsuffixed parent output track exists; and
      - at least TWO numbered sibling playlists point back to that parent.

    This avoids turning every '.01' style track name into a lane accidentally.

    The active/unsuffixed playlist is lane 0.
    """
    keys = list(audio_tracks.keys())
    keyset = set(keys)

    # Gather candidate alternates by actual existing parent output track.
    by_parent: Dict[str, List[str]] = {}

    for key in keys:
        stem, channel_suffix = _split_output_track_name(key)

        for root in _playlist_candidate_roots(stem):
            parent = root + channel_suffix
            if parent in keyset and parent != key:
                by_parent.setdefault(parent, []).append(key)
                break

    groups = []
    consumed = set()

    for parent, alternates in by_parent.items():
        # Require at least 2 alternate playlists for safety.
        alternates = sorted(
            set(alternates),
            key=lambda n: (
                _playlist_sort_key(_split_output_track_name(n)[0]),
                n.lower(),
            ),
        )
        if len(alternates) < 2:
            continue

        group = PlaylistLaneGroup(parent)
        parent_stem, _ = _split_output_track_name(parent)

        # lane 0 = active PT playlist
        group.lanes.append(
            (parent_stem, audio_tracks.get(parent, []), True)
        )

        for alt in alternates:
            alt_stem, _ = _split_output_track_name(alt)
            group.lanes.append(
                (alt_stem, audio_tracks.get(alt, []), False)
            )

        groups.append(group)
        consumed.add(parent)
        consumed.update(alternates)

    return groups, consumed


def _playlist_sort_key(name: str):
    """
    Natural-ish playlist sorting:
      Track.01 ... Track.10, Track.dup1.01 ... etc.
    """
    parts = re.split(r"(\d+)", name.lower())
    return tuple(int(p) if p.isdigit() else p for p in parts)


def _quote_rpp_string(value: str) -> str:
    return '"' + value.replace('"', "'") + '"'



def samples_to_seconds(n: int, rate: int = DEFAULT_SAMPLE_RATE) -> float:
    return n / rate


def write_rpp(
    out_path: Path,
    audio_tracks: Dict[str, List[ClipPlacement]],
    session_start_samples: int,
    sample_rate: int,
    midi_tracks: Dict[str, List[MidiPlacement]],
    resolved_midi,
    tempo_bpm: float,
    memory_locations: Optional[List[dict]] = None,
    max_gap_heal_ms: float = 250.0,
    playlists_to_lanes: bool = False,
) -> Tuple[int, int, int, int]:
    """
    Write the REAPER project.

    Audio and MIDI are both shifted by the SAME project origin.  This fixes the
    earlier prototype behaviour where audio was origin-shifted but MIDI remained
    in absolute session time.
    """
    lines: List[str] = []

    def L(text: str = "", indent: int = 0) -> None:
        lines.append("  " * indent + text)

    def clean_name(value: str) -> str:
        return value.replace('"', "'").strip()

    L(f'<REAPER_PROJECT 0.1 "7.0/win64" {int(time.time())}>')
    L(f"TEMPO {tempo_bpm:.10f} 4 4")
    L(f"SAMPLERATE {sample_rate} 0 0")
    L("LOOP 0")

    colour_idx = 0
    item_counter = 0
    audio_written = 0
    effective_audio_lengths, healed_audio_items = heal_short_audio_item_lengths(
        audio_tracks,
        sample_rate,
        max_gap_heal_ms,
    )

    # Project origin in seconds, shared by audio, MIDI and markers.
    origin_seconds = samples_to_seconds(session_start_samples, sample_rate)

    # ---------------------------------------------------------------
    # Pro Tools Memory Locations -> REAPER project markers
    # ---------------------------------------------------------------
    used_marker_ids = set()
    next_marker_id = 1

    for marker in memory_locations or []:
        requested_id = int(marker.get("index", 0) or 0)

        if requested_id > 0 and requested_id not in used_marker_ids:
            marker_id = requested_id
        else:
            while next_marker_id in used_marker_ids:
                next_marker_id += 1
            marker_id = next_marker_id

        used_marker_ids.add(marker_id)
        next_marker_id = max(next_marker_id, marker_id + 1)

        marker_pos = (
            float(marker["position_seconds"]) - origin_seconds
        )
        marker_name = clean_name(str(marker.get("name", "")))
        marker_key = (
            f"PT_MARKER|{marker_id}|{marker_name}|"
            f"{marker_pos:.12f}"
        )

        L(
            f'MARKER {marker_id} {marker_pos:.12f} '
            f'"{marker_name}" 0 0 1 R '
            f'{stable_guid(marker_key)} 0'
        )

    playlist_groups = []
    playlist_consumed = set()
    if playlists_to_lanes:
        playlist_groups, playlist_consumed = build_playlist_lane_groups(
            audio_tracks
        )

        print()
        print("Detected Pro Tools playlist families:")
        if not playlist_groups:
            print("  none")
        else:
            for group in playlist_groups:
                print(
                    f"  {group.track_name!r}: "
                    f"{len(group.lanes)} lane(s)"
                )
                for lane_index, (lane_name, placements, is_active) in enumerate(
                    group.lanes,
                    start=1,
                ):
                    state = "ACTIVE" if is_active else "alternate"
                    print(
                        f"    Lane {lane_index:02d}: "
                        f"{lane_name!r}  "
                        f"items={len(placements)}  {state}"
                    )

    # ---------------------------------------------------------------
    # Fixed-lane playlist tracks
    # ---------------------------------------------------------------
    playlist_track_count = 0

    for group in playlist_groups:
        all_writable = [
            p
            for _, placements, _ in group.lanes
            for p in placements
            if p.region.wav_file
        ]
        if not all_writable:
            continue

        playlist_track_count += 1
        colour = TRACK_COLOURS[colour_idx % len(TRACK_COLOURS)]
        colour_idx += 1

        lane_count = max(1, len(group.lanes))
        lane_h = 1.0 / lane_count

        L("<TRACK")
        L(f'NAME "{clean_name(group.track_name)}"', 1)
        L(f"PEAKCOL {colour | 0x01000000}", 1)
        L("BEAT -1", 1)
        L("VOLPAN 1 0 -1 -1 1", 1)
        L("MUTESOLO 0 0 0", 1)
        L("IPHASE 0", 1)
        L("ISBUS 0 0", 1)
        L("BUSCOMP 0 0 0 0 0", 1)
        L("SHOWINMIX 1 0.6667 0.5 1 0.5 0 0 0", 1)
        L("SEL 0", 1)
        L("REC 0 0 1 0 0 0 0 0", 1)
        L("VU 2", 1)
        L("TRACKHEIGHT 0 0 0 0 0 0", 1)
        L("INQ 0 0 0 0.5 100 0 0 100", 1)
        L("NCHAN 1", 1)
        L("FX 1", 1)

        # REAPER 7 fixed item lanes.
        # Lane 1 / index 0 is the active Pro Tools playlist.
        L("FREEMODE 2", 1)
        L("FIXEDLANES 8 0 0 0 0", 1)
        L("LANESOLO 1 0 0 0 0 0 0 0", 1)

        # REAPER stores lane names as one list.  The first two integers are
        # retained as conservative defaults used by current REAPER state chunks.
        lane_names = " ".join(
            _quote_rpp_string(name)
            for name, _, _ in group.lanes
        )
        L(f"LANENAME 1 2 {lane_names}", 1)

        L(f"TRACKID {stable_guid('TRACK|PLAYLISTS|' + group.track_name)}", 1)

        for lane_index, (lane_name, placements, is_active) in enumerate(group.lanes):
            lane_y = lane_index * lane_h

            for placement in placements:
                region = placement.region
                if not region.wav_file:
                    continue

                item_counter += 1
                audio_written += 1

                position_seconds = samples_to_seconds(
                    max(0, placement.timeline_start - session_start_samples),
                    sample_rate,
                )
                effective_length = effective_audio_lengths.get(
                    id(placement),
                    region.length,
                )
                length_seconds = samples_to_seconds(
                    effective_length,
                    sample_rate,
                )
                source_offset_seconds = samples_to_seconds(
                    region.src_offset,
                    sample_rate,
                )
                relative_path = os.path.relpath(
                    region.wav_file,
                    str(out_path.parent),
                ).replace("\\", "/")

                item_key = (
                    f"AUDIO|LANE|{group.track_name}|{lane_index}|"
                    f"{region.index}|{placement.timeline_start}|{region.wav_file}"
                )

                L("<ITEM", 1)
                L(f"POSITION {position_seconds:.10f}", 2)
                L("SNAPOFFS 0", 2)
                L(f"LENGTH {length_seconds:.10f}", 2)
                L("LOOP 0", 2)
                L("ALLTAKES 0", 2)

                # Fixed lane location.
                L(f"YPOS {lane_y:.10f} {lane_h:.10f} 2", 2)

                L("FADEIN 0 0 0 0 0 0 0", 2)
                L("FADEOUT 0 0 0 0 0 0 0", 2)
                L("MUTE 0 0", 2)
                L("SEL 0", 2)
                L(f"IGUID {stable_guid(item_key)}", 2)
                L(f"IID {item_counter}", 2)
                L(f'NAME "{clean_name(region.name)}"', 2)
                L("VOLPAN 1 0 1 -1", 2)
                L(f"SOFFS {source_offset_seconds:.10f}", 2)
                L("PLAYRATE 1 1 0 -1 0 0.0025", 2)
                L("CHANMODE 0", 2)
                L(f"GUID {stable_guid(item_key + '|TAKE')}", 2)
                L("<SOURCE WAVE", 2)
                L(f'FILE "{relative_path}"', 3)
                L(">", 2)
                L(">", 1)

        L(">")

    # ---------------------------------------------------------------
    # Ordinary audio tracks (unchanged)
    # ---------------------------------------------------------------
    for track_name, placements in audio_tracks.items():
        if track_name in playlist_consumed:
            continue

        writable = [p for p in placements if p.region.wav_file]
        if not writable:
            continue

        colour = TRACK_COLOURS[colour_idx % len(TRACK_COLOURS)]
        colour_idx += 1

        L("<TRACK")
        L(f'NAME "{clean_name(track_name)}"', 1)
        L(f"PEAKCOL {colour | 0x01000000}", 1)
        L("BEAT -1", 1)
        L("VOLPAN 1 0 -1 -1 1", 1)
        L("MUTESOLO 0 0 0", 1)
        L("IPHASE 0", 1)
        L("ISBUS 0 0", 1)
        L("BUSCOMP 0 0 0 0 0", 1)
        L("SHOWINMIX 1 0.6667 0.5 1 0.5 0 0 0", 1)
        L("SEL 0", 1)
        L("REC 0 0 1 0 0 0 0 0", 1)
        L("VU 2", 1)
        L("TRACKHEIGHT 0 0 0 0 0 0", 1)
        L("INQ 0 0 0 0.5 100 0 0 100", 1)
        L("NCHAN 1", 1)
        L("FX 1", 1)
        L(f"TRACKID {stable_guid('TRACK|AUDIO|' + track_name)}", 1)

        for placement in writable:
            region = placement.region
            item_counter += 1
            audio_written += 1

            position_seconds = samples_to_seconds(
                max(0, placement.timeline_start - session_start_samples),
                sample_rate,
            )
            effective_length = effective_audio_lengths.get(
                id(placement),
                region.length,
            )
            length_seconds = samples_to_seconds(effective_length, sample_rate)
            source_offset_seconds = samples_to_seconds(
                region.src_offset, sample_rate
            )
            relative_path = os.path.relpath(
                region.wav_file, str(out_path.parent)
            ).replace("\\", "/")

            item_key = (
                f"AUDIO|{track_name}|{region.index}|"
                f"{placement.timeline_start}|{region.wav_file}"
            )

            L("<ITEM", 1)
            L(f"POSITION {position_seconds:.10f}", 2)
            L("SNAPOFFS 0", 2)
            L(f"LENGTH {length_seconds:.10f}", 2)
            L("LOOP 0", 2)
            L("ALLTAKES 0", 2)
            L("FADEIN 0 0 0 0 0 0 0", 2)
            L("FADEOUT 0 0 0 0 0 0 0", 2)
            L("MUTE 0 0", 2)
            L("SEL 0", 2)
            L(f"IGUID {stable_guid(item_key)}", 2)
            L(f"IID {item_counter}", 2)
            L(f'NAME "{clean_name(region.name)}"', 2)
            L("VOLPAN 1 0 1 -1", 2)
            L(f"SOFFS {source_offset_seconds:.10f}", 2)
            L("PLAYRATE 1 1 0 -1 0 0.0025", 2)
            L("CHANMODE 0", 2)
            L(f"GUID {stable_guid(item_key + '|TAKE')}", 2)
            L("<SOURCE WAVE", 2)
            L(f'FILE "{relative_path}"', 3)
            L(">", 2)
            L(">", 1)

        L(">")

    midi_written = 0

    for track_name, placements in midi_tracks.items():
        if not placements:
            continue

        colour = TRACK_COLOURS[colour_idx % len(TRACK_COLOURS)]
        colour_idx += 1

        L("<TRACK")
        L(f'NAME "{clean_name(track_name)}"', 1)
        L(f"PEAKCOL {colour | 0x01000000}", 1)
        L("BEAT 1", 1)
        L("VOLPAN 1 0 -1 -1 1", 1)
        L("MUTESOLO 0 0", 1)
        L("NCHAN 2", 1)
        L(f"TRACKID {stable_guid('TRACK|MIDI|' + track_name)}", 1)

        for placement, chunk, meta, sliced_notes in resolved_midi.get(
            track_name, []
        ):
            if not sliced_notes:
                continue

            item_counter += 1
            midi_written += 1

            absolute_seconds = ptticks_to_seconds(
                placement.timeline_ticks, tempo_bpm
            )
            position_seconds = max(0.0, absolute_seconds - origin_seconds)

            length_ticks = max(
                int(meta["length"]),
                PT_MIDI_TICKS_PER_QN // 64,
            )
            length_seconds = ptticks_to_seconds(length_ticks, tempo_bpm)

            item_key = (
                f"MIDI|{track_name}|{placement.region_index}|"
                f"{placement.timeline_ticks}|{chunk.index}|{item_counter}"
            )

            L("<ITEM", 1)
            L(f"POSITION {position_seconds:.10f}", 2)
            L("SNAPOFFS 0", 2)
            L(f"LENGTH {length_seconds:.10f}", 2)
            L("LOOP 0", 2)
            L("ALLTAKES 0", 2)
            # Match REAPER's native MIDI item/take wrapper.  Earlier PTX2RPP
            # builds omitted the take CHANMODE/GUID fields, which left REAPER
            # to synthesize an incomplete take wrapper when loading the RPP.
            # Keep the MIDI source itself unpooled: POOLEDEVTS is deliberately
            # absent so separate PT regions cannot become pooled copies.
            L("FADEIN 1 0 0 1 0 0 0", 2)
            L("FADEOUT 1 0 0 1 0 0 0", 2)
            L("MUTE 0 0", 2)
            L("SEL 0", 2)
            L(f"IGUID {stable_guid(item_key)}", 2)
            L(f"IID {item_counter}", 2)
            L(f'NAME "{clean_name(meta.get("name", track_name))}"', 2)
            L("VOLPAN 1 0 1 -1", 2)
            L("SOFFS 0 0", 2)
            L("PLAYRATE 1 1 0 -1 0 0.0025", 2)
            L("CHANMODE 0", 2)
            L(f"GUID {stable_guid(item_key + '|TAKE')}", 2)
            L("<SOURCE MIDI", 2)
            L("HASDATA 1 960 QN", 3)
            L("CCINTERP 32", 3)

            source_end_ppq = max(
                1,
                int(round(length_ticks * RPP_PPQ / PT_MIDI_TICKS_PER_QN)),
            )
            for event_line in _midi_source_events(
                sliced_notes,
                source_end_ppq=source_end_ppq,
            ):
                L(event_line, 3)

            # Native REAPER MIDI-source footer.  Keep POOLEDEVTS absent:
            # PTX2RPP regions must remain independent rather than pooled copies.
            L("CCINTERP 32", 3)
            L("CHASE_CC_TAKEOFFS 1", 3)
            L(f"GUID {stable_guid(item_key + '|MIDI_SOURCE')}", 3)
            L(f"IGNTEMPO 0 {tempo_bpm:.10g} 4 4", 3)
            L(
                "EVTFILTER 0 -1 -1 -1 -1 0 0 0 0 "
                "-1 -1 -1 -1 0 -1 0 -1 -1",
                3,
            )
            L(">", 2)
            L(">", 1)

        L(">")

    L(">")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return audio_written, midi_written, healed_audio_items, playlist_track_count



def detect_session_sample_rate(data: bytes, top: list) -> int:
    sample_rate = DEFAULT_SAMPLE_RATE
    for block in find_by_ct(top, 0x1028):
        if block[3] + 8 <= len(data):
            candidate = r4(data, block[3] + 4)
            if 8000 <= candidate <= 768000:
                sample_rate = candidate
    return sample_rate


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ptx_to_reaper.py",
        description="Convert a Pro Tools .ptx session to a REAPER .rpp project.",
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=".",
        help="PTX file or session directory",
    )
    parser.add_argument(
        "ptx_name",
        nargs="?",
        help="Optional PTX filename when input is a session directory",
    )
    parser.add_argument(
        "--output",
        help="Override output .rpp path",
    )
    parser.add_argument(
        "--audio-dir",
        help="Override the session Audio Files directory",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed parser diagnostics",
    )
    parser.add_argument(
        "--playlists-to-lanes",
        action="store_true",
        help=(
            "Experimental: convert detected Pro Tools audio playlists into "
            "REAPER 7 fixed item lanes"
        ),
    )
    parser.add_argument(
        "--max-gap-heal-ms",
        type=float,
        default=250.0,
        help=(
            "Extend short audio items to the next edit when the gap is no more "
            "than this many milliseconds and source media is available "
            "(default: 250; use 0 to disable)"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return a failure exit code if active media/MIDI cannot be written",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    global VERBOSE

    args = build_arg_parser().parse_args(argv)
    VERBOSE = args.verbose

    try:
        ptx_file, audio_dir, output_rpp = resolve_session_input(
            args.input,
            args.ptx_name,
            args.audio_dir,
            args.output,
        )
    except Exception as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return 2

    print(f"{APP_NAME} {APP_VERSION}")
    print(f"Input : {ptx_file}")
    print(f"Output: {output_rpp}")

    try:
        raw = ptx_file.read_bytes()
        if len(raw) < 0x14:
            raise ValueError("PTX file is too small to contain a valid header")

        data = decrypt_ptx(raw)
        top = find_top(data)
        sample_rate = detect_session_sample_rate(data, top)

        # ── Audio ──────────────────────────────────────────────────────────
        registered_audio = extract_audio_files(data, top)
        audio_track_defs = extract_audio_tracks(data, top)
        regions = extract_regions(
            data,
            top,
            verbose=VERBOSE,
        )
        if audio_dir.is_dir():
            media_index = build_wav_index(audio_dir)
        else:
            media_index = {}
            if registered_audio:
                print(
                    f"Warning: Audio Files directory not found: {audio_dir}",
                    file=sys.stderr,
                )

        registered_media = build_registered_media_map(
        registered_audio,
        media_index,
        verbose=VERBOSE,
        )
        match_regions_to_wavs(
            regions,
            registered_media,
            verbose=VERBOSE,
        )
        session_tc_origin_samples, session_tc_rate_enum, session_tc_origin_frames = (
            extract_session_timecode_origin_samples(data, top, sample_rate)
        )

        (
            audio_track_map,
            inactive_audio_skipped,
            timestamp_audio_corrected,
        ) = assign_regions_to_tracks(
            data,
            top,
            audio_track_defs,
            regions,
            sample_rate,
            session_tc_origin_samples,
        )
        duplicate_audio_placements = dedupe_redundant_same_track_audio(
            audio_track_map
        )
        pruned_audio_aliases = prune_spurious_cross_track_audio(audio_track_map)

        audio_placement_count = sum(
            len(items) for items in audio_track_map.values()
        )
        matched_region_count = sum(1 for region in regions if region.wav_file)

        placement_starts = [
            placement.timeline_start
            for placements in audio_track_map.values()
            for placement in placements
        ]

        # ── MIDI ───────────────────────────────────────────────────────────
        (
            midi_tracks,
            resolved_midi,
            unresolved_midi,
            duplicate_midi_refs_skipped,
        ) = resolve_midi_placements_direct(data, top)

        # If a session contains no active audio placement, use the earliest MIDI
        # placement as the project origin instead.
        if placement_starts:
            session_start_samples = min(placement_starts)
        else:
            midi_starts = [
                placement.timeline_ticks
                for placements in midi_tracks.values()
                for placement in placements
            ]
            if midi_starts:
                tempo_for_origin = extract_session_tempo(data, top)
                origin_seconds = ptticks_to_seconds(
                    min(midi_starts),
                    tempo_for_origin,
                )
                session_start_samples = int(
                    round(origin_seconds * sample_rate)
                )
            else:
                session_start_samples = 0

        tempo_bpm = extract_session_tempo(data, top)

        # ── Memory Locations / markers ─────────────────────────────────────
        memory_locations = extract_memory_locations(
            data,
            sample_rate,
            tempo_bpm,
            verbose=VERBOSE,
            )

        # ── Write ──────────────────────────────────────────────────────────
        audio_written, midi_written, healed_audio_items, playlist_track_count = write_rpp(
            output_rpp,
            audio_track_map,
            session_start_samples,
            sample_rate,
            midi_tracks,
            resolved_midi,
            tempo_bpm,
            memory_locations=memory_locations,
            max_gap_heal_ms=max(0.0, args.max_gap_heal_ms),
            playlists_to_lanes=args.playlists_to_lanes,
        )

        total_midi_placements = sum(
            len(items) for items in midi_tracks.values()
        )

        print()
        print("Conversion complete")
        print(f"  Sample rate          : {sample_rate} Hz")
        print(f"  Tempo                : {tempo_bpm:.6f} BPM")
        print(
            f"  Audio media matched  : "
            f"{matched_region_count}/{len(regions)} region definitions"
        )
        print(
            f"  Audio items written  : "
            f"{audio_written}/{audio_placement_count} active placements"
        )
        print(
            f"  Inactive audio skipped: {inactive_audio_skipped}"
        )
        print(
            f"  Timestamp audio fixed: {timestamp_audio_corrected}"
        )
        print(
            f"  Audio duplicates     : {duplicate_audio_placements} skipped"
        )
        print(
            f"  Audio aliases pruned : {pruned_audio_aliases}"
        )
        print(
            f"  Short audio healed   : {healed_audio_items} "
            f"(max {max(0.0, args.max_gap_heal_ms):.1f} ms)"
        )
        print(
            f"  Playlist lane tracks : {playlist_track_count} "
            f"({'enabled' if args.playlists_to_lanes else 'disabled'})"
        )
        print(
            f"  MIDI items written   : "
            f"{midi_written}/{total_midi_placements} active placements"
        )
        print(f"  MIDI unresolved      : {len(unresolved_midi)}")
        print(f"  MIDI duplicate refs  : {duplicate_midi_refs_skipped} skipped")
        print(f"  Memory Locations     : {len(memory_locations)} imported")
        if session_tc_rate_enum is not None:
            if session_tc_origin_samples is not None:
                print(
                    f"  Session TC origin    : "
                    f"{session_tc_origin_samples / sample_rate:.6f}s "
                    f"(enum 0x{session_tc_rate_enum:02X}, "
                    f"frames={session_tc_origin_frames})"
                )
            else:
                print(
                    f"  Session TC origin    : unverified "
                    f"(enum 0x{session_tc_rate_enum:02X}, "
                    f"frames={session_tc_origin_frames})"
                )
        print(f"  Project origin       : {session_start_samples / sample_rate:.6f}s")
        print(f"  RPP                   : {output_rpp}")

        if unresolved_midi and VERBOSE:
            print("\nUnresolved MIDI:")
            for track, region, ticks, reason in unresolved_midi:
                print(
                    f"  {track!r} r{region} at {ticks} ticks: {reason}"
                )

        missing_active_audio = audio_placement_count - audio_written
        failed = bool(unresolved_midi or missing_active_audio)

        if args.strict and failed:
            return 1

        return 0

    except Exception as exc:
        if VERBOSE:
            import traceback
            traceback.print_exc()
        else:
            print(f"{APP_NAME}: conversion failed: {exc}", file=sys.stderr)
            print("Run again with --verbose for a traceback.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
