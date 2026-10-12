"""Byte-identity regression tests for the reproduction lineages.

These require the large reference fixtures under ``samples/`` (gitignored)
and lineage-specific FFmpeg builds:

  shadow  (encode-patch)  — an ffmpeg 8.0 build (``TK_SHADOW_FFMPEG``)
  stealth (stream-copy)   — a Linux-built ffmpeg 6.1.x (``TK_STEALTH_FFMPEG``)

Everything is skipped when a fixture or build is absent, so the suite stays
green on machines without the investigation material.  Skips are NOT passes:
the release-readiness gate reports them separately.
"""

import hashlib
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

SAMPLES = os.path.join(REPO_ROOT, "samples")

from tk_optimize.method_patch import run as shadow_run
from tk_optimize.method_stream import run as stealth_run


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _require(input_name, ref_name, env_var):
    raw = os.path.join(SAMPLES, input_name)
    ref = os.path.join(SAMPLES, ref_name)
    if not (os.path.isfile(raw) and os.path.isfile(ref)):
        pytest.skip(f"reference fixtures missing under {SAMPLES}")
    ffmpeg = os.environ.get(env_var)
    if not (ffmpeg and os.path.isfile(ffmpeg)):
        pytest.skip(f"{env_var} must point at a suitable ffmpeg build")
    return raw, ref


# input, reference, kwargs for the lineage
SHADOW_CASES = [
    ("1080p_60fps_nohdr.mp4", "ref-shadow-sdr.mp4", {}),
    ("1080p_60fps_hdr10plus.mp4", "ref-shadow-hdr.mp4",
     {"video_bitrate": "34550k", "fps": "911556/15169"}),
]
STEALTH_CASES = [
    ("1080p_60fps_nohdr.mp4", "ref-stealth-sdr.mp4", {}),
    ("1080p_60fps_hdr10plus.mp4", "ref-stealth-hdr.mp4", {}),
]


@pytest.mark.parametrize(
    "input_name,ref_name,kwargs",
    SHADOW_CASES,
    ids=[case[1] for case in SHADOW_CASES],
)
def test_shadow_byte_identity(input_name, ref_name, kwargs, tmp_path):
    raw, ref = _require(input_name, ref_name, "TK_SHADOW_FFMPEG")
    out = tmp_path / "out.mp4"
    shadow_run(raw, str(out), ffmpeg=os.environ["TK_SHADOW_FFMPEG"], **kwargs)
    assert out.read_bytes() == open(ref, "rb").read()
    assert _sha256(str(out)) == _sha256(ref)


@pytest.mark.parametrize(
    "input_name,ref_name,kwargs",
    STEALTH_CASES,
    ids=[case[1] for case in STEALTH_CASES],
)
def test_stealth_byte_identity(input_name, ref_name, kwargs, tmp_path):
    raw, ref = _require(input_name, ref_name, "TK_STEALTH_FFMPEG")
    out = tmp_path / "out.mp4"
    stealth_run(raw, str(out), ffmpeg=os.environ["TK_STEALTH_FFMPEG"], **kwargs)
    assert out.read_bytes() == open(ref, "rb").read()
    assert _sha256(str(out)) == _sha256(ref)


def test_shadow_determinism(tmp_path):
    raw, _ = _require("1080p_60fps_nohdr.mp4", "ref-shadow-sdr.mp4", "TK_SHADOW_FFMPEG")
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    shadow_run(raw, str(first), ffmpeg=os.environ["TK_SHADOW_FFMPEG"])
    shadow_run(raw, str(second), ffmpeg=os.environ["TK_SHADOW_FFMPEG"])
    assert _sha256(str(first)) == _sha256(str(second))


def test_stealth_determinism(tmp_path):
    raw, _ = _require("1080p_60fps_nohdr.mp4", "ref-stealth-sdr.mp4", "TK_STEALTH_FFMPEG")
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    stealth_run(raw, str(first), ffmpeg=os.environ["TK_STEALTH_FFMPEG"])
    stealth_run(raw, str(second), ffmpeg=os.environ["TK_STEALTH_FFMPEG"])
    assert _sha256(str(first)) == _sha256(str(second))


def test_legacy_transform_determinism(h264_fixture):
    """Method 2 (legacy structural workflow) stays byte-stable across runs."""
    from tk_optimize import parse_file, transform_bytes

    first = transform_bytes(parse_file(h264_fixture))
    second = transform_bytes(parse_file(h264_fixture))
    assert first == second
