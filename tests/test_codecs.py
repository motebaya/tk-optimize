"""Codec detection helpers and H.264 vs HEVC transform eligibility."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tk_optimize import (
    H264_ENTRY_TYPES,
    HEVC_ENTRY_TYPES,
    TransformError,
    codec_label,
    is_h264,
    is_hevc,
    is_supported_input_codec,
    is_transformable,
    parse_file,
    transform_bytes,
)


def test_entry_type_contracts():
    assert H264_ENTRY_TYPES == ("avc1", "avc2", "avc3")
    assert HEVC_ENTRY_TYPES == ("hvc1", "hev1")


def test_is_h264_and_is_hevc():
    for codec in H264_ENTRY_TYPES:
        assert is_h264(codec)
        assert not is_hevc(codec)
    for codec in HEVC_ENTRY_TYPES:
        assert is_hevc(codec)
        assert not is_h264(codec)
    assert not is_h264("av01")
    assert not is_hevc("av01")


def test_is_supported_input_codec():
    for codec in H264_ENTRY_TYPES + HEVC_ENTRY_TYPES:
        assert is_supported_input_codec(codec)
    assert not is_supported_input_codec("av01")
    assert not is_supported_input_codec("vp09")
    assert not is_supported_input_codec("mp4v")
    assert not is_supported_input_codec("")


def test_transform_eligibility_distinction():
    # H.264 is directly transformable; HEVC is supported but not transformable.
    assert all(is_transformable(c) for c in H264_ENTRY_TYPES)
    assert not any(is_transformable(c) for c in HEVC_ENTRY_TYPES)
    assert not is_transformable("mp4v")


def test_codec_label():
    assert codec_label("avc1") == "H.264/AVC"
    assert codec_label("hvc1") == "HEVC/H.265"
    assert codec_label("mp4v") == "mp4v"


def test_transform_bytes_rejects_hevc(hevc_fixture):
    pf = parse_file(hevc_fixture)
    with pytest.raises(TransformError, match="requires H.264/AVC"):
        transform_bytes(pf)


def test_transform_bytes_rejects_unsupported(mpeg4_fixture):
    pf = parse_file(mpeg4_fixture)
    with pytest.raises(TransformError, match="requires H.264/AVC"):
        transform_bytes(pf)


def test_transform_bytes_accepts_h264(h264_fixture):
    pf = parse_file(h264_fixture)
    out = transform_bytes(pf)
    assert len(out) > len(pf.data)