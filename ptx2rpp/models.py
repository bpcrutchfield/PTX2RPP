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
