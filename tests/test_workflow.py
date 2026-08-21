"""Codec routing and end-to-end workflow behaviour."""

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from tk_optimize import (
    FFmpegError,
    FFmpegNotFoundError,
    UnsupportedCodecError,
    WorkflowError,
    dry_run,
    load_config,
    process_file,
)
from tk_optimize import ffmpeg as fmod
from tk_optimize.workflow import default_output_path, temporary_workspace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "tk-cli.py")


def _run_cli(args, stdin=None):
    return subprocess.run(
        [sys.executable, CLI] + args,
        capture_output=True,
        text=True,
        input=stdin,
    )


def test_h264_bypasses_ffmpeg(h264_fixture, tmp_path, monkeypatch):
    """The H.264 no-optimization path must never construct an FFmpegRunner."""
    monkeypatch.setattr(
        "tk_optimize.workflow.FFmpegRunner",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("FFmpegRunner must not be constructed")),
    )
    logs = []
    out = str(tmp_path / "out.mp4")
    result = process_file(
        h264_fixture,
        out_path=out,
        confirm=lambda: True,
        log=logs.append,
        config=load_config(cli_optimize_bitrate=False),
    )
    assert result.mode == "direct"
    assert result.preprocessed is False
    assert result.output_path == out
    assert os.path.isfile(out)
    assert result.checks and all(c.ok for c in result.checks)
    assert "H.264 preprocessing" not in "\n".join(logs)


def test_hevc_routes_to_conversion(runner, hevc_fixture, tmp_path):
    logs = []
    out = str(tmp_path / "out.mp4")
    result = process_file(hevc_fixture, out_path=out, confirm=lambda: True, runner=runner, log=logs.append)
    assert result.mode == "hevc_preprocessed"
    assert result.preprocessed is True
    assert result.output_path == out
    assert os.path.isfile(out)
    assert result.checks and all(c.ok for c in result.checks)
    text = "\n".join(logs)
    assert "[!] input codec: HEVC/H.265" in text
    assert "[+] converting HEVC -> H.264" in text
    assert "[+] H.264 preprocessing complete" in text
    assert f"[+] ffmpeg: {runner.ffmpeg_path}" in text


def test_hevc_rejection_cancels_cleanly(hevc_fixture, tmp_path):
    logs = []
    out = str(tmp_path / "out.mp4")
    result = process_file(hevc_fixture, out_path=out, confirm=lambda: False, log=logs.append)
    assert result.mode == "cancelled"
    assert result.preprocessed is False
    assert result.output_path is None
    assert not os.path.exists(out)
    text = "\n".join(logs)
    assert "[!] conversion cancelled" in text
    assert "[!] no output generated" in text


def test_hevc_missing_ffmpeg_is_clear_error(hevc_fixture, tmp_path):
    class BrokenRunner:
        def require(self, program):
            raise fmod.FFmpegNotFoundError(fmod.not_found_message(program))

    with pytest.raises(FFmpegNotFoundError, match="Set TK_FFMPEG or add ffmpeg to PATH"):
        process_file(
            hevc_fixture,
            out_path=str(tmp_path / "o.mp4"),
            confirm=lambda: True,
            runner=BrokenRunner(),
            log=lambda _l: None,
        )


def test_unsupported_codec_rejected(mpeg4_fixture, tmp_path):
    with pytest.raises(UnsupportedCodecError, match="unsupported video codec"):
        process_file(
            mpeg4_fixture,
            out_path=str(tmp_path / "o.mp4"),
            confirm=lambda: True,
            log=lambda _l: None,
        )


def test_unsupported_codec_message_lists_supported(mpeg4_fixture, tmp_path):
    with pytest.raises(UnsupportedCodecError) as excinfo:
        process_file(
            mpeg4_fixture,
            out_path=str(tmp_path / "o.mp4"),
            confirm=lambda: True,
            log=lambda _l: None,
        )
    msg = str(excinfo.value)
    assert "unsupported video codec" in msg
    assert "supported codecs" in msg


