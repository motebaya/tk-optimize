"""tk-optimize: MP4 upload optimization.

Supports H.264/AVC inputs directly and HEVC/H.265 inputs via an optional
FFmpeg preprocessing step (see tk_optimize.workflow).  A delivery bitrate-
optimization encode (rate-capped H.264) can optionally run first when the
source video bitrate exceeds the recommended range.  User preferences are
resolved by tk_optimize.config and never affect the MP4 transformation
(see tk_optimize.transformer).  Library code emits semantic Messages
(tk_optimize.ui) and never depends on console libraries like Rich.
"""

from .config import (
    DEFAULT_DELIVERY,
    RECOMMENDED_MAX_VIDEO_BITRATE_MBPS,
    RECOMMENDED_MIN_VIDEO_BITRATE_MBPS,
    Config,
    ConfigError,
    DeliveryConfig,
    load_config,
)
from .parser import (
    H264_ENTRY_TYPES,
    HEVC_ENTRY_TYPES,
    ParseError,
    ParsedFile,
    codec_label,
    is_h264,
    is_hevc,
    is_supported_input_codec,
    is_transformable,
    parse_file,
)
from .transformer import TransformError, transform_bytes, transform_file
from .verifier import Check, verify_file
from .ffmpeg import FFmpegError, FFmpegNotFoundError, FFmpegRunner
from .preprocess import (
    DEFAULT_H264_ENCODE,
    H264EncodeConfig,
    delivery_encode,
    estimated_video_size,
    hevc_to_h264,
    probe_video,
    verify_delivery,
)
from .ui import (
    LEVELS,
    Logger,
    Message,
    render,
    render_all,
)
from .workflow import (
    MODE_CANCELLED,
    MODE_DIRECT,
    MODE_HEVC,
    ProcessResult,
    UnsupportedCodecError,
    WorkflowError,
    dry_run,
    process_file,
)

__version__ = "0.7.0"

__all__ = [
    "Config",
    "ConfigError",
    "DeliveryConfig",
    "DEFAULT_DELIVERY",
    "RECOMMENDED_MIN_VIDEO_BITRATE_MBPS",
    "RECOMMENDED_MAX_VIDEO_BITRATE_MBPS",
    "ParseError",
    "ParsedFile",
    "TransformError",
    "Check",
    "FFmpegError",
    "FFmpegNotFoundError",
    "FFmpegRunner",
    "ProcessResult",
    "UnsupportedCodecError",
    "WorkflowError",
    "H264EncodeConfig",
    "DEFAULT_H264_ENCODE",
    "H264_ENTRY_TYPES",
    "HEVC_ENTRY_TYPES",
    "MODE_DIRECT",
    "MODE_HEVC",
    "MODE_CANCELLED",
    "Logger",
    "Message",
    "LEVELS",
    "load_config",
    "parse_file",
    "transform_bytes",
    "transform_file",
    "verify_file",
    "hevc_to_h264",
    "delivery_encode",
    "estimated_video_size",
    "verify_delivery",
    "probe_video",
    "is_h264",
    "is_hevc",
    "is_supported_input_codec",
    "is_transformable",
    "codec_label",
    "render",
    "render_all",
    "dry_run",
    "process_file",
    "__version__",
]