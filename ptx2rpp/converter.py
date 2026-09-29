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
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from .ptx import (
    decrypt_ptx,
    find_top,
)
from .markers import extract_memory_locations

from .timing import (
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
)

from .midi import (
    resolve_midi_placements_direct,
)

from .reaper import write_rpp

from .session import (
    detect_session_sample_rate,
    extract_session_tempo,
    extract_session_timecode_origin_samples,
)

APP_NAME = "PTX2RPP"
APP_VERSION = "1.2.0-memory-locations"

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
            extract_session_timecode_origin_samples(
                data,
                top,
                sample_rate,
                debug_fn=debug,
            )
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
                tempo_for_origin = extract_session_tempo(
                data,
                top,
                debug_fn=debug,
                )
                origin_seconds = ptticks_to_seconds(
                    min(midi_starts),
                    tempo_for_origin,
                )
                session_start_samples = int(
                    round(origin_seconds * sample_rate)
                )
            else:
                session_start_samples = 0

        tempo_bpm = extract_session_tempo(
            data,
            top,
            debug_fn=debug,
            )

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
