"""P3: delivery bitrate optimization, thresholds, routing, dry-run bitrate,
and the semantic UI protocol.  FFmpeg behaviour is mocked via FakeRunner so
the encode-count contracts are deterministic.
"""

import json
import os
import shutil
from types import SimpleNamespace

import pytest

from tk_optimize import dry_run, load_config, process_file
from tk_optimize.preprocess import estimated_video_size, verify_delivery
from tk_optimize.ffmpeg import FFmpegError
from tk_optimize.workflow import _to_logger


class FakeRunner:
    """Deterministic FFmpeg/FFprobe stand-in for workflow tests.

    ``ffprobe`` reports the configured source properties; after the first
    ``ffmpeg()`` call the reported codec flips to ``h264`` and the target
    file is copied from ``encode_fixture`` so ``parse_file`` can succeed.
    """

    ffmpeg_path = "/fake/ffmpeg"
    ffprobe_path = "/fake/ffprobe"

    def __init__(self, codec="h264", bit_rate="60000000", width=1920, height=1080,
                 avg_frame_rate="30/1", r_frame_rate="30/1", duration="10.0",
                 audio_count=1, audio_codecs=("aac",), encode_fixture=None):
        self.props = {
            "codec": codec,
            "width": width,
            "height": height,
            "pix_fmt": "yuv420p",
            "bit_rate": bit_rate,
            "avg_frame_rate": avg_frame_rate,
            "r_frame_rate": r_frame_rate,
            "sample_aspect_ratio": "1:1",
            "display_aspect_ratio": "16:9",
            "color_primaries": "bt709",
            "color_transfer": "bt709",
            "color_space": "bt709",
            "color_range": "tv",
            "field_order": "progressive",
            "audio_count": audio_count,
            "audio_codecs": list(audio_codecs),
            "duration": duration,
        }
        self.encoded = False
        self.ffmpeg_calls = []
        self.ffprobe_calls = []
        self.encode_fixture = encode_fixture

    def require(self, program):
        return getattr(self, program + "_path")

    def version(self, program):
        return None

    def ffmpeg(self, args):
        self.ffmpeg_calls.append(list(args))
        self.encoded = True
        if self.encode_fixture and os.path.isfile(self.encode_fixture):
            shutil.copyfile(self.encode_fixture, args[-1])
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")

    def _streams(self):
        props = dict(self.props)
        if self.encoded:
            props["codec"] = "h264"
        streams = [{
            "codec_type": "video",
            "codec_name": props["codec"],
            "width": props["width"],
            "height": props["height"],
            "pix_fmt": props["pix_fmt"],
            "bit_rate": props["bit_rate"],
            "avg_frame_rate": props["avg_frame_rate"],
            "r_frame_rate": props["r_frame_rate"],
            "sample_aspect_ratio": props["sample_aspect_ratio"],
            "display_aspect_ratio": props["display_aspect_ratio"],
            "color_primaries": props["color_primaries"],
            "color_transfer": props["color_transfer"],
            "color_space": props["color_space"],
            "color_range": props["color_range"],
            "field_order": props["field_order"],
        }]
        for codec_name in props["audio_codecs"]:
            streams.append({"codec_type": "audio", "codec_name": codec_name})
        return streams

    def ffprobe(self, args):
        self.ffprobe_calls.append(list(args))
        payload = {"streams": self._streams(), "format": {"duration": self.props["duration"]}}
        return SimpleNamespace(
            ok=True, returncode=0, stdout=json.dumps(payload), stderr=""
        )


def _logs_for(fake, input_path, out_path, confirm, config=None):
    logs = []
    result = process_file(
        input_path, out_path=out_path, confirm=confirm, runner=fake,
        log=logs.append, config=config,
    )
    return result, logs, fake


# --- bitrate detection / thresholds ---------------------------------------


def test_bitrate_at_threshold_skips_prompt(h264_fixture, tmp_path):
    fake = FakeRunner(codec="h264", bit_rate="20000000", duration="10.0", encode_fixture=h264_fixture)
    result, logs, fake = _logs_for(fake, h264_fixture, str(tmp_path / "o.mp4"), confirm=lambda: True)
    assert result.delivery_encoded is False
    assert result.source_bitrate_mbps == 20.0
    assert not fake.ffmpeg_calls
    assert "[?] optimize video bitrate?" not in "\n".join(logs)
    assert "within recommended delivery range" in "\n".join(logs)


def test_bitrate_above_threshold_prompts_and_optimizes(h264_fixture, tmp_path):
    fake = FakeRunner(codec="h264", bit_rate="21000000", duration="10.0", encode_fixture=h264_fixture)
    result, logs, fake = _logs_for(fake, h264_fixture, str(tmp_path / "o.mp4"), confirm=lambda: True)
    assert result.delivery_encoded is True
    assert len(fake.ffmpeg_calls) == 1
    text = "\n".join(logs)
    assert "[?] optimize video bitrate? [Y/n]:" in text
    assert "this is above the recommended delivery range" in text


