"""Low-level ISO-BMFF box parsing primitives.

Handles both 32-bit box sizes and the 64-bit `largesize` variant
(size field == 1 followed by a 64-bit size).
"""

import struct

BOX_HEADER = 8
BOX_HEADER_64 = 16


class Box:
    """A single box: its type, absolute offset, total size, and header size."""

    __slots__ = ("type", "offset", "size", "header")

    def __init__(self, type_, offset, size, header):
        self.type = type_
        self.offset = offset
        self.size = size
        self.header = header

    @property
    def payload_offset(self):
        return self.offset + self.header

    @property
    def end(self):
        return self.offset + self.size

    def __repr__(self):
        return f"<Box {self.type} off={self.offset} size={self.size} hdr={self.header}>"


def parse_box(data, offset, end):
    """Parse a single box header at `offset`. Returns a Box or None."""
    if offset + BOX_HEADER > end:
        return None
    size = struct.unpack(">I", data[offset : offset + 4])[0]
    type_ = data[offset + 4 : offset + 8].decode("latin1")
    header = BOX_HEADER
    if size == 1:
        if offset + BOX_HEADER_64 > end:
            return None
        size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
        header = BOX_HEADER_64
    elif size == 0:
        size = end - offset
    if size < header or offset + size > end:
        return None
    return Box(type_, offset, size, header)


def iter_boxes(data, start, end):
    """Yield all top-level boxes in the byte range [start, end)."""
    offset = start
    while offset + BOX_HEADER <= end:
        box = parse_box(data, offset, end)
        if box is None:
            break
        yield box
        offset = box.end


def find_child(data, box, type_):
    """Find the first child box of `type_` inside `box`."""
    for child in iter_boxes(data, box.payload_offset, box.end):
        if child.type == type_:
            return child
    return None


def descend(data, box, *path):
    """Walk a chain of child boxes, e.g. descend(moov, 'trak', 'mdia', 'minf')."""
    current = box
    for name in path:
        current = find_child(data, current, name)
        if current is None:
            return None
    return current


def box_bytes(data, box):
    """Raw bytes of a box including its header."""
    return data[box.offset : box.end]


def make_box(type_, payload):
    """Build a box with a 32-bit size header (used for rebuilt boxes)."""
    return struct.pack(">I", BOX_HEADER + len(payload)) + type_.encode("latin1") + payload


def u32(data, offset):
    return struct.unpack(">I", data[offset : offset + 4])[0]


def u64(data, offset):
    return struct.unpack(">Q", data[offset : offset + 8])[0]