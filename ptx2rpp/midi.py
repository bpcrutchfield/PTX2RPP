"""Pro Tools MIDI parsing and placement resolution."""

from typing import List

from .models import MidiNote, MidiRegionData
from .ptx import r4, r5


def _walk_blocks(blocks):
    """Yield every parsed PTX block recursively."""
    for block in blocks:
        yield block
        yield from _walk_blocks(block[4])


def extract_midi_event_chunks(
    data: bytes,
    top: list,
) -> List[MidiRegionData]:
    """
    Decode MdNLB event chunks using the same 5-byte event representation used
    by libptformat. In this PTX the event data uses 960,000 PT ticks per QN.

    The decoded lists are later linked to PT MIDI regions through the trailing
    u32 in each 0x2633 wrapper.
    """
    chunks = []

    all_blocks = list(
        _walk_blocks(top)
    )

    for block in [
        item
        for item in all_blocks
        if item[1] == 0x2000
    ]:
        start = block[3]
        end = min(
            len(data),
            block[3] + block[2],
        )

        k = start

        while k + 35 < end:
            p = data.find(
                b"MdNLB",
                k,
                end,
            )

            if p < 0:
                break

            q = p + 11

            if q + 9 > end:
                break

            n_events = r4(
                data,
                q,
            )

            q += 4

            zero_ticks = r5(
                data,
                q,
            )

            ep = q
            notes = []

            for _ in range(n_events):
                if ep + 18 > end:
                    break

                raw_pos = r5(
                    data,
                    ep,
                )

                pos = raw_pos - zero_ticks

                if pos < 0:
                    pos = 0

                note = data[ep + 8]

                length = r5(
                    data,
                    ep + 9,
                )

                velocity = data[ep + 17]

                if (
                    0 <= note <= 127
                    and 0 <= velocity <= 127
                    and length >= 0
                ):
                    notes.append(
                        MidiNote(
                            pos,
                            note,
                            length,
                            velocity,
                        )
                    )

                ep += 35

            chunks.append(
                MidiRegionData(
                    len(chunks),
                    f"MIDI Region {len(chunks)}",
                    notes,
                )
            )

            k = max(
                ep,
                p + 5,
            )

    return chunks