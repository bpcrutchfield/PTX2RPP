"""Pro Tools audio parsing and media handling."""

import re
import wave

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from .models import AudioTrack, ClipPlacement, Region
from .ptx import find_by_ct, parse_three_point, r2, r4, r5
from .timing import ZERO_TICKS

def extract_audio_files(data: bytes, top: list) -> List[str]:
    """
    Parse the PT registered audio-file table using the same counting rule as
    libptformat: folder/group entries do NOT consume one of nwavs.

    Earlier versions incremented the counter for "Audio Files", which shifted
    every source index by one and stopped before the final real media file.
    """
    audio_files = []

    for b1004 in find_by_ct(top, 0x1004):
        if b1004[3] + 6 > len(data):
            continue

        nwavs = r4(data, b1004[3] + 2)

        for b103a in [c for c in b1004[4] if c[1] == 0x103a]:
            pos = b103a[3] + 11
            valid_count = 0

            while pos < b103a[3] + b103a[2] and valid_count < nwavs:
                if pos + 4 > len(data):
                    break

                nl = r4(data, pos)
                if nl == 0 or nl > 512 or pos + 4 + nl > len(data):
                    pos += 4
                    continue

                fname = data[pos + 4:pos + 4 + nl].decode(
                    "utf-8", "replace"
                ).rstrip("\x00")
                pos += 4 + nl

                if pos + 9 > len(data):
                    break

                wavtype = data[pos:pos + 4].decode("latin-1", "replace")
                pos += 9  # 4-byte type + 5 bytes metadata/padding

                # These are structural/path entries, not audio files.
                if (
                    ".grp" in fname
                    or "Audio Files" in fname
                    or "Fade Files" in fname
                ):
                    continue

                # Mirror libptformat's accepted media types. For newer PTX
                # files wavtype can be NUL and the extension is authoritative.
                type_ok = any(x in wavtype for x in (
                    "WAVE", "EVAW", "AIFF", "FFIA", "fFXM", "MXFf"
                ))
                ext_ok = fname.lower().endswith((
                    ".wav", ".wave", ".aif", ".aiff", ".mxf"
                ))

                if not (type_ok or ext_ok):
                    continue

                audio_files.append(fname)
                valid_count += 1

    return audio_files


def read_pt_string(data: bytes, pos: int) -> Tuple[str, int]:
    """
    Read a ptformat-style length-prefixed string.

    Returns:
        (string, position immediately after string)
    """
    if pos + 4 > len(data):
        return "", pos

    length = r4(data, pos)
    pos += 4

    if length < 0 or length > 4096 or pos + length > len(data):
        return "", pos

    value = data[pos:pos + length].decode(
        "utf-8", "replace"
    ).rstrip("\x00")

    return value, pos + length


def extract_audio_tracks(data: bytes, top: list) -> List[AudioTrack]:
    """
    Parse PT audio-track definitions.

    Based on libptformat parserest():
        0x1015 = AUDIO tracks
        0x1014 = AUDIO track name / channel mapping
    """
    tracks = []
    seen_offsets = set()

    for container in find_by_ct(top, 0x1015):
        for block in container[4]:
            if block[1] != 0x1014:
                continue

            # Our recursive parser can sometimes encounter the same
            # underlying block more than once.
            if block[3] in seen_offsets:
                continue

            seen_offsets.add(block[3])

            j = block[3] + 2

            name, after_name = read_pt_string(data, j)

            if not name:
                continue

            # ptformat:
            #
            # j = c->offset + 2;
            # trackname = parsestring(j);
            # j += trackname.size() + 5;
            # nch = read4(j);
            #
            # after_name points one byte earlier than that final j.
            j = after_name + 1

            if j + 4 > len(data):
                continue

            nch = r4(data, j)
            j += 4

            # Protect against corrupt / misidentified structures.
            if nch < 1 or nch > 8:
                continue

            channel_ids = []

            for _ in range(nch):
                if j + 2 > len(data):
                    break

                channel_ids.append(r2(data, j))
                j += 2

            if channel_ids:
                tracks.append(AudioTrack(name, channel_ids))

    return tracks


