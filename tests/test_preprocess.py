"""HEVC -> H.264 preprocessing: argument construction and end-to-end behaviour."""

import os

import pytest

from tk_optimize.preprocess import (
    DEFAULT_H264_ENCODE,
    H264EncodeConfig,
    build_ffmpeg_args,
    delivery_encode,
    display_frame_rate,
    estimated_video_size,
    hevc_to_h264,
    probe_video,
    verify_delivery,
)
from tk_optimize.ffmpeg import FFmpegError

BASIC_PROPS = {
    "codec": "hevc",
    "width": 1920,
    "height": 1080,
    "pix_fmt": "yuv420p",
    "avg_frame_rate": "30/1",
    "r_frame_rate": "30/1",
    "color_primaries": "bt709",
    "color_transfer": "bt709",
    "color_space": "bt709",
    "color_range": "tv",
    "field_order": "progressive",
    "audio_count": 1,
    "audio_codecs": ["aac"],
}


def test_build_args_uses_libx264_defaults():
    args = build_ffmpeg_args(BASIC_PROPS, "in.mp4", "out.mp4")
    joined = " ".join(args)
    assert "-map 0:v:0 -map 0:a?" in joined
    assert "-c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p" in joined
    assert args[-3:-1] == ["-c:a", "copy"]  # audio is stream-copied
    assert args[-1] == "out.mp4"


def test_build_args_uses_explicit_encode_config():
    encode = H264EncodeConfig(encoder="libx264", preset="medium", crf=18, pix_fmt="yuv420p")
    args = build_ffmpeg_args(BASIC_PROPS, "in.mp4", "out.mp4", encode=encode)
    joined = " ".join(args)
    assert "-c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p" in joined


def test_build_args_preserves_colour_metadata():
    args = build_ffmpeg_args(BASIC_PROPS, "in.mp4", "out.mp4")
    joined = " ".join(args)
    assert "-color_primaries bt709" in joined
    assert "-color_trc bt709" in joined
    assert "-colorspace bt709" in joined
    assert "-color_range tv" in joined


def test_build_args_does_not_change_geometry_or_timing():
    args = build_ffmpeg_args(BASIC_PROPS, "in.mp4", "out.mp4")
    joined = " ".join(args)
    assert "-vf" not in joined and "-filter" not in joined
    assert "scale" not in joined and "fps" not in joined and "-r " not in joined
    assert "sar" not in joined and "setdar" not in joined
    assert "-noautorotate" in joined  # rotation is preserved as metadata, not applied


def test_build_args_preserves_interlaced_flag():
    props = dict(BASIC_PROPS, field_order="tt")
    args = build_ffmpeg_args(props, "in.mp4", "out.mp4")
    joined = " ".join(args)
    assert "-flags +ildct+ilme" in joined
    assert "-top 1" in joined


def test_display_frame_rate():
    assert display_frame_rate("30/1") == "30 fps"
    assert display_frame_rate("24000/1001") == "23.976 fps"
    assert display_frame_rate("0/0") == "variable"


def test_hevc_to_h264_preserves_properties(runner, hevc_fixture, tmp_path):
    dst = str(tmp_path / "converted.mp4")
    props = hevc_to_h264(runner, hevc_fixture, dst, encode=H264EncodeConfig(preset="ultrafast"))
    assert props["codec"] == "hevc"
    assert os.path.isfile(dst)

    out = probe_video(runner, dst)
    assert out["codec"] == "h264"
    assert (out["width"], out["height"]) == (props["width"], props["height"])
    assert out["audio_count"] == props["audio_count"]
    assert out["audio_codecs"] == props["audio_codecs"]
    assert display_frame_rate(out["avg_frame_rate"] or out["r_frame_rate"]) == display_frame_rate(
        props["avg_frame_rate"] or props["r_frame_rate"]
    )


def test_hevc_to_h264_rejects_avc_source(runner, h264_fixture, tmp_path):
    dst = str(tmp_path / "nope.mp4")
    with pytest.raises(FFmpegError, match="expected HEVC"):
        hevc_to_h264(runner, h264_fixture, dst, encode=H264EncodeConfig(preset="ultrafast"))
    assert not os.path.exists(dst)


def test_h264_encode_config_defaults():
    assert DEFAULT_H264_ENCODE.encoder == "libx264"
    assert DEFAULT_H264_ENCODE.preset == "slow"
    assert DEFAULT_H264_ENCODE.crf == 16
    assert DEFAULT_H264_ENCODE.pix_fmt == "yuv420p"


def test_h264_encode_config_overrides():
    cfg = H264EncodeConfig(encoder="libx264", preset="medium", crf=18, pix_fmt="yuv422p")
    assert cfg.preset == "medium"
    assert cfg.crf == 18
    assert cfg.pix_fmt == "yuv422p"


def test_h264_encode_config_is_frozen():
    with pytest.raises(Exception):
        DEFAULT_H264_ENCODE.crf = 0  # dataclasses.FrozenInstanceError


def test_h264_encode_config_invalid_values():
    for kwargs in (
        {"encoder": ""},
        {"encoder": "   "},
        {"preset": ""},
        {"pix_fmt": ""},
        {"crf": "16"},          # not an int
        {"crf": 16.5},          # not an int
        {"crf": True},          # bool is excluded
        {"crf": -1},
        {"crf": 52},
    ):
        with pytest.raises(ValueError):
            H264EncodeConfig(**kwargs)


