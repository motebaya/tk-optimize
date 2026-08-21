"""End-to-end workflow: codec routing, optional preprocessing, optional
delivery bitrate optimization, and the MP4 optimization transformation.

Pipeline overview::

    input.mp4 -> inspect -> [delivery H.264 encode?] -> [HEVC->H.264?]
                 -> MP4 optimization transformation -> output/<stem>-optimized.mp4

Exactly one encode is ever run per workflow: an H.264 delivery encode (only
when bitrate optimization is chosen for an H.264 source), an HEVC -> H.264
conversion (bitrate optimization folded into that single encode when chosen),
or no encode at all (H.264 direct).  Intermediate files live in a temporary
directory, are removed on success, and are preserved (with their path
reported) if any step fails.  The original input is never modified.

The orchestration is kept in :func:`process_file`; the individual steps are
small helpers so the pipeline reads as:

    inspect input -> resolve output -> decide/prepare codec -> transform ->
    verify -> ProcessResult
"""

import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass

from .config import load_config
from .ffmpeg import FFmpegError, FFmpegNotFoundError, FFmpegRunner
from .parser import (
    ParseError,
    codec_label,
    is_h264,
    is_hevc,
    parse_file,
)
from .preprocess import (
    delivery_encode,
    display_frame_rate,
    estimated_video_size,
    hevc_to_h264,
    probe_video,
    verify_delivery,
)
from .transformer import EXTRA_SAMPLE_MULTIPLIER, TransformError, transform_file
from .ui import Logger, render
from .verifier import verify_file

CONVERT_QUESTION = "Convert to H.264 first? [Y/n]:"

MODE_DIRECT = "direct"
MODE_HEVC = "hevc_preprocessed"
MODE_CANCELLED = "cancelled"


class UnsupportedCodecError(Exception):
    """Raised when the video codec is neither H.264/AVC nor HEVC/H.265."""

    @staticmethod
    def message(codec):
        return (
            f"unsupported video codec: {codec}\n"
            "supported codecs: H.264/AVC and HEVC/H.265"
        )


class WorkflowError(Exception):
    """Raised when the workflow cannot produce output."""

    def __init__(self, message, intermediate_path=None, checks=None):
        super().__init__(message)
        self.intermediate_path = intermediate_path
        self.checks = checks


@dataclass
class ProcessResult:
    """Structured outcome of a workflow run."""

    codec: str
    label: str
    mode: str  # MODE_DIRECT | MODE_HEVC | MODE_CANCELLED
    output_path: str = None
    props: dict = None
    checks: list = None
    preprocessed: bool = None  # True when an HEVC->H.264 step ran
    delivery_encoded: bool = False  # True when a delivery encode ran
    source_bitrate_mbps: float = None  # detected source video bitrate
    target_bitrate_mbps: float = None  # delivery target when optimized
    input_samples: int = None
    input_chunks: int = None
    output_samples: int = None
    output_chunks: int = None


def _default_confirm():
    try:
        answer = input()
    except EOFError:
        return False
    return answer.strip().lower() not in ("n", "no", "0")


def _to_logger(log):
    """Normalize ``log`` (Logger, plain callable, or None) to a Logger."""
    if isinstance(log, Logger):
        return log
    if log is None:
        return Logger()
    sink = log
    return Logger(sink=lambda message: sink(render(message)))


def default_output_path(input_path, output_dir="output"):
    stem, ext = os.path.splitext(os.path.basename(input_path))
    return os.path.join(output_dir, stem + "-optimized" + ext)


def _display_fps(sample_count, timescale, duration):
    if duration <= 0 or timescale <= 0:
        return "?"
    fps = sample_count * timescale / duration
    return f"{fps:.3f}".rstrip("0").rstrip(".") + " fps"


def _format_duration(duration):
    if not duration or duration <= 0:
        return "?"
    return f"{duration:.3f}".rstrip("0").rstrip(".") + " s"


def _intermediate(tmp):
    """Return ``tmp`` when it holds a useful intermediate file, else None."""
    if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
        return tmp
    return None