def test_bitrate_optimization_declined_keeps_source(h264_fixture, tmp_path):
    fake = FakeRunner(codec="h264", bit_rate="60000000", duration="10.0", encode_fixture=h264_fixture)
    result, logs, fake = _logs_for(fake, h264_fixture, str(tmp_path / "o.mp4"), confirm=lambda: False)
    assert result.delivery_encoded is False
    assert not fake.ffmpeg_calls
    assert os.path.isfile(str(tmp_path / "o.mp4"))
    text = "\n".join(logs)
    assert "keeping source video bitrate: 60 Mbps" in text
    assert "bitrate optimization skipped" in text


def test_unavailable_bitrate_skips_analysis(h264_fixture, tmp_path):
    fake = FakeRunner(codec="h264", bit_rate=None, duration="10.0", encode_fixture=h264_fixture)
    result, logs, fake = _logs_for(fake, h264_fixture, str(tmp_path / "o.mp4"), confirm=lambda: True)
    assert result.delivery_encoded is False
    assert result.source_bitrate_mbps is None
    assert "video bitrate unavailable; skipping bitrate analysis" in "\n".join(logs)


def test_optimize_disabled_never_probes(h264_fixture, tmp_path):
    fake = FakeRunner(codec="h264", bit_rate="60000000", duration="10.0", encode_fixture=h264_fixture)
    config = load_config(cli_optimize_bitrate=False)
    result, logs, fake = _logs_for(fake, h264_fixture, str(tmp_path / "o.mp4"), confirm=lambda: True, config=config)
    assert result.delivery_encoded is False
    assert not fake.ffmpeg_calls
    assert not fake.ffprobe_calls
    assert "[?] optimize video bitrate?" not in "\n".join(logs)


def test_optimize_forced_runs_delivery_encode(h264_fixture, tmp_path):
    fake = FakeRunner(codec="h264", bit_rate="2000000", duration="10.0", encode_fixture=h264_fixture)
    config = load_config(cli_optimize_bitrate=True)
    result, logs, fake = _logs_for(fake, h264_fixture, str(tmp_path / "o.mp4"), confirm=lambda: True, config=config)
    assert result.delivery_encoded is True
    assert result.target_bitrate_mbps == 20.0
    assert len(fake.ffmpeg_calls) == 1
    text = "\n".join(logs)
    assert "[?] optimize video bitrate?" not in text
    assert "bitrate optimization complete" in text


# --- routing encode-count contracts ---------------------------------------


def test_h264_optimization_is_single_delivery_encode(h264_fixture, tmp_path):
    fake = FakeRunner(codec="h264", bit_rate="60000000", duration="10.0", encode_fixture=h264_fixture)
    result, logs, fake = _logs_for(fake, h264_fixture, str(tmp_path / "o.mp4"), confirm=lambda: True)
    assert result.mode == "direct"
    assert result.preprocessed is False
    assert result.delivery_encoded is True
    assert result.source_bitrate_mbps == 60.0
    assert len(fake.ffmpeg_calls) == 1
    assert os.path.isfile(str(tmp_path / "o.mp4"))
    args = fake.ffmpeg_calls[0]
    assert "-maxrate" in args and "20M" in args
    assert "-bufsize" in args and "20M" in args
    assert args[args.index("-c:a") + 1] == "copy"


def test_h264_low_bitrate_never_encodes(h264_fixture, tmp_path):
    fake = FakeRunner(codec="h264", bit_rate="2000000", duration="10.0", encode_fixture=h264_fixture)
    result, logs, fake = _logs_for(fake, h264_fixture, str(tmp_path / "o.mp4"), confirm=lambda: True)
    assert result.mode == "direct"
    assert result.delivery_encoded is False
    assert not fake.ffmpeg_calls
    assert os.path.isfile(str(tmp_path / "o.mp4"))


def test_hevc_optimization_folds_into_single_encode(hevc_fixture, h264_fixture, tmp_path):
    fake = FakeRunner(codec="hevc", bit_rate="60000000", duration="10.0", encode_fixture=h264_fixture)
    result, logs, fake = _logs_for(fake, hevc_fixture, str(tmp_path / "o.mp4"), confirm=lambda: True)
    assert result.mode == "hevc_preprocessed"
    assert result.preprocessed is True
    assert result.delivery_encoded is True
    assert len(fake.ffmpeg_calls) == 1
    args = fake.ffmpeg_calls[0]
    assert "-maxrate" in args and "20M" in args
    assert args[args.index("-c:a") + 1] == "copy"
    assert "optimizing video bitrate while converting" in "\n".join(logs)


def test_hevc_without_optimization_is_single_conversion(hevc_fixture, h264_fixture, tmp_path):
    fake = FakeRunner(codec="hevc", bit_rate="2000000", duration="10.0", encode_fixture=h264_fixture)
    result, logs, fake = _logs_for(fake, hevc_fixture, str(tmp_path / "o.mp4"), confirm=lambda: True)
    assert result.mode == "hevc_preprocessed"
    assert result.preprocessed is True
    assert result.delivery_encoded is False
    assert len(fake.ffmpeg_calls) == 1
    args = fake.ffmpeg_calls[0]
    assert "-maxrate" not in args and "-bufsize" not in args
    assert args[args.index("-c:a") + 1] == "copy"


def test_delivery_encode_preserves_properties_reported(h264_fixture, tmp_path):
    fake = FakeRunner(codec="h264", bit_rate="60000000", duration="10.0", encode_fixture=h264_fixture)
    result, logs, fake = _logs_for(fake, h264_fixture, str(tmp_path / "o.mp4"), confirm=lambda: True)
    text = "\n".join(logs)
    assert "preserving resolution: 1920x1080" in text
    assert "preserving frame rate: 30 fps" in text
    assert "preserving audio stream" in text
    assert "video bitrate: 60 Mbps -> ~20 Mbps target" in text


# --- dry run ---------------------------------------------------------------


def test_dry_run_reports_bitrate_recommendation(h264_fixture, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = FakeRunner(codec="h264", bit_rate="60000000", duration="10.0", encode_fixture=h264_fixture)
    logs = []
    result = dry_run(h264_fixture, config=load_config(), log=logs.append, runner=fake)
    assert result.source_bitrate_mbps == 60.0
    assert result.delivery_encoded is True
    assert result.target_bitrate_mbps == 20.0
    assert not fake.ffmpeg_calls
    text = "\n".join(logs)
    assert "video bitrate: 60 Mbps" in text
    assert "bitrate optimization would be recommended" in text
    assert "target: ~20 Mbps" in text
    assert "estimated video size: ~75.0 MB" in text


def test_dry_run_hevc_bitrate_without_encoding(hevc_fixture, h264_fixture, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = FakeRunner(codec="hevc", bit_rate="60000000", duration="10.0", encode_fixture=h264_fixture)
    logs = []
    result = dry_run(hevc_fixture, config=load_config(), log=logs.append, runner=fake)
    assert result.mode == "hevc_preprocessed"
    assert result.source_bitrate_mbps == 60.0
    assert result.delivery_encoded is True
    assert not fake.ffmpeg_calls


def test_dry_run_within_range_reports_no_optimization(h264_fixture, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = FakeRunner(codec="h264", bit_rate="2000000", duration="10.0", encode_fixture=h264_fixture)
    logs = []
    result = dry_run(h264_fixture, config=load_config(), log=logs.append, runner=fake)
    assert result.delivery_encoded is False
    assert "within recommended delivery range" in "\n".join(logs)


# --- preprocess helpers -----------------------------------------------------


def test_estimated_video_size():
    assert estimated_video_size(60_000_000, 10) == 75_000_000
    assert estimated_video_size(None, 10) is None
    assert estimated_video_size(60_000_000, 0) is None


def test_verify_delivery_resolution_mismatch_raises(h264_fixture):
    fake = FakeRunner(codec="h264", width=800, height=600, duration="10.0", encode_fixture=h264_fixture)
    src_props = dict(fake.props, codec="hevc", width=1920, height=1080)
    with pytest.raises(FFmpegError, match="resolution"):
        verify_delivery(fake, "/tmp/out.mp4", src_props)


def test_verify_delivery_audio_mismatch_raises(h264_fixture):
    fake = FakeRunner(codec="h264", audio_count=0, audio_codecs=(), duration="10.0", encode_fixture=h264_fixture)
    src_props = dict(fake.props, audio_count=1, audio_codecs=["aac"])
    with pytest.raises(FFmpegError, match="audio tracks"):
        verify_delivery(fake, "/tmp/out.mp4", src_props)


# --- UI protocol ------------------------------------------------------------


def test_logger_render_roundtrip():
    from tk_optimize.ui import Message, Logger, render, render_all

    collected = []
    Logger(sink=collected.append).info("a").warning("b").success("c").dim("d").question("e")
    assert render_all(collected) == ["[+] a", "[!] b", "[+] c", "[#] d", "[?] e"]
    assert render(Message("error", "boom")) == "[!] boom"


def test_message_rejects_unknown_level():
    from tk_optimize.ui import Message

    with pytest.raises(ValueError, match="unknown level"):
        Message("nope", "text")


def test_plain_callable_log_gets_rendered_lines():
    lines = []
    logger = _to_logger(lines.append)
    logger.warning("careful").info("done")
    assert lines == ["[!] careful", "[+] done"]


def test_library_ui_has_no_rich_import():
    import inspect

    import tk_optimize.ui as ui_mod

    source = inspect.getsource(ui_mod)
    assert "import rich" not in source
    assert "from rich" not in source