"""REAPER project serialization helpers."""

import hashlib
import os
import time

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .audio import (
    build_playlist_lane_groups,
    heal_short_audio_item_lengths,
)
from .models import (
    ClipPlacement,
    MidiNote,
    MidiPlacement,
)
from .timing import (
    PT_MIDI_TICKS_PER_QN,
    RPP_PPQ,
    ptticks_to_rpp_ppq,
    ptticks_to_seconds,
)

TRACK_COLOURS = [
    0x0094FF,
    0xFF6B35,
    0x00C853,
    0xFF1744,
    0xAA00FF,
    0x00BCD4,
    0xFFAB40,
    0x76FF03,
    0xF50057,
    0x448AFF,
]

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

def samples_to_seconds(
    samples: int,
    sample_rate: int,
) -> float:
    """Convert an audio sample position or length to seconds."""
    return samples / sample_rate