def _intermediate_exists(tmpdir):
    return os.path.isfile(os.path.join(tmpdir, "preprocessed.mp4")) or os.path.isfile(
        os.path.join(tmpdir, "delivery.mp4")
    )


@contextmanager
def temporary_workspace(prefix="tk-optimize-", preserve_if=None):
    """Yield a fresh secure temp directory and clean it up afterwards.

    The directory is removed on success and on any exception unless
    ``preserve_if(tmpdir)`` returns True, in which case it is kept so the
    intermediate file can be inspected.  ``tempfile.mkdtemp`` guarantees the
    directory never sits next to the source input.
    """
    tmpdir = tempfile.mkdtemp(prefix=prefix)
    try:
        yield tmpdir
    except BaseException:
        if not (preserve_if and preserve_if(tmpdir)):
            shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    else:
        shutil.rmtree(tmpdir, ignore_errors=True)


def inspect_input(input_path: str, log=None) -> tuple:
    """Parse the input and return (pf, codec, label)."""
    pf = parse_file(input_path)
    codec = pf.video.codec
    return pf, codec, codec_label(codec)


def resolve_output(input_path: str, output_dir: str) -> str:
    """Resolve the default output path for a source in ``output_dir``."""
    return default_output_path(input_path, output_dir=output_dir)


def transform_and_verify(pf, out_path: str, log, mode: str, props=None,
                         intermediate_path=None, verbose=False) -> "ProcessResult":
    """Apply the MP4 transformation, verify it, and build a ProcessResult."""
    log.info("transforming...")
    try:
        transform_file(pf.path, out_path=out_path)
    except (ParseError, TransformError, OSError) as exc:
        raise WorkflowError(str(exc), intermediate_path=intermediate_path) from exc
    ok, checks = verify_file(pf, out_path)
    if verbose:
        for c in checks:
            if c.ok:
                log.dim(f"check: {c.name}")
            else:
                detail = f": {c.detail}" if c.detail else ""
                log.error(f"failed check: {c.name}{detail}")
    if not ok:
        raise WorkflowError(
            "structural verification failed",
            intermediate_path=intermediate_path,
            checks=checks,
        )
    n = pf.video.table.sample_count
    return ProcessResult(
        codec=pf.video.codec,
        label=codec_label(pf.video.codec),
        mode=mode,
        output_path=out_path,
        props=props,
        checks=checks,
        preprocessed=mode == MODE_HEVC,
        input_samples=n,
        input_chunks=pf.video.table.chunk_count,
        output_samples=n * 10,
        output_chunks=pf.video.table.chunk_count + n * EXTRA_SAMPLE_MULTIPLIER,
    )


