"""Data models used by PTX2RPP."""

from typing import List


class AudioTrack:
    """A Pro Tools audio track and its associated channel IDs."""

    __slots__ = ("name", "channel_ids")

    def __init__(self, name: str, channel_ids: List[int]):
        self.name = name
        self.channel_ids = channel_ids

    def __repr__(self):
        return f"AudioTrack({self.name!r}, {self.channel_ids})"


class Region:
    """A Pro Tools audio region definition."""

    __slots__ = (
        "index",
        "name",
        "start",
        "length",
        "src_offset",
        "file_index",
        "wav_file",
    )

    def __init__(
        self,
        index,
        name,
        start,
        length,
        src_offset,
        file_index=-1,
    ):
        self.index = index
        self.name = name
        self.start = start
        self.length = length
        self.src_offset = src_offset
        self.file_index = file_index
        self.wav_file = ""


class ClipPlacement:
    """A timeline use of a Region definition on one PT channel."""

    __slots__ = (
        "region",
        "track_name",
        "channel_id",
        "channel_number",
        "timeline_start",
        "raw_start5",
        "meta",
    )

    def __init__(
        self,
        region: Region,
        track_name: str,
        channel_id: int,
        channel_number: int,
        timeline_start: int,
        raw_start5: int,
        meta: bytes,
    ):
        self.region = region
        self.track_name = track_name
        self.channel_id = channel_id
        self.channel_number = channel_number
        self.timeline_start = timeline_start
        self.raw_start5 = raw_start5
        self.meta = meta

class MidiNote:

    """A decoded Pro Tools MIDI note."""

    __slots__ = ("pos", "note", "length", "velocity")

    def __init__(self, pos, note, length, velocity):
        self.pos = int(pos)
        self.note = int(note)
        self.length = int(length)
        self.velocity = int(velocity)


class MidiRegionData:
    """Decoded MIDI note data belonging to a Pro Tools MIDI region."""

    __slots__ = ("index", "name", "notes", "length")

    def __init__(self, index, name, notes):
        self.index = index
        self.name = name
        self.notes = notes
        self.length = max(
            (n.pos + n.length for n in notes),
            default=0,
        )


class MidiPlacement:
    """A timeline placement of a Pro Tools MIDI region."""

    __slots__ = ("track_name", "region_index", "timeline_ticks")

    def __init__(self, track_name, region_index, timeline_ticks):
        self.track_name = track_name
        self.region_index = int(region_index)
        self.timeline_ticks = int(timeline_ticks)

class PlaylistLaneGroup:
    """A REAPER fixed-lane representation of a Pro Tools playlist family."""

    __slots__ = ("track_name", "lanes")

    def __init__(self, track_name: str):
        self.track_name = track_name
        self.lanes = []
        