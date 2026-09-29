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
import os
import re
import struct
import sys
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
    assign_regions_to_tracks,
    build_registered_media_map,
    build_wav_index,
    dedupe_redundant_same_track_audio,
    extract_audio_files,
    extract_audio_tracks,
    extract_regions,
    match_regions_to_wavs,
    prune_spurious_cross_track_audio,
    read_pt_string,
    heal_short_audio_item_lengths,
    build_playlist_lane_groups,
)

from .midi import (
    build_midi_chunk_windows,
    extract_midi_event_chunks,
    extract_midi_placements,
    extract_midi_region_mdnlb_links,
    extract_midi_region_windows,
    resolve_midi_placements_direct,
)

from .reaper import (
    _midi_source_events,
    _quote_rpp_string,
    memory_location_marker_lines,
    project_header_lines,
    samples_to_seconds,
    stable_guid,
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





def _walk_blocks(blocks):
    for b in blocks:
        yield b
        yield from _walk_blocks(b[4])



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

    for line in project_header_lines(
        tempo_bpm,
        sample_rate,
    ):
        L(line)

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
    for line in memory_location_marker_lines(
        memory_locations,
        origin_seconds,
    ):
        L(line)

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
            verbose=VERBOSE,
        )
        duplicate_audio_placements = dedupe_redundant_same_track_audio(
            audio_track_map,
            verbose=VERBOSE,
        )
        pruned_audio_aliases = prune_spurious_cross_track_audio(
            audio_track_map,
            verbose=VERBOSE,
        )

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