def _mbps(value):
    """Source video bitrate in Mbps (from an ffprobe bit_rate string)."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value / 1_000_000


def _fmt_mbps(mbps):
    return f"{mbps:g} Mbps"


def _format_size(size):
    if size >= 1_000_000_000:
        return f"{size / 1_000_000_000:.1f} GB"
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.1f} KB"
    return f"{size} B"


def _probe_best_effort(runner, path):
    """Probe video properties, or None when ffprobe is unavailable/fails."""
    if runner is None:
        return None
    try:
        return probe_video(runner, path)
    except Exception:
        return None


def _bitrate_decision(props, config, confirm, log):
    """Analyze the source bitrate and decide on a delivery encode.

    Returns ``(should_optimize, source_mbps)``.  The prompt is rendered via
    ``log.question`` and answered by ``confirm()`` (no-arg) or the default
    ``input()`` reader.
    """
    source_mbps = _mbps(props.get("bit_rate"))
    if source_mbps is None:
        log.warning("video bitrate unavailable; skipping bitrate analysis")
        return False, None
    log.info(f"video bitrate: {_fmt_mbps(source_mbps)}")
    if config.optimize_bitrate is False:
        log.info("bitrate optimization skipped")
        return False, source_mbps
    if config.optimize_bitrate is True:
        return True, source_mbps
    min_mbps = config.recommended_min_bitrate_mbps
    max_mbps = config.recommended_max_bitrate_mbps
    if source_mbps <= max_mbps:
        log.success(
            f"within recommended delivery range ({min_mbps:g}-{max_mbps:g} Mbps)"
        )
        return False, source_mbps
    log.warning("this is above the recommended delivery range")
    log.success(f"recommended delivery range: {min_mbps:g}-{max_mbps:g} Mbps")
    size_bytes = estimated_video_size(props.get("bit_rate"), props.get("duration"))
    if size_bytes:
        log.info(f"estimated video size: ~{_format_size(size_bytes)}")
    log.question("optimize video bitrate? [Y/n]:")
    if not (confirm or _default_confirm)():
        log.info(f"keeping source video bitrate: {_fmt_mbps(source_mbps)}")
        log.info("bitrate optimization skipped")
        return False, source_mbps
    return True, source_mbps


def _log_runner_info(runner, log, encode=None, verbose=False):
    log.info(f"ffmpeg: {runner.require('ffmpeg')}")
    ffmpeg_version = runner.version("ffmpeg")
    if ffmpeg_version:
        log.info(f"version: {ffmpeg_version}")
    if verbose:
        if runner.ffprobe_path:
            log.dim(f"ffprobe: {runner.ffprobe_path}")
            ffprobe_version = runner.version("ffprobe")
            if ffprobe_version:
                log.dim(f"version: {ffprobe_version}")
        if encode is not None:
            log.dim(
                f"encode: {encode.encoder} / {encode.preset} / "
                f"crf={encode.crf} / {encode.pix_fmt}"
            )
            maxrate = getattr(encode, "maxrate_mbps", None)
            if maxrate:
                log.dim(f"maxrate: {maxrate:g}M")
            bufsize = getattr(encode, "bufsize_mbps", None)
            if bufsize:
                log.dim(f"bufsize: {bufsize:g}M")


def _log_preserved(props, log):
    log.info(f"preserving resolution: {props['width']}x{props['height']}")
    log.info(
        f"preserving frame rate: "
        f"{display_frame_rate(props['avg_frame_rate'] or props['r_frame_rate'])}"
    )
    if props["audio_count"]:
        log.info("preserving audio stream")
    else:
        log.warning("no audio track present")


def _delivery_encode_and_transform(runner, src, out_path, log, config,
                                   source_props, source_mbps, verbose=False):
    """Run one delivery H.264 encode, then the MP4 transformation."""
    with temporary_workspace(preserve_if=_intermediate_exists) as tmpdir:
        if verbose:
            log.dim(f"temp workspace: {tmpdir}")
        tmp = os.path.join(tmpdir, "delivery.mp4")
        _log_runner_info(runner, log, encode=config.delivery, verbose=verbose)
        log.info("optimizing video bitrate")
        try:
            props = delivery_encode(
                runner, src, tmp,
                encode=config.delivery.encode_config(),
                maxrate=config.delivery.maxrate_mbps,
                bufsize=config.delivery.bufsize_mbps,
            )
        except Exception as exc:
            raise WorkflowError(
                str(exc), intermediate_path=_intermediate(tmp)
            ) from exc
        log.success("bitrate optimization complete")
        log.info(
            f"video bitrate: {_fmt_mbps(source_mbps)} -> "
            f"~{_fmt_mbps(config.delivery.target_mbps)} target"
        )
        _log_preserved(props, log)
        verify_delivery(runner, tmp, source_props)
        try:
            pf_h264 = parse_file(tmp)
        except ParseError as exc:
            raise WorkflowError(
                f"delivery file is not a valid H.264 MP4: {exc}",
                intermediate_path=_intermediate(tmp),
            ) from exc
        log.info("applying MP4 optimization transformation")
        result = transform_and_verify(
            pf_h264, out_path, log, mode=MODE_DIRECT, props=props,
            intermediate_path=_intermediate(tmp), verbose=verbose,
        )
        result.delivery_encoded = True
        result.source_bitrate_mbps = source_mbps
        result.target_bitrate_mbps = config.delivery.target_mbps
    return result


def _process_h264(pf, out_path, confirm, runner, log, config, verbose=False):
    """Direct H.264 route with an optional delivery bitrate optimization."""
    log.info("input codec: H.264/AVC")
    source_mbps = None
    should_optimize = False
    props = None
    if config.optimize_bitrate is not False:
        if runner is None:
            try:
                runner = FFmpegRunner()
            except Exception:
                runner = None
        props = _probe_best_effort(runner, pf.path)
        if props is None:
            log.warning("ffprobe unavailable; skipping bitrate analysis")
        else:
            should_optimize, source_mbps = _bitrate_decision(
                props, config, confirm, log
            )
    if should_optimize:
        return _delivery_encode_and_transform(
            runner, pf.path, out_path, log, config, props, source_mbps,
            verbose=verbose,
        )
    log.info("applying MP4 optimization transformation")
    result = transform_and_verify(pf, out_path, log, mode=MODE_DIRECT, verbose=verbose)
    result.source_bitrate_mbps = source_mbps
    return result


def _process_hevc(pf, out_path, confirm, runner, log, label, config, verbose=False):
    log.warning(f"input codec: {label}")
    log.warning("H.264 preprocessing is required")
    log.question(CONVERT_QUESTION)
    if not (confirm or _default_confirm)():
        log.warning("conversion cancelled")
        log.warning("no output generated")
        return ProcessResult(
            codec=pf.video.codec, label=label, mode=MODE_CANCELLED, preprocessed=False
        )

    runner = runner or FFmpegRunner()
    source_mbps = None
    should_optimize = False
    props = None
    if config.optimize_bitrate is not False:
        props = _probe_best_effort(runner, pf.path)
        if props is None:
            log.warning("ffprobe unavailable; skipping bitrate analysis")
        else:
            should_optimize, source_mbps = _bitrate_decision(
                props, config, confirm, log
            )

    with temporary_workspace(preserve_if=_intermediate_exists) as tmpdir:
        if verbose:
            log.dim(f"temp workspace: {tmpdir}")
        tmp = os.path.join(tmpdir, "preprocessed.mp4")
        encode = config.delivery.encode_config() if should_optimize else config.h264_encode
        _log_runner_info(
            runner, log,
            encode=config.delivery if should_optimize else config.h264_encode,
            verbose=verbose,
        )
        if should_optimize:
            log.info("optimizing video bitrate while converting")
        log.info("converting HEVC -> H.264")
        try:
            if should_optimize:
                props = delivery_encode(
                    runner, pf.path, tmp, encode=encode,
                    maxrate=config.delivery.maxrate_mbps,
                    bufsize=config.delivery.bufsize_mbps,
                )
            else:
                props = hevc_to_h264(runner, pf.path, tmp, encode=config.h264_encode)
        except Exception as exc:
            raise WorkflowError(
                str(exc), intermediate_path=_intermediate(tmp)
            ) from exc

        _log_preserved(props, log)
        if should_optimize:
            log.success("bitrate optimization complete")
            log.info(
                f"video bitrate: {_fmt_mbps(source_mbps)} -> "
                f"~{_fmt_mbps(config.delivery.target_mbps)} target"
            )
            verify_delivery(runner, tmp, props)

        try:
            pf_h264 = parse_file(tmp)
        except ParseError as exc:
            raise WorkflowError(
                f"preprocessed file is not a valid H.264 MP4: {exc}",
                intermediate_path=_intermediate(tmp),
            ) from exc
        log.info("H.264 preprocessing complete")
        log.info("applying MP4 optimization transformation")
        result = transform_and_verify(
            pf_h264, out_path, log, mode=MODE_HEVC, props=props,
            intermediate_path=_intermediate(tmp), verbose=verbose,
        )
        result.delivery_encoded = should_optimize
        result.source_bitrate_mbps = source_mbps
        if should_optimize:
            result.target_bitrate_mbps = config.delivery.target_mbps
    return result


def dry_run(input_path, config=None, log=None, runner=None):
    """Inspect the input and plan the route without writing anything.

    Returns a :class:`ProcessResult` describing the plan.  No output file,
    output directory, temporary file, or encode is ever produced.  FFmpeg/
    FFprobe may be invoked only to report the source video bitrate and
    estimated size; failures degrade gracefully.
    """
    logger = _to_logger(log)
    config = config or load_config()
    pf, codec, label = inspect_input(input_path)
    out_path = resolve_output(input_path, config.output_dir)
    n = pf.video.table.sample_count

    if is_h264(codec):
        mode, preprocessed = MODE_DIRECT, False
    elif is_hevc(codec):
        mode, preprocessed = MODE_HEVC, True
    else:
        raise UnsupportedCodecError(UnsupportedCodecError.message(codec))

    result = ProcessResult(
        codec=codec,
        label=label,
        mode=mode,
        output_path=out_path,
        preprocessed=preprocessed,
        input_samples=n,
        input_chunks=pf.video.table.chunk_count,
        output_samples=n * 10,
        output_chunks=pf.video.table.chunk_count + n * EXTRA_SAMPLE_MULTIPLIER,
    )

    if runner is None:
        try:
            runner = FFmpegRunner()
        except Exception:
            runner = None
    props = _probe_best_effort(runner, input_path)
    if props is not None:
        source_mbps = _mbps(props.get("bit_rate"))
        result.source_bitrate_mbps = source_mbps
        if source_mbps is not None:
            logger.info(f"video bitrate: {_fmt_mbps(source_mbps)}")
            if source_mbps > config.recommended_max_bitrate_mbps:
                logger.success("bitrate optimization would be recommended")
                logger.info(f"target: ~{_fmt_mbps(config.delivery.target_mbps)}")
                result.delivery_encoded = True
                result.target_bitrate_mbps = config.delivery.target_mbps
            else:
                logger.success("within recommended delivery range")
        size_bytes = estimated_video_size(props.get("bit_rate"), props.get("duration"))
        if size_bytes:
            logger.info(f"estimated video size: ~{_format_size(size_bytes)}")
    return result


def process_file(input_path: str, out_path=None, confirm=None, runner=None,
                 log=None, config=None, verbose=False) -> "ProcessResult":
    """Run the full workflow for ``input_path``.

    - H.264/AVC inputs are transformed directly; when the video bitrate is
      above the recommended delivery range (or optimization is explicitly
      requested), exactly one delivery encode runs first.
    - HEVC/H.265 inputs are routed through the optional conversion prompt;
      bitrate optimization, when chosen, is folded into that single encode.
    - anything else raises :class:`UnsupportedCodecError`.

    ``log`` may be a :class:`Logger` or a ``line -> None`` callable.  With
    ``verbose=True`` additional diagnostic lines (metadata, FFmpeg versions,
    workspace path, per-check verification) are logged.
    """
    logger = _to_logger(log)
    config = config or load_config()
    pf, codec, label = inspect_input(input_path)
    out_path = out_path or resolve_output(input_path, config.output_dir)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    v = pf.video
    logger.info(
        f"source/codec/video: {input_path} | {label} | "
        f"{v.width}x{v.height} @ {_display_fps(v.table.sample_count, v.timescale, v.duration)} | "
        f"samples: {v.table.sample_count} | chunks: {v.table.chunk_count} | "
        f"audio: {len(pf.audio_tracks)} track(s)"
    )

    if verbose:
        logger.dim(f"codec: {label}")
        logger.dim(f"resolution: {v.width}x{v.height}")
        logger.dim(
            f"frame rate: {_display_fps(v.table.sample_count, v.timescale, v.duration)}"
        )
        logger.dim(f"timescale: {v.timescale}")
        dur_seconds = (v.duration / v.timescale) if v.timescale else 0
        logger.dim(f"duration: {_format_duration(dur_seconds)}")
        logger.dim(f"samples: {v.table.sample_count}")
        logger.dim(f"chunks: {v.table.chunk_count}")
        logger.dim(f"output directory: {config.output_dir}")
        logger.dim(f"output path: {out_path}")

    if is_h264(codec):
        return _process_h264(pf, out_path, confirm, runner, logger, config, verbose=verbose)

    if is_hevc(codec):
        return _process_hevc(pf, out_path, confirm, runner, logger, label, config, verbose=verbose)

    raise UnsupportedCodecError(UnsupportedCodecError.message(codec))