def test_hevc_to_h264_does_not_swap_dimensions(runner, tmp_path):
    """Non-square sources must keep their orientation (no autorotate/resize)."""
    import subprocess as sp

    src = str(tmp_path / "landscape.mp4")
    sp.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=96x64:rate=10:duration=0.5",
            "-c:v", "libx265", "-x265-params", "log-level=error",
            "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an",
            src,
        ],
        check=True,
    )
    src_props = probe_video(runner, src)
    assert src_props["width"] == 96 and src_props["height"] == 64

    dst = str(tmp_path / "out.mp4")
    hevc_to_h264(runner, src, dst, encode=H264EncodeConfig(preset="ultrafast"))
    out = probe_video(runner, dst)
    assert (out["width"], out["height"]) == (96, 64)


def test_build_args_adds_delivery_rate_cap():
    args = build_ffmpeg_args(BASIC_PROPS, "in.mp4", "out.mp4", maxrate=20, bufsize=20)
    joined = " ".join(args)
    assert "-maxrate 20M" in joined
    assert "-bufsize 20M" in joined


def test_build_args_no_rate_cap_by_default():
    args = build_ffmpeg_args(BASIC_PROPS, "in.mp4", "out.mp4")
    joined = " ".join(args)
    assert "-maxrate" not in joined and "-bufsize" not in joined


def test_probe_reports_bitrate(runner, h264_fixture):
    props = probe_video(runner, h264_fixture)
    assert "bit_rate" in props


def test_delivery_encode_accepts_h264_source(runner, h264_fixture, tmp_path):
    dst = str(tmp_path / "delivery.mp4")
    props = delivery_encode(
        runner, h264_fixture, dst, maxrate=20, bufsize=20,
        encode=H264EncodeConfig(preset="ultrafast"),
    )
    assert props["codec"] == "h264"
    assert os.path.isfile(dst)
    out = probe_video(runner, dst)
    assert out["codec"] == "h264"
    assert (out["width"], out["height"]) == (props["width"], props["height"])


def test_delivery_encode_rejects_unsupported_source(mpeg4_fixture, runner, tmp_path):
    dst = str(tmp_path / "nope.mp4")
    with pytest.raises(FFmpegError, match="expected H.264 or HEVC"):
        delivery_encode(runner, mpeg4_fixture, dst)
    assert not os.path.exists(dst)


def test_estimated_video_size():
    assert estimated_video_size(60_000_000, 10) == 75_000_000
    assert estimated_video_size("60000000", "10.0") == 75_000_000
    assert estimated_video_size(None, 10) is None
    assert estimated_video_size(60_000_000, 0) is None
    assert estimated_video_size("junk", 10) is None


def test_verify_delivery_accepts_matching_props(runner, h264_fixture, tmp_path):
    dst = str(tmp_path / "delivery.mp4")
    props = delivery_encode(
        runner, h264_fixture, dst, encode=H264EncodeConfig(preset="ultrafast")
    )
    out = verify_delivery(runner, dst, props)
    assert out["codec"] == "h264"


def test_verify_delivery_rejects_resolution_change(runner, h264_fixture, tmp_path):
    dst = str(tmp_path / "delivery.mp4")
    props = delivery_encode(
        runner, h264_fixture, dst, encode=H264EncodeConfig(preset="ultrafast")
    )
    wrong = dict(props, width=1234, height=567)
    with pytest.raises(FFmpegError, match="resolution"):
        verify_delivery(runner, dst, wrong)


def test_verify_delivery_tolerates_avg_frame_rate_rounding():
    """120 fps source with an avg_frame_rate rounding artifact (1802/15) vs a
    clean delivery encode (120/1) must not fail verification."""
    import json
    from types import SimpleNamespace

    from tk_optimize.preprocess import verify_delivery

    class RoundingRunner:
        ffmpeg_path = "/fake/ffmpeg"
        ffprobe_path = "/fake/ffprobe"

        def ffprobe(self, args):
            payload = {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "pix_fmt": "yuv420p",
                        "bit_rate": "169996975",
                        "avg_frame_rate": "120/1",
                        "r_frame_rate": "120/1",
                        "sample_aspect_ratio": "1:1",
                        "display_aspect_ratio": "9:16",
                        "color_primaries": "bt709",
                        "color_transfer": "bt709",
                        "color_space": "bt709",
                        "color_range": "tv",
                        "field_order": "progressive",
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"duration": "30.0"},
            }
            return SimpleNamespace(
                ok=True, returncode=0, stdout=json.dumps(payload), stderr=""
            )

    src_props = {
        "codec": "h264",
        "width": 1920,
        "height": 1080,
        "pix_fmt": "yuv420p",
        "bit_rate": "169996975",
        "avg_frame_rate": "1802/15",
        "r_frame_rate": "120/1",
        "sample_aspect_ratio": "1:1",
        "display_aspect_ratio": "9:16",
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "color_space": "bt709",
        "color_range": "tv",
        "field_order": "progressive",
        "audio_count": 1,
        "audio_codecs": ["aac"],
        "duration": "30.02",
    }
    out = verify_delivery(RoundingRunner(), "/tmp/out.mp4", src_props)
    assert out["codec"] == "h264"