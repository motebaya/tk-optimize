import os
import shutil
import subprocess

import pytest

from tk_optimize.ffmpeg import FFmpegRunner

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def _generate(args, path):
    if os.path.exists(path):
        return path
    subprocess.run(args, check=True)
    return path


@pytest.fixture(scope="session")
def runner():
    """A real FFmpegRunner, or skip if FFmpeg is unavailable."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not available")
    return FFmpegRunner()


@pytest.fixture(scope="session")
def h264_fixture():
    """A small valid H.264+AAC MP4 generated once per session."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not available to generate the H.264 fixture")
    os.makedirs(FIXTURES, exist_ok=True)
    path = os.path.join(FIXTURES, "small.mp4")
    if os.path.exists(path):
        return path
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
            "-c:a", "aac", "-shortest",
            path,
        ],
        check=True,
    )
    return path


@pytest.fixture(scope="session")
def audio_only_fixture():
    """An MP4 with no video track (rejection path)."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not available")
    os.makedirs(FIXTURES, exist_ok=True)
    path = os.path.join(FIXTURES, "audio-only.mp4")
    if os.path.exists(path):
        return path
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
            "-c:a", "aac", "-vn",
            path,
        ],
        check=True,
    )
    return path


@pytest.fixture(scope="session")
def hevc_fixture():
    """A small HEVC/H.265+AAC MP4 (requires an HEVC encoder)."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not available to generate the HEVC fixture")
    os.makedirs(FIXTURES, exist_ok=True)
    path = os.path.join(FIXTURES, "small-hevc.mp4")
    if os.path.exists(path):
        return path
    try:
        return _generate(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=0.5",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                "-c:v", "libx265", "-x265-params", "log-level=error",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest",
                path,
            ],
            path,
        )
    except subprocess.CalledProcessError:
        pytest.skip("no usable HEVC encoder (libx265) available")


@pytest.fixture(scope="session")
def mpeg4_fixture():
    """A small MPEG-4 Part 2 (mp4v) file for unsupported-codec rejection."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not available to generate the mpeg4 fixture")
    os.makedirs(FIXTURES, exist_ok=True)
    path = os.path.join(FIXTURES, "small-mpeg4.mp4")
    if os.path.exists(path):
        return path
    try:
        return _generate(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=0.5",
                "-c:v", "mpeg4", "-q:v", "5", "-an",
                path,
            ],
            path,
        )
    except subprocess.CalledProcessError:
        pytest.skip("no usable MPEG-4 encoder available")