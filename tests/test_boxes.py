import struct

import pytest

from tk_optimize.boxes import (
    Box,
    find_child,
    iter_boxes,
    make_box,
    parse_box,
)


def test_parse_box_32bit():
    data = make_box("ftyp", b"\x00" * 24)
    box = parse_box(data, 0, len(data))
    assert box.type == "ftyp"
    assert box.offset == 0
    assert box.size == 32
    assert box.header == 8
    assert box.payload_offset == 8
    assert box.end == 32


def test_parse_box_64bit():
    payload = b"\x00" * 40
    data = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 16 + len(payload)) + payload
    box = parse_box(data, 0, len(data))
    assert box.type == "mdat"
    assert box.size == 56
    assert box.header == 16


def test_parse_box_size_zero_runs_to_end():
    data = struct.pack(">I", 0) + b"free" + b"\xaa" * 12
    box = parse_box(data, 0, len(data))
    assert box.type == "free"
    assert box.size == len(data)
    assert box.end == len(data)


def test_parse_box_truncated_returns_none():
    assert parse_box(b"\x00\x00\x00\x08abcd", 0, 4) is None


def test_iter_boxes_stops_at_garbage():
    data = make_box("aaaa", b"x" * 4) + b"garbage" + make_box("bbbb", b"y" * 4)
    boxes = list(iter_boxes(data, 0, len(data)))
    assert [b.type for b in boxes] == ["aaaa"]


def test_find_child_and_box_bytes():
    inner = make_box("ilst", make_box("\xa9too", b"data"))
    data = make_box("moov", make_box("udta", inner))
    moov = parse_box(data, 0, len(data))
    udta = find_child(data, moov, "udta")
    assert udta is not None
    ilst = find_child(data, udta, "ilst")
    assert ilst is not None
    too = find_child(data, ilst, "\xa9too")
    assert too is not None


def test_make_box_roundtrip():
    payload = b"\x00\x01\x02\x03"
    raw = make_box("btrt", payload)
    box = parse_box(raw, 0, len(raw))
    assert box.size == 12
    assert raw[box.payload_offset:box.end] == payload