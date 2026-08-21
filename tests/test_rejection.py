"""Clean-rejection paths: unsupported inputs must fail with clear errors."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tk_optimize import ParseError, parse_file, transform_file


def test_missing_file():
    with pytest.raises(ParseError, match="file not found"):
        parse_file("/nonexistent/nope.mp4")


def test_garbage_file(tmp_path):
    p = tmp_path / "junk.mp4"
    p.write_bytes(b"\x00\x01\x02\x03 not an mp4 at all " * 20)
    with pytest.raises(ParseError, match="no top-level boxes"):
        parse_file(str(p))


def test_audio_only_rejected(audio_only_fixture):
    with pytest.raises(ParseError, match="no video track found"):
        parse_file(audio_only_fixture)


def test_cli_missing_input_exits_nonzero():
    import subprocess

    cli = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tk-cli.py")
    proc = subprocess.run(
        [sys.executable, cli, "/nonexistent/nope.mp4"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "file not found" in proc.stdout + proc.stderr


def test_cli_existing_output_exits_nonzero(h264_fixture, tmp_path):
    import subprocess

    cli = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tk-cli.py")
    copy = tmp_path / "copy.mp4"
    copy.write_bytes(open(h264_fixture, "rb").read())
    # pre-create the output file
    (tmp_path / "copy-optimized.mp4").write_bytes(b"occupied")
    proc = subprocess.run(
        [sys.executable, cli, str(copy), "--output-dir", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "already exists" in proc.stdout + proc.stderr