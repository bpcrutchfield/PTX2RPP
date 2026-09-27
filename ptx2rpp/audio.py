"""Pro Tools audio parsing and media handling."""

from pathlib import Path
from typing import Dict, List, Tuple

from .models import AudioTrack, Region
from .ptx import find_by_ct, parse_three_point, r2, r4


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