"""HEVC -> H.264 preprocessing (a compatibility layer, not part of the
optimization transformation).

Only the video codec changes: width, height, frame rate, duration, aspect
ratio, colour metadata and interlacing are preserved, and audio is
stream-copied (``-c:a copy``).  Encoding quality is controlled by a small
immutable :class:`H264EncodeConfig`; it never reaches the MP4
transformation, which operates purely on the H.264 intermediate file.
"""

import json
import os
from dataclasses import dataclass

from .ffmpeg import FFmpegError


@dataclass(frozen=True)
class H264EncodeConfig:
    """Immutable HEVC -> H.264 encoding preferences."""

    encoder: str = "libx264"
    preset: str = "slow"
    crf: int = 16
    pix_fmt: str = "yuv420p"

    def __post_init__(self):
        if not isinstance(self.encoder, str) or not self.encoder.strip():
            raise ValueError("encoder must be a non-empty string")
        if not isinstance(self.preset, str) or not self.preset.strip():
            raise ValueError("preset must be a non-empty string")
        if not isinstance(self.pix_fmt, str) or not self.pix_fmt.strip():
            raise ValueError("pix_fmt must be a non-empty string")
        if isinstance(self.crf, bool) or not isinstance(self.crf, int):
            raise ValueError("crf must be an integer")
        if not 0 <= self.crf <= 51:
            raise ValueError("crf must be between 0 and 51")


DEFAULT_H264_ENCODE = H264EncodeConfig()


def display_frame_rate(fraction):
    """Format an ffprobe frame-rate fraction (``"120/1"``) for humans."""
    try:
        num, den = (int(x) for x in fraction.split("/"))
    except (ValueError, AttributeError):
        return "unknown"
    if den <= 0:
        return "variable"
    if num % den == 0:
        return f"{num // den} fps"
    return f"{num / den:.3f} fps"


def probe_video(runner, src):
    """Probe the source and return a dict of the properties we preserve."""
    result = runner.ffprobe(
        ["-v", "error", "-print_format", "json", "-show_streams", "-show_format", src]
    )
    if not result.ok:
        raise FFmpegError(
            f"FFprobe failed (exit {result.returncode}):\n{result.stderr_tail}"
        )
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"FFprobe returned invalid JSON: {exc}")
    streams = info.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    if not videos:
        raise FFmpegError("FFprobe found no video stream")
    v = videos[0]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    return {
        "codec": v.get("codec_name"),
        "width": v.get("width"),
        "height": v.get("height"),
        "pix_fmt": v.get("pix_fmt"),
        "bit_rate": v.get("bit_rate"),
        "avg_frame_rate": v.get("avg_frame_rate"),
        "r_frame_rate": v.get("r_frame_rate"),
        "sample_aspect_ratio": v.get("sample_aspect_ratio"),
        "display_aspect_ratio": v.get("display_aspect_ratio"),
        "color_primaries": v.get("color_primaries"),
        "color_transfer": v.get("color_transfer"),
        "color_space": v.get("color_space"),
        "color_range": v.get("color_range"),
        "field_order": v.get("field_order"),
        "audio_count": len(audio),
        "audio_codecs": [s.get("codec_name") for s in audio],
        "duration": info.get("format", {}).get("duration"),
    }


def _add_color_metadata(args, props):
    for flag, key in (
        ("-color_primaries", "color_primaries"),
        ("-color_trc", "color_transfer"),
        ("-colorspace", "color_space"),
    ):
        value = props.get(key)
        if value:
            args += [flag, value]
    range_ = props.get("color_range")
    if range_:
        args += ["-color_range", {"tv": "tv", "limited": "tv", "pc": "pc", "full": "pc"}.get(range_, range_)]


def _add_field_order(args, props):
    order = props.get("field_order")
    if order in ("tt", "tb", "bb", "bt"):
        args += ["-flags", "+ildct+ilme"]
        args += ["-top", "1" if order in ("tt", "tb") else "0"]


def build_ffmpeg_args(props, src, dst, encode=DEFAULT_H264_ENCODE,
                      maxrate=None, bufsize=None):
    """Build the H.264 encode argument list.

    Conceptually equivalent to::

        ffmpeg -i INPUT
               -map 0:v:0 -map 0:a?
               -c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p
               -c:a copy
               OUTPUT

    plus colour-metadata/interlacing flags adapted from the source metadata.
    When ``maxrate``/``bufsize`` are given (in Mbps) a delivery rate cap
    ``-maxrate XM -bufsize YM`` is added on top of CRF rate control.
    No scaling, filtering, frame-rate changes or re-encoding of audio.
    ``-noautorotate`` keeps the source orientation (a rotation matrix in the
    source metadata is preserved as metadata instead of being applied to
    pixels).
    """
    args = ["-y", "-noautorotate", "-i", src]
    args += ["-map", "0:v:0", "-map", "0:a?"]
    args += [
        "-c:v",
        encode.encoder,
        "-preset",
        encode.preset,
        "-crf",
        str(encode.crf),
        "-pix_fmt",
        encode.pix_fmt,
    ]
    if maxrate is not None:
        args += ["-maxrate", f"{maxrate:g}M"]
    if bufsize is not None:
        args += ["-bufsize", f"{bufsize:g}M"]
    _add_color_metadata(args, props)
    _add_field_order(args, props)
    args += ["-c:a", "copy", dst]
    return args


