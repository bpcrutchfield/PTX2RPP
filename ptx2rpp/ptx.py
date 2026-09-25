"""Low-level Pro Tools PTX binary parsing utilities."""

import struct
from typing import Optional, Tuple


def gen_xor_delta(xv: int, mul: int, neg: bool) -> int:
    for i in range(256):
        if ((i * mul) & 0xff) == xv:
            return ((-i) & 0xff) if neg else i
    return 0


def decrypt_ptx(raw: bytes) -> bytes:
    """Decrypt the PTX payload used by the tested Pro Tools PT10-12 family."""
    xor_type = raw[0x12]
    xor_value = raw[0x13]

    if xor_type == 0x01:
        xd = gen_xor_delta(xor_value, 53, False)
    else:  # 0x05 PT 10-12
        xd = gen_xor_delta(xor_value, 11, True)

    xxor = [(i * xd) & 0xff for i in range(256)]
    out = bytearray(raw[:0x14])

    for i in range(0x14, len(raw)):
        key = i & 0xff if xor_type == 0x01 else (i >> 12) & 0xff
        out.append(raw[i] ^ xxor[key])

    return bytes(out)


def r2(d: bytes, p: int) -> int:
    """Read a little-endian 2-byte unsigned integer."""
    return d[p] | (d[p + 1] << 8)


def r4(d: bytes, p: int) -> int:
    """Read a little-endian 4-byte unsigned integer."""
    return struct.unpack_from("<I", d, p)[0]


def r5(d: bytes, i: int) -> int:
    """Read a little-endian 5-byte unsigned integer."""
    return int.from_bytes(d[i:i + 5], byteorder="little", signed=False)


def parse_block(data: bytes, pos: int, parent_end: Optional[int] = None):
    if pos >= len(data) or data[pos] != 0x5a:
        return None

    end = parent_end if parent_end is not None else len(data)

    if pos + 9 > end:
        return None

    bt = r2(data, pos + 1)
    bs = r4(data, pos + 3)
    ct = r2(data, pos + 7)

    if bt & 0xff00 or bs > 0x4000000:
        return None

    be = pos + 7 + bs

    if be > len(data):
        return None

    children = []
    i = 1

    while i < bs:
        child = parse_block(data, pos + i, be)

        if child:
            children.append(child)
            i += child[2] + 7
        else:
            i += 1

    return (bt, ct, bs, pos + 7, children)


def find_top(data: bytes) -> list:
    """Find top-level PTX blocks."""
    blocks = []
    pos = 0x14

    while pos < len(data):
        block = parse_block(data, pos)

        if block:
            blocks.append(block)
            pos += block[2] + 7
        else:
            pos += 1

    return blocks


def find_by_ct(blocks: list, ct: int) -> list:
    """Recursively find blocks with a particular content type."""
    result = []

    for block in blocks:
        if block[1] == ct:
            result.append(block)

        result.extend(find_by_ct(block[4], ct))

    return result


def parse_three_point(data: bytes, j: int) -> Tuple[int, int, int]:
    """Return (source_offset, length, timeline_start), all in samples."""
    if j + 10 >= len(data):
        return 0, 0, 0

    offsetbytes = (data[j + 1] & 0xf0) >> 4
    lengthbytes = (data[j + 2] & 0xf0) >> 4
    startbytes = (data[j + 3] & 0xf0) >> 4
    base = j + 5

    def rle(pos, n):
        if n == 0 or pos + n > len(data):
            return 0

        value = 0

        for k in range(n):
            value |= data[pos + k] << (8 * k)

        return value

    src_off = rle(base, offsetbytes)
    length = rle(base + offsetbytes, lengthbytes)
    start = rle(base + offsetbytes + lengthbytes, startbytes)

    return src_off, length, start
