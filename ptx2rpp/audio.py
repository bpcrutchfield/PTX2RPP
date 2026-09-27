"""Pro Tools audio parsing and media handling."""

from typing import List, Tuple

from .models import AudioTrack
from .ptx import find_by_ct, r2, r4


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