def extract_regions(
    data: bytes,
    top: list,
    verbose: bool = False,
) -> List[Region]:
    """
    Extract audio regions in exactly the order used by libptformat.

    This detail is critical: 0x104F placement records refer to regions by
    their sequential index. A recursive search for every 0x2629 block can
    change that ordering (and can include blocks outside the actual region
    list), causing perfectly valid indexes to resolve to the wrong region.

    libptformat only indexes direct 0x1008/0x2629 children of top-level
    0x100B/0x262A audio-region-list blocks.
    """
    regions: List[Region] = []

    region_lists = [
        b for b in top
        if b[1] in (0x100B, 0x262A)
    ]

    if verbose:
        print(f"  Audio region-list blocks: {len(region_lists)}")

    for region_list in region_lists:
        for block in region_list[4]:
            if block[1] not in (0x1008, 0x2629):
                continue

            j = block[3] + 11
            if j + 4 > len(data):
                continue

            name, j_after_name = read_pt_string(data, j)
            if not name:
                continue

            # This mirrors ptformat:
            #   j = c->offset + 11
            #   regionname = parsestring(j)
            #   j += regionname.size() + 4
            #   r.index = rindex
            #   parse_region_info(j, *d, r)
            #
            # Our parse_three_point() reads the same timing triple.
            j = j_after_name
            src_off, length, start = parse_three_point(data, j)

            if length <= 0:
                continue

            # libptformat does NOT infer the source file from the region
            # name. The source WAV index is stored immediately after the
            # region's first child block:
            #
            #   findex = read4(child.offset + child.block_size)
            #
            file_index = -1
            if block[4]:
                source_child = block[4][0]
                findex_pos = source_child[3] + source_child[2]

                if findex_pos + 4 <= len(data):
                    file_index = r4(data, findex_pos)

            region_index = len(regions)

            regions.append(
                Region(
                    region_index,
                    name,
                    start,
                    length,
                    src_off,
                    file_index,
                )
            )

    return regions


def build_wav_index(audio_dir: Path) -> Dict[str, str]:
    """stem → full path for every wav file on disk."""
    index = {}

    for f in audio_dir.iterdir():
        if f.suffix.lower() in (".wav", ".aif", ".aiff"):
            index[f.stem.lower()] = str(f)

    return index


def _normalise_media_name(name: str) -> str:
    """Normalise a PT registered media name to a disk stem."""
    name = Path(name.replace("\\", "/")).name
    return Path(name).stem.lower().strip()


def build_registered_media_map(
    registered_audio_files: List[str],
    wav_index: Dict[str, str],
    verbose: bool = False,
) -> Dict[int, str]:
    """
    Map Pro Tools' registered audio-file indexes to real files on disk.

    This is the important distinction that earlier versions missed:
    region names are edit/clip names; they are not necessarily media names.
    """
    result: Dict[int, str] = {}

    for i, registered in enumerate(registered_audio_files):
        key = _normalise_media_name(registered)
        path = wav_index.get(key)

        if path is None:
            # Conservative case-insensitive prefix fallback for extension/
            # naming differences, but still driven by the PT media name.
            candidates = [
                p for stem, p in wav_index.items()
                if stem == key or stem.startswith(key) or key.startswith(stem)
            ]

            if len(candidates) == 1:
                path = candidates[0]

        if path:
            result[i] = path

            if verbose:
                print(
                    f"    MEDIA [{i}] {registered!r} -> "
                    f"{Path(path).name!r}"
                )

        elif verbose:
            print(f"    MEDIA [{i}] {registered!r} -> NOT FOUND")

    return result


def match_regions_to_wavs(
    regions: List[Region],
    registered_media_map: Dict[int, str],
    verbose: bool = False,
) -> None:
    """Attach each Region to its real PT source file by file index."""

    for region in regions:
        region.wav_file = registered_media_map.get(
            region.file_index,
            "",
        )

        if verbose:
            status = (
                Path(region.wav_file).name
                if region.wav_file
                else "NOT FOUND"
            )

            print(
                f"    REGION [{region.index:02d}] "
                f"file_index={region.file_index} "
                f"source={status!r} clip={region.name!r}"
            )