def _run_encode(runner, src, dst, encode=DEFAULT_H264_ENCODE, maxrate=None,
                bufsize=None, operation="encode"):
    """Probe, encode ``src`` to ``dst``, and return the source properties.

    Raises :class:`FFmpegError` on failure.  The source file is never
    modified.
    """
    props = probe_video(runner, src)
    args = build_ffmpeg_args(
        props, src, dst, encode=encode, maxrate=maxrate, bufsize=bufsize
    )
    result = runner.ffmpeg(args)
    if not result.ok:
        raise FFmpegError(
            f"{operation} failed (exit {result.returncode}):\n"
            f"{result.stderr_tail}"
        )
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise FFmpegError("FFmpeg reported success but produced no output file")
    return props


def hevc_to_h264(runner, src, dst, encode=DEFAULT_H264_ENCODE):
    """Convert a HEVC file at ``src`` to an H.264 file at ``dst``.

    Returns the probed source properties.  Raises :class:`FFmpegError` on
    failure.  The source file is never modified.
    """
    props = probe_video(runner, src)
    if props["codec"] != "hevc":
        raise FFmpegError(
            f"source video is {props['codec']}, expected HEVC/H.265"
        )
    return _run_encode(
        runner, src, dst, encode=encode, operation="HEVC -> H.264 conversion"
    )


def delivery_encode(runner, src, dst, encode=DEFAULT_H264_ENCODE,
                    maxrate=None, bufsize=None):
    """Encode ``src`` (H.264 or HEVC) to an H.264 ``dst``.

    The single delivery encode: used for H.264 source files whose bitrate
    exceeds the recommended range (with a rate cap) and for HEVC inputs
    (with an optional rate cap).  Returns the probed source properties.
    Raises :class:`FFmpegError` on failure.
    """
    props = probe_video(runner, src)
    if props["codec"] not in ("h264", "hevc"):
        raise FFmpegError(
            f"source video is {props['codec']}, expected H.264 or HEVC"
        )
    return _run_encode(
        runner, src, dst, encode=encode, maxrate=maxrate, bufsize=bufsize,
        operation="delivery encode",
    )


def _parse_fraction(value):
    if not value:
        return None
    try:
        num, den = (int(x) for x in value.split("/"))
    except (ValueError, AttributeError):
        return None
    if den <= 0:
        return None
    return num / den


def _parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _frame_rate(props):
    """Return the video frame rate in fps, preferring the nominal rate.

    ``r_frame_rate`` is the container-declared rate and stays stable across an
    encode.  ``avg_frame_rate`` (``nb_frames / duration``) can drift from
    rounding or VFR padding -- e.g. ``1802/15`` vs ``120/1`` for the same
    120 fps stream -- so it is used only as a fallback.
    """
    return _parse_fraction(props.get("r_frame_rate")) or _parse_fraction(
        props.get("avg_frame_rate")
    )


def verify_delivery(runner, dst, src_props):
    """Verify a delivery encode preserved codec, geometry, timing, audio.

    Returns the probed output properties.  Raises :class:`FFmpegError` when
    the encoded file deviates from the source in any checked dimension.
    """
    out = probe_video(runner, dst)
    problems = []
    if out.get("codec") != "h264":
        problems.append(f"codec is {out.get('codec')}, expected h264")
    if (out.get("width"), out.get("height")) != (
        src_props.get("width"),
        src_props.get("height"),
    ):
        problems.append(
            f"resolution {out.get('width')}x{out.get('height')} != "
            f"{src_props.get('width')}x{src_props.get('height')}"
        )
    src_fps = _frame_rate(src_props)
    out_fps = _frame_rate(out)
    if src_fps and out_fps:
        tolerance = max(0.1, src_fps * 0.01)
        if abs(src_fps - out_fps) > tolerance:
            problems.append(
                f"frame rate {out.get('r_frame_rate') or out.get('avg_frame_rate')} != "
                f"{src_props.get('r_frame_rate') or src_props.get('avg_frame_rate')}"
            )
    src_duration = _parse_float(src_props.get("duration"))
    out_duration = _parse_float(out.get("duration"))
    if src_duration and out_duration and abs(src_duration - out_duration) > 0.5:
        problems.append(
            f"duration {out.get('duration')} != {src_props.get('duration')}"
        )
    if out.get("audio_count") != src_props.get("audio_count"):
        problems.append(
            f"audio tracks {out.get('audio_count')} != "
            f"{src_props.get('audio_count')}"
        )
    if problems:
        raise FFmpegError(
            "delivery encode verification failed: " + "; ".join(problems)
        )
    return out


def estimated_video_size(bitrate_bps, duration_seconds):
    """Approximate video size in bytes, or None when unknown/invalid."""
    if not bitrate_bps or not duration_seconds:
        return None
    try:
        bps = int(bitrate_bps)
        seconds = float(duration_seconds)
    except (TypeError, ValueError):
        return None
    if bps <= 0 or seconds <= 0:
        return None
    return int(bps * seconds / 8)
