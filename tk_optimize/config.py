"""User-configurable preferences.

Separated from the algorithm's immutable invariants (which live in
``tk_optimize.transformer``): only user preferences live here -- the output
directory, the HEVC -> H.264 encoding quality, and the optional delivery
bitrate-optimization profile.  CLI flags take precedence over environment
variables, which take precedence over built-in defaults:

    CLI option > TK_* environment variable > default
"""

import os
from dataclasses import dataclass

from .preprocess import DEFAULT_H264_ENCODE, H264EncodeConfig

RECOMMENDED_MIN_VIDEO_BITRATE_MBPS = 15
RECOMMENDED_MAX_VIDEO_BITRATE_MBPS = 20


class ConfigError(Exception):
    """Raised when configuration is invalid."""


@dataclass(frozen=True)
class DeliveryConfig:
    """Immutable delivery bitrate-optimization encode preferences.

    ``target_mbps`` is the recommended target; the actual rate control is
    ``-crf N -maxrate XM -bufsize YM`` (no blind ``-b:v``).
    """

    encoder: str = "libx264"
    preset: str = "slow"
    crf: int = 18
    pix_fmt: str = "yuv420p"
    target_mbps: float = 20.0
    maxrate_mbps: float = 20.0
    bufsize_mbps: float = 20.0

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
        for name in ("target_mbps", "maxrate_mbps", "bufsize_mbps"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            if value <= 0:
                raise ValueError(f"{name} must be positive")

    def encode_config(self) -> H264EncodeConfig:
        """The equivalent H.264 encode config for this profile."""
        return H264EncodeConfig(
            encoder=self.encoder,
            preset=self.preset,
            crf=self.crf,
            pix_fmt=self.pix_fmt,
        )


DEFAULT_DELIVERY = DeliveryConfig()


class Config:
    """Resolved user configuration."""

    __slots__ = (
        "output_dir",
        "h264_encode",
        "delivery",
        "recommended_min_bitrate_mbps",
        "recommended_max_bitrate_mbps",
        "optimize_bitrate",
    )

    def __init__(
        self,
        output_dir="output",
        h264_encode=None,
        delivery=None,
        recommended_min_bitrate_mbps=RECOMMENDED_MIN_VIDEO_BITRATE_MBPS,
        recommended_max_bitrate_mbps=RECOMMENDED_MAX_VIDEO_BITRATE_MBPS,
        optimize_bitrate=None,
    ):
        self.output_dir = output_dir
        self.h264_encode = h264_encode or DEFAULT_H264_ENCODE
        self.delivery = delivery or DEFAULT_DELIVERY
        self.recommended_min_bitrate_mbps = recommended_min_bitrate_mbps
        self.recommended_max_bitrate_mbps = recommended_max_bitrate_mbps
        self.optimize_bitrate = optimize_bitrate  # None=ask, True=always, False=never


def _env_value(name):
    """Return the env value; None when unset, otherwise the raw string."""
    return os.environ.get(name)


def _env_crf():
    raw = _env_value("TK_H264_CRF")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"TK_H264_CRF must be an integer, got {raw!r}") from exc


def _env_delivery_crf():
    raw = _env_value("TK_DELIVERY_CRF")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"TK_DELIVERY_CRF must be an integer, got {raw!r}") from exc


def _env_float(name):
    raw = _env_value(name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {raw!r}")
    return value


def load_config(
    cli_output_dir=None,
    cli_encoder=None,
    cli_preset=None,
    cli_crf=None,
    cli_pix_fmt=None,
    cli_optimize_bitrate=None,
    cli_target_mbps=None,
    cli_delivery_crf=None,
    cli_delivery_preset=None,
):
    """Resolve configuration.  CLI flags beat env vars, which beat defaults."""
    output_dir = cli_output_dir or _env_value("TK_OUTPUT_DIR") or "output"

    encoder = cli_encoder if cli_encoder is not None else _env_value("TK_H264_ENCODER")
    if encoder is None:
        encoder = DEFAULT_H264_ENCODE.encoder
    preset = cli_preset if cli_preset is not None else _env_value("TK_H264_PRESET")
    if preset is None:
        preset = DEFAULT_H264_ENCODE.preset
    pix_fmt = cli_pix_fmt if cli_pix_fmt is not None else _env_value("TK_H264_PIX_FMT")
    if pix_fmt is None:
        pix_fmt = DEFAULT_H264_ENCODE.pix_fmt
    crf = cli_crf if cli_crf is not None else _env_crf()
    if crf is None:
        crf = DEFAULT_H264_ENCODE.crf

    try:
        h264_encode = H264EncodeConfig(
            encoder=encoder, preset=preset, crf=crf, pix_fmt=pix_fmt
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    delivery_preset = (
        cli_delivery_preset
        if cli_delivery_preset is not None
        else _env_value("TK_DELIVERY_PRESET")
    )
    if delivery_preset is None:
        delivery_preset = DEFAULT_DELIVERY.preset
    delivery_crf = (
        cli_delivery_crf if cli_delivery_crf is not None else _env_delivery_crf()
    )
    if delivery_crf is None:
        delivery_crf = DEFAULT_DELIVERY.crf
    target_mbps = (
        cli_target_mbps if cli_target_mbps is not None else _env_float("TK_DELIVERY_TARGET_MBPS")
    )
    if target_mbps is None:
        target_mbps = DEFAULT_DELIVERY.target_mbps
    maxrate_mbps = _env_float("TK_DELIVERY_MAXRATE_MBPS")
    if maxrate_mbps is None:
        maxrate_mbps = DEFAULT_DELIVERY.maxrate_mbps
    bufsize_mbps = _env_float("TK_DELIVERY_BUFSIZE_MBPS")
    if bufsize_mbps is None:
        bufsize_mbps = DEFAULT_DELIVERY.bufsize_mbps

    try:
        delivery = DeliveryConfig(
            encoder=DEFAULT_DELIVERY.encoder,
            preset=delivery_preset,
            crf=delivery_crf,
            pix_fmt=DEFAULT_DELIVERY.pix_fmt,
            target_mbps=target_mbps,
            maxrate_mbps=maxrate_mbps,
            bufsize_mbps=bufsize_mbps,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    min_mbps = _env_float("TK_RECOMMENDED_MIN_BITRATE_MBPS")
    if min_mbps is None:
        min_mbps = RECOMMENDED_MIN_VIDEO_BITRATE_MBPS
    max_mbps = _env_float("TK_RECOMMENDED_MAX_BITRATE_MBPS")
    if max_mbps is None:
        max_mbps = RECOMMENDED_MAX_VIDEO_BITRATE_MBPS
    if min_mbps >= max_mbps:
        min_mbps = max_mbps - 1.0

    return Config(
        output_dir=output_dir,
        h264_encode=h264_encode,
        delivery=delivery,
        recommended_min_bitrate_mbps=min_mbps,
        recommended_max_bitrate_mbps=max_mbps,
        optimize_bitrate=cli_optimize_bitrate,
    )