def assign_regions_to_tracks(
    data: bytes,
    top: list,
    tracks: List[AudioTrack],
    regions: List[Region],
    session_rate: int,
    session_timecode_origin_samples: Optional[int] = None,
    verbose: bool = False,
) -> Tuple[Dict[str, List[ClipPlacement]], int, int]:
    """
    Build actual timeline placements instead of treating Region definitions
    themselves as timeline clips.

    Each PT channel gets its own REAPER track for now. This is deliberate:
    PT stereo tracks are stored as paired mono channel maps, and keeping the
    channels separate is lossless while we continue reverse-engineering the
    stereo relationship.
    """

    def debug(*args, **kwargs) -> None:
        if verbose:
            print(*args, **kwargs)

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

    for i, track in enumerate(channel_tracks):
        debug(
            f"    [{i:02d}] id={track['id']} "
            f"logical={track['logical_index']} "
            f"channel={track['channel_number']} "
            f"name='{track['name']}'"
        )

    # Separate mono channel tracks are the safest representation in REAPER.
    track_dict: Dict[str, List[ClipPlacement]] = {}
    region_by_index = {region.index: region for region in regions}

    full_maps = [
        block for block in top
        if block[1] == 0x1054
    ]

    if not full_maps:
        seen = set()
        full_maps = []

        for block in find_by_ct(top, 0x1054):
            if block[3] not in seen:
                seen.add(block[3])
                full_maps.append(block)

    debug(f"  PT8+ 0x1054 maps: {len(full_maps)}")

    total_placements = 0
    inactive_placements_skipped = 0
    timestamp_positions_corrected = 0

    for map_no, full_map in enumerate(full_maps):
        map_entries = [
            child for child in full_map[4]
            if child[1] == 0x1052
        ]

        debug(
            f"  MAP BLOCK #{map_no} "
            f"offset=0x{full_map[3]:08X}"
        )
        debug(f"    0x1052 entries: {len(map_entries)}")

        for count, map_entry in enumerate(map_entries):
            entry_label, _ = read_pt_string(
                data,
                map_entry[3] + 2,
            )

            if count < len(channel_tracks):
                channel_track = channel_tracks[count]

                logical_name = channel_track["name"]
                channel_id = channel_track["id"]
                channel_number = channel_track["channel_number"]

            else:
                logical_name = entry_label or f"_map_{count}"
                channel_id = -1
                channel_number = count

            suffix = (
                "L"
                if channel_number == 0
                else "R"
                if channel_number == 1
                else f"ch{channel_number + 1}"
            )

            # 0x1052's own label is the playlist/map identity. For ordinary
            # tracks it normally matches the logical 0x1014 name; for
            # alternate Pro Tools playlists it carries names such as:
            #
            #   Vocals Chorus.01
            #   Vocals Chorus.02
            #
            # Keep that identity instead of replacing it with the parallel
            # 0x1014 name by array position.
            mapped_name = (
                entry_label.strip()
                if entry_label
                else logical_name
            )

            if not mapped_name:
                mapped_name = logical_name or f"_map_{count}"

            output_track = f"{mapped_name} [{suffix}]"

            track_dict.setdefault(output_track, [])

            placements = [
                child for child in map_entry[4]
                if child[1] == 0x1050
            ]

            debug(
                f"    ENTRY [{count:02d}] "
                f"label={entry_label!r} "
                f"-> '{output_track}' "
                f"placements={len(placements)}"
            )

            for placement_no, placement in enumerate(placements):

                # Keep the old fade test only as a diagnostic. The earlier
                # +46 byte sometimes points beyond the 0x1050 payload, so
                # this version does not discard a placement on that basis.
                refs = [
                    child for child in placement[4]
                    if child[1] == 0x104F
                ]

                for ref_no, ref in enumerate(refs):
                    j = ref[3] + 4

                    if j + 16 > len(data):
                        continue

                    raw_index = r4(data, j)
                    raw_start5 = r5(data, j + 5)

                    timeline_start = (
                        raw_start5 - ZERO_TICKS
                        if raw_start5 >= ZERO_TICKS
                        else raw_start5
                    )

                    region = region_by_index.get(raw_index)

                    if region is None:
                        debug(
                            f"      SKIP p={placement_no:02d} "
                            f"r={ref_no:02d} "
                            f"unknown region index {raw_index}"
                        )
                        continue

                    meta = data[j + 10:j + 16]

                    placement_kind = (
                        data[j + 13]
                        if j + 13 < len(data)
                        else None
                    )

                    # meta[2] == 0x40 marks the timestamp/original-position
                    # class observed in Greyscale. For these live placements,
                    # the visible PT position is reconstructed from the
                    # region's absolute media timestamp rather than the
                    # ordinary 0x104F position field:
                    #
                    #   region.start
                    #   - region.src_offset
                    #   - session TC origin
                    #
                    timestamp_positioned = (
                        len(meta) >= 3
                        and meta[2] == 0x40
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
                                f"0x104F="
                                f"{timeline_start / session_rate:9.3f}s "
                                f"timestamp="
                                f"{timestamp_start / session_rate:9.3f}s "
                                f"name='{region.name}'"
                            )

                            timeline_start = timestamp_start
                            timestamp_positions_corrected += 1

                        else:
                            debug(
                                f"      TC40 correction rejected "
                                f"idx={raw_index:02d}: "
                                f"derived negative position "
                                f"{timestamp_start / session_rate:.6f}s"
                            )

                    # PT8+ 0x1050 contains the 0x104F region reference plus a
                    # one-byte trailing placement-state value.
                    #
                    # Reverse engineering across the test sessions shows:
                    #
                    #   kind 0x03 + state 0x00 -> live timeline clip
                    #   kind 0x03 + state 0x01 -> stale/inactive reference
                    #
                    placement_state_pos = (
                        placement[3]
                        + placement[2]
                        - 1
                    )

                    placement_state = (
                        data[placement_state_pos]
                        if 0 <= placement_state_pos < len(data)
                        else None
                    )

                    # j+13 separates the main live/aux reference classes.
                    if placement_kind != 0x03:
                        debug(
                            f"      AUX  p={placement_no:02d} "
                            f"idx={raw_index:02d} "
                            f"kind=0x{placement_kind:02X} "
                            f"state={placement_state!r} "
                            f"at="
                            f"{timeline_start / session_rate:9.3f}s "
                            f"name='{region.name}'"
                        )
                        continue

                    # A 0x03 reference with trailing state 0x01 is not a live
                    # timeline clip.
                    if placement_state == 0x01:
                        inactive_placements_skipped += 1

                        debug(
                            f"      INACTIVE "
                            f"p={placement_no:02d} "
                            f"idx={raw_index:02d} "
                            f"kind=0x03 state=0x01 "
                            f"at="
                            f"{timeline_start / session_rate:9.3f}s "
                            f"name='{region.name}'"
                        )

                        continue

                    clip_placement = ClipPlacement(
                        region=region,
                        track_name=mapped_name,
                        channel_id=channel_id,
                        channel_number=channel_number,
                        timeline_start=timeline_start,
                        raw_start5=raw_start5,
                        meta=meta,
                    )

                    track_dict[output_track].append(
                        clip_placement
                    )

                    total_placements += 1

                    debug(
                        f"      CLIP p={placement_no:02d} "
                        f"idx={raw_index:02d} "
                        f"at="
                        f"{timeline_start / session_rate:9.3f}s "
                        f"len="
                        f"{region.length / session_rate:8.3f}s "
                        f"name='{region.name}'"
                    )

    for placements in track_dict.values():
        placements.sort(
            key=lambda placement: placement.timeline_start
        )

    debug()
    debug(
        f"  Active timeline placements created: "
        f"{total_placements}"
    )
    debug(
        f"  Inactive/stale 0x03 placements skipped: "
        f"{inactive_placements_skipped}"
    )
    debug(
        "  Region definitions not referenced by the active map "
        "are intentionally not written to the RPP."
    )

    return (
        track_dict,
        inactive_placements_skipped,
        timestamp_positions_corrected,
    )

