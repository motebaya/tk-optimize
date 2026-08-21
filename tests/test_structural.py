"""Structural verification of the transformation on arbitrary H.264 input.

Uses a small ffmpeg-generated H.264+AAC file to prove the transformation
generalises (dynamic sample counts, stco/co64, track layout).
"""

import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tk_optimize import ParseError, parse_file, transform_bytes, transform_file, verify_file
from tk_optimize.transformer import SHARED_SAMPLE_PAYLOAD, TARGET_ENCODER_MARKER

MOD = 9


def _read_u32(data, offset):
    return struct.unpack(">I", data[offset : offset + 4])[0]


def _patch_size(data, offset, size):
    return data[:offset] + struct.pack(">I", size) + data[offset + 4 :]


def _insert_child(data, parent, child, ancestors=()):
    """Insert ``child`` bytes inside ``parent``, fixing parent + ancestor sizes."""
    inserted = data[: parent.end] + child + data[parent.end :]
    inserted = _patch_size(
        inserted, parent.offset, _read_u32(inserted, parent.offset) + len(child)
    )
    for anc in ancestors:
        inserted = _patch_size(
            inserted, anc.offset, _read_u32(inserted, anc.offset) + len(child)
        )
    return inserted


def test_transform_generalizes_and_verifies(h264_fixture, tmp_path):
    pf = parse_file(h264_fixture)
    n = pf.video.table.sample_count
    k = n * MOD
    out = transform_bytes(pf)

    out_path = tmp_path / "out.mp4"
    out_path.write_bytes(out)

    ok, checks = verify_file(pf, str(out_path))
    assert ok, [c.detail for c in checks if c.failed]

    pf_out = parse_file(str(out_path))
    tops = [b.type for b in pf_out.tops]
    assert tops == ["ftyp", "free", "moov", "mdat"]

    v_out = pf_out.video
    assert v_out.codec == pf.video.codec
    assert (v_out.width, v_out.height) == (pf.video.width, pf.video.height)
    assert v_out.table.sample_count == n * 10
    assert v_out.table.stts[-1][1] == pf.video.table.stts[-1][1]
    assert v_out.table.stsc[-1] == (pf.video.table.chunk_count + 1, 1, 1)
    assert all(s == 8 for s in v_out.table.sample_sizes[n:])

    shift = pf_out.mdat_payload_offset - pf.mdat_payload_offset
    tail_abs = pf_out.mdat_payload_offset + len(pf.mdat_payload)
    assert v_out.table.chunk_offsets[: pf.video.table.chunk_count] == [
        o + shift for o in pf.video.table.chunk_offsets
    ]
    assert all(o == tail_abs for o in v_out.table.chunk_offsets[pf.video.table.chunk_count :])
    assert len(pf_out.mdat_payload) == len(pf.mdat_payload) + 8
    assert pf_out.mdat_payload[len(pf.mdat_payload) :] == SHARED_SAMPLE_PAYLOAD


def test_transform_is_deterministic(h264_fixture):
    pf = parse_file(h264_fixture)
    a = transform_bytes(pf)
    b = transform_bytes(parse_file(h264_fixture))
    assert a == b


def test_too_string_marker(h264_fixture, tmp_path):
    pf = parse_file(h264_fixture)
    out = tmp_path / "out.mp4"
    out.write_bytes(transform_bytes(pf))
    data = out.read_bytes()
    assert TARGET_ENCODER_MARKER.encode() in data
    assert b"Lavf61.7.100" not in data


def test_unrelated_lavf_bytes_untouched(h264_fixture, tmp_path):
    """The encoder marker is patched only at moov/udta/meta/ilst/(c)too/data.

    A ``Lavf`` byte sequence in an unrelated location must survive intact.
    """
    import tk_optimize.boxes as boxes
    from tk_optimize.boxes import make_box

    pf = parse_file(h264_fixture)
    udta = boxes.find_child(pf.data, pf.moov, "udta")
    assert udta is not None

    stray_payload = b"\x00\x00\x00\x00\x00\x00\x00\x01" + b"Lavf99.9.9"
    stray = make_box("data", stray_payload)
    modified = _insert_child(pf.data, udta, stray, ancestors=(pf.moov,))
    src = tmp_path / "mod.mp4"
    src.write_bytes(modified)

    out = transform_bytes(parse_file(str(src)))

    # the real .too marker is rewritten...
    assert TARGET_ENCODER_MARKER.encode() in out
    assert b"Lavf61.7.100" not in out
    # ...while the unrelated Lavf bytes under moov/udta/data are preserved
    assert make_box("data", stray_payload) in out


def test_transform_file_output_path_and_no_overwrite(h264_fixture, tmp_path):
    out = tmp_path / "copy.mp4"
    out.write_bytes(open(h264_fixture, "rb").read())
    result = transform_file(str(out))
    assert result == str(tmp_path / "copy-optimized.mp4")
    assert os.path.exists(result)
    # second run must refuse to overwrite
    with pytest.raises(Exception) as exc:
        transform_file(str(out))
    assert "already exists" in str(exc.value)
    # source untouched
    assert out.read_bytes() == open(h264_fixture, "rb").read()


def test_transform_file_rejects_source_as_output(h264_fixture):
    with pytest.raises(Exception) as exc:
        transform_file(h264_fixture, h264_fixture)
    assert "differ from the input" in str(exc.value)