def test_dry_run_h264_plans_without_writing(h264_fixture, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = dry_run(h264_fixture, config=load_config(), log=lambda _l: None)
    assert result.mode == "direct"
    assert result.preprocessed is False
    assert result.output_path == os.path.join(
        "output", os.path.splitext(os.path.basename(h264_fixture))[0] + "-optimized.mp4"
    )
    assert not os.path.exists("output")
    assert os.listdir(tmp_path) == []


def test_dry_run_hevc_plans_route_without_ffmpeg(hevc_fixture, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class NoFfmpeg:
        def ffprobe(self, args):
            raise fmod.FFmpegNotFoundError(fmod.not_found_message("ffprobe"))

    result = dry_run(
        hevc_fixture, config=load_config(), log=lambda _l: None, runner=NoFfmpeg()
    )
    assert result.mode == "hevc_preprocessed"
    assert result.preprocessed is True
    assert not os.path.exists("output")
    assert os.listdir(tmp_path) == []


def test_dry_run_does_not_modify_input(h264_fixture, tmp_path, monkeypatch):
    before = open(h264_fixture, "rb").read()
    monkeypatch.chdir(tmp_path)
    dry_run(h264_fixture, config=load_config())
    assert open(h264_fixture, "rb").read() == before


def test_dry_run_unsupported_codec_rejected(mpeg4_fixture, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(UnsupportedCodecError, match="supported codecs"):
        dry_run(mpeg4_fixture, config=load_config())


class _StubRunner:
    ffmpeg_path = "/fake/ffmpeg"
    ffprobe_path = None

    def require(self, program):
        return self.ffmpeg_path

    def version(self, program):
        return None


def _track_tmpdirs(monkeypatch):
    created = []
    real_mkdtemp = tempfile.mkdtemp

    def fake_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        created.append(d)
        return d

    monkeypatch.setattr("tk_optimize.workflow.tempfile.mkdtemp", fake_mkdtemp)
    return created


def test_temp_workspace_removed_on_success():
    seen = []
    with temporary_workspace() as tmpdir:
        seen.append(tmpdir)
        with open(os.path.join(tmpdir, "preprocessed.mp4"), "wb") as fh:
            fh.write(b"x")
    assert not os.path.exists(seen[0])


def test_temp_workspace_preserved_on_failure_with_intermediate():
    seen = []
    with pytest.raises(RuntimeError):
        with temporary_workspace(preserve_if=lambda d: os.path.isfile(os.path.join(d, "preprocessed.mp4"))) as tmpdir:
            seen.append(tmpdir)
            with open(os.path.join(tmpdir, "preprocessed.mp4"), "wb") as fh:
                fh.write(b"partial")
            raise RuntimeError("boom")
    assert os.path.exists(seen[0])
    shutil.rmtree(seen[0])


def test_temp_workspace_removed_on_failure_without_intermediate():
    seen = []
    with pytest.raises(RuntimeError):
        with temporary_workspace(preserve_if=lambda d: os.path.isfile(os.path.join(d, "preprocessed.mp4"))) as tmpdir:
            seen.append(tmpdir)
            raise RuntimeError("boom")
    assert not os.path.exists(seen[0])


def test_hevc_success_cleans_workspace(runner, hevc_fixture, tmp_path, monkeypatch):
    created = _track_tmpdirs(monkeypatch)
    out = str(tmp_path / "out.mp4")
    result = process_file(
        hevc_fixture, out_path=out, confirm=lambda: True, runner=runner,
        log=lambda _l: None,
    )
    assert result.mode == "hevc_preprocessed"
    assert os.path.isfile(out)
    assert created and not os.path.exists(created[0])


def test_hevc_failure_preserves_intermediate(hevc_fixture, tmp_path, monkeypatch):
    created = _track_tmpdirs(monkeypatch)

    def boom(runner, src, dst, encode=None):
        with open(dst, "wb") as fh:
            fh.write(b"partial intermediate")
        raise FFmpegError("conversion failed (exit 1)")

    monkeypatch.setattr("tk_optimize.workflow.hevc_to_h264", boom)
    with pytest.raises(WorkflowError) as excinfo:
        process_file(
            hevc_fixture, out_path=str(tmp_path / "o.mp4"),
            confirm=lambda: True, runner=_StubRunner(), log=lambda _l: None,
        )
    kept = excinfo.value.intermediate_path
    assert kept is not None and os.path.isfile(kept)
    # preserved in OS temp storage, never beside the source input
    assert os.path.dirname(kept) != os.path.dirname(hevc_fixture)
    assert os.path.exists(created[0])
    shutil.rmtree(created[0])


def test_hevc_failure_without_intermediate_cleans_up(hevc_fixture, tmp_path, monkeypatch):
    created = _track_tmpdirs(monkeypatch)

    def boom(runner, src, dst, encode=None):
        raise FFmpegError("probe failed")

    monkeypatch.setattr("tk_optimize.workflow.hevc_to_h264", boom)
    with pytest.raises(WorkflowError) as excinfo:
        process_file(
            hevc_fixture, out_path=str(tmp_path / "o.mp4"),
            confirm=lambda: True, runner=_StubRunner(), log=lambda _l: None,
        )
    assert excinfo.value.intermediate_path is None
    assert created and not os.path.exists(created[0])


def test_default_output_path():
    assert default_output_path("dir/video.mp4") == "output/video-optimized.mp4"
    assert default_output_path("a.b.c.mov") == "output/a.b.c-optimized.mov"
    assert default_output_path("a.b.c.mov", output_dir="out") == "out/a.b.c-optimized.mov"


def test_cli_h264_direct(h264_fixture, tmp_path):
    copy = str(tmp_path / "copy.mp4")
    with open(h264_fixture, "rb") as fh:
        with open(copy, "wb") as out:
            out.write(fh.read())
    proc = _run_cli([copy, "--output-dir", str(tmp_path)])
    assert proc.returncode == 0
    assert "[+] input codec: H.264/AVC" in proc.stdout
    assert "[+] applying MP4 optimization transformation" in proc.stdout
    assert "[+] completed successfully" in proc.stdout
    assert os.path.isfile(str(tmp_path / "copy-optimized.mp4"))


def test_cli_hevc_reject_no_output(hevc_fixture, tmp_path):
    copy = str(tmp_path / "copy.mp4")
    with open(hevc_fixture, "rb") as fh:
        with open(copy, "wb") as out:
            out.write(fh.read())
    proc = _run_cli([copy, "--output-dir", str(tmp_path)], stdin="n\n")
    assert proc.returncode == 0
    assert "[!] input codec: HEVC/H.265" in proc.stdout
    assert "[!] conversion cancelled" in proc.stdout
    assert "[!] no output generated" in proc.stdout
    assert not os.path.isfile(str(tmp_path / "copy-optimized.mp4"))


def test_cli_hevc_accept_convert(runner, hevc_fixture, tmp_path):
    if runner.ffmpeg_path is None:
        pytest.skip("ffmpeg not available")
    copy = str(tmp_path / "copy.mp4")
    with open(hevc_fixture, "rb") as fh:
        with open(copy, "wb") as out:
            out.write(fh.read())
    proc = _run_cli([copy, "--output-dir", str(tmp_path)], stdin="y\n")
    assert proc.returncode == 0
    assert "[+] converting HEVC -> H.264" in proc.stdout
    assert "[+] H.264 preprocessing complete" in proc.stdout
    assert "[+] completed successfully" in proc.stdout
    out_path = str(tmp_path / "copy-optimized.mp4")
    assert os.path.isfile(out_path)
    # original input must be untouched
    with open(copy, "rb") as fh:
        assert fh.read() == open(hevc_fixture, "rb").read()


def test_cli_missing_ffmpeg_message(hevc_fixture, tmp_path, monkeypatch):
    copy = str(tmp_path / "copy.mp4")
    with open(hevc_fixture, "rb") as fh:
        with open(copy, "wb") as out:
            out.write(fh.read())
    monkeypatch.setenv("TK_FFMPEG", str(tmp_path / "missing-ffmpeg"))
    monkeypatch.setenv("TK_FFPROBE", str(tmp_path / "missing-ffprobe"))
    proc = _run_cli([copy, "--output-dir", str(tmp_path)], stdin="y\n")
    assert proc.returncode != 0
    assert "[!] TK_FFMPEG set but does not exist" in proc.stdout
    assert not os.path.isfile(str(tmp_path / "copy-optimized.mp4"))