def dedupe_redundant_same_track_audio(
    track_dict: Dict[str, List[ClipPlacement]],
    verbose: bool = False,
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

    def debug(*args, **kwargs) -> None:
        if verbose:
            print(*args, **kwargs)

    removed = 0

    for track_name, placements in list(track_dict.items()):
        if len(placements) < 2:
            continue

        groups = {}

        for order, cp in enumerate(placements):
            region = cp.region

            if region.file_index is not None and region.file_index >= 0:
                source_identity = (
                    "file-index",
                    int(region.file_index),
                )

            elif region.wav_file:
                source_identity = (
                    "wav",
                    str(region.wav_file).lower(),
                )

            else:
                # Without source identity, only repeats of the exact Region
                # definition are safe to collapse.
                source_identity = (
                    "region-index",
                    int(region.index),
                )

            key = (
                source_identity,
                int(cp.timeline_start),
                int(region.src_offset),
            )

            groups.setdefault(key, []).append(
                (order, cp)
            )

        kept = []

        for key, members in groups.items():
            if len(members) == 1:
                kept.append(members[0])
                continue

            winner_order, winner = max(
                members,
                key=lambda pair: (
                    int(pair[1].region.length),
                    -pair[0],
                ),
            )

            kept.append((winner_order, winner))

            removed += len(members) - 1

            debug(
                f"  [audio] same-track duplicate group on "
                f"{track_name!r}: "
                f"kept {winner.region.name!r} "
                f"(len={winner.region.length}), "
                f"removed {len(members) - 1}"
            )

        kept.sort(
            key=lambda pair: pair[0]
        )

        track_dict[track_name] = [
            cp for _, cp in kept
        ]

    return removed


def _audio_name_match_score(
    track_name: str,
    region: Region,
) -> int:
    """
    Conservative affinity score used only to arbitrate a region that appears
    on more than one PT audio track.

    This is not used for normal track assignment.
    """

    def norm(value: str) -> str:
        value = value.lower().strip()
        value = re.sub(
            r"\s+\[[lrc]+\]$",
            "",
            value,
        )
        return value

    track = norm(track_name)
    clip = norm(region.name)

    wav = norm(
        Path(region.wav_file).stem
        if region.wav_file
        else ""
    )

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


def _audio_region_alias_key(
    region: Region,
):
    """
    Return a conservative identity for cross-track alias arbitration.

    Pro Tools can contain duplicate Region definitions with different region
    indexes even though they have the same clip name and registered source
    audio file. Group those definitions together so the alias-pruner can
    recognise them as siblings.

    The registered PT file_index is deliberately part of the key. Two clips
    with the same visible name but different source files are therefore NOT
    treated as aliases.

    If the source file index is unavailable, fall back to the original
    region-index identity rather than guessing.
    """
    if region.file_index is None or region.file_index < 0:
        return (
            "region-index",
            region.index,
        )

    name = (
        region.name or ""
    ).strip().lower()

    name = re.sub(
        r"\s+",
        " ",
        name,
    )

    if not name:
        return (
            "region-index",
            region.index,
        )

    return (
        "pt-source-name",
        region.file_index,
        name,
    )


def prune_spurious_cross_track_audio(
    track_dict: Dict[str, List[ClipPlacement]],
    verbose: bool = False,
) -> int:
    """
    Remove only high-confidence cross-track alias placements.

    PT 0x1054/0x1052 maps can contain references which look active but belong
    to another source/playlist context. We avoid broad filename filtering.

    A placement is removed only when:
      1. the same PT source/clip identity appears on multiple REAPER tracks;
      2. exactly one of those tracks has clearly stronger name/source affinity;
      3. the weaker placement overlaps another item already on its own track.

    This keeps legitimate copied clips on otherwise empty/non-overlapping
    tracks while removing high-confidence foreign aliases.

    Important safety rule:
      - If PT source-file identity is unavailable, grouping falls back to the
        exact Region index used by earlier PTX2RPP versions.
    """

    def debug(*args, **kwargs) -> None:
        if verbose:
            print(*args, **kwargs)

    by_identity: Dict[
        tuple,
        List[Tuple[str, ClipPlacement]],
    ] = {}

    for output_track, placements in track_dict.items():
        for cp in placements:
            key = _audio_region_alias_key(
                cp.region
            )

            by_identity.setdefault(
                key,
                [],
            ).append(
                (output_track, cp)
            )

    remove_ids = set()

    for identity, occurrences in by_identity.items():
        track_names = {
            track_name
            for track_name, _ in occurrences
        }

        if len(track_names) < 2:
            continue

        scored = [
            (
                _audio_name_match_score(
                    track_name,
                    cp.region,
                ),
                track_name,
                cp,
            )
            for track_name, cp in occurrences
        ]

        best_score = max(
            score
            for score, _, _ in scored
        )

        best_tracks = {
            track_name
            for score, track_name, _ in scored
            if score == best_score
        }

        # Require one unique, clearly matching owner.
        if best_score < 4 or len(best_tracks) != 1:
            continue

        owner_track = next(
            iter(best_tracks)
        )

        for score, track_name, cp in scored:
            if (
                track_name == owner_track
                or score >= best_score
            ):
                continue

            start = cp.timeline_start
            end = (
                start
                + max(0, cp.region.length)
            )

            # Preserve legitimate cross-track copies unless this foreign
            # placement actually collides with material already belonging to
            # the destination track.
            overlaps_other = False

            for other in track_dict.get(
                track_name,
                [],
            ):
                if other is cp:
                    continue

                other_start = other.timeline_start

                other_end = (
                    other_start
                    + max(0, other.region.length)
                )

                if (
                    start < other_end
                    and other_start < end
                ):
                    overlaps_other = True
                    break

            if not overlaps_other:
                continue

            remove_ids.add(
                id(cp)
            )

            sibling_indexes = sorted(
                {
                    sibling.region.index
                    for _, sibling in occurrences
                }
            )

            debug(
                f"  [audio] pruned cross-track alias: "
                f"{cp.region.name!r} "
                f"region={cp.region.index} "
                f"from {track_name!r}; "
                f"strong owner={owner_track!r}; "
                f"source_file_index={cp.region.file_index}; "
                f"sibling_regions={sibling_indexes}"
            )

    if not remove_ids:
        return 0

    removed = 0

    for track_name in list(track_dict):
        before = len(
            track_dict[track_name]
        )

        track_dict[track_name] = [
            cp
            for cp in track_dict[track_name]
            if id(cp) not in remove_ids
        ]

        removed += (
            before
            - len(track_dict[track_name])
        )

    return removed

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
    verbose: bool = False,
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

    def debug(*args, **kwargs) -> None:
        if verbose:
            print(*args, **kwargs)

    max_gap_samples = max(
        0,
        int(round(max_gap_ms * sample_rate / 1000.0)),
    )

    effective: Dict[int, int] = {}
    media_frames: Dict[str, Optional[int]] = {}
    healed = 0

    for track_name, placements in audio_tracks.items():
        ordered = sorted(
            placements,
            key=lambda p: p.timeline_start,
        )

        for i, placement in enumerate(ordered):
            region = placement.region
            base_len = max(0, int(region.length))

            effective[id(placement)] = base_len

            if (
                i + 1 >= len(ordered)
                or max_gap_samples <= 0
            ):
                continue

            next_placement = ordered[i + 1]

            next_start = int(
                next_placement.timeline_start
            )

            current_end = (
                int(placement.timeline_start)
                + base_len
            )

            if next_start <= current_end:
                continue

            gap = next_start - current_end

            if gap > max_gap_samples:
                continue

            wav_path = region.wav_file

            if not wav_path:
                continue

            if wav_path not in media_frames:
                media_frames[wav_path] = (
                    _media_frame_count(wav_path)
                )

            total_frames = media_frames[wav_path]

            if total_frames is None:
                continue

            target_len = (
                next_start
                - int(placement.timeline_start)
            )

            source_end = (
                int(region.src_offset)
                + target_len
            )

            if source_end > total_frames:
                continue

            effective[id(placement)] = target_len
            healed += 1

            debug(
                f"  [audio] healed short item on "
                f"{track_name!r}: "
                f"{region.name!r} "
                f"+{gap} samples "
                f"({gap / sample_rate * 1000.0:.2f} ms)"
            )

    return effective, healed