"""Configuration resolution: CLI flags beat env vars beat defaults."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tk_optimize import (
    DEFAULT_H264_ENCODE,
    Config,
    ConfigError,
    DeliveryConfig,
    load_config,
)


def _clear_env(monkeypatch):
    for name in (
        "TK_OUTPUT_DIR",
        "TK_H264_ENCODER",
        "TK_H264_PRESET",
        "TK_H264_CRF",
        "TK_H264_PIX_FMT",
        "TK_DELIVERY_TARGET_MBPS",
        "TK_DELIVERY_MAXRATE_MBPS",
        "TK_DELIVERY_BUFSIZE_MBPS",
        "TK_DELIVERY_CRF",
        "TK_DELIVERY_PRESET",
        "TK_RECOMMENDED_MIN_BITRATE_MBPS",
        "TK_RECOMMENDED_MAX_BITRATE_MBPS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults(monkeypatch):
    _clear_env(monkeypatch)
    cfg = load_config()
    assert cfg.output_dir == "output"
    assert cfg.h264_encode == DEFAULT_H264_ENCODE


def test_env_vars_override_defaults(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TK_OUTPUT_DIR", "custom-out")
    monkeypatch.setenv("TK_H264_ENCODER", "libx264")
    monkeypatch.setenv("TK_H264_PRESET", "medium")
    monkeypatch.setenv("TK_H264_CRF", "20")
    monkeypatch.setenv("TK_H264_PIX_FMT", "yuv420p")
    cfg = load_config()
    assert cfg.output_dir == "custom-out"
    assert cfg.h264_encode.preset == "medium"
    assert cfg.h264_encode.crf == 20
    assert cfg.h264_encode.pix_fmt == "yuv420p"


def test_cli_flags_beat_env_vars(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TK_OUTPUT_DIR", "env-out")
    monkeypatch.setenv("TK_H264_PRESET", "slow")
    cfg = load_config(cli_output_dir="cli-out", cli_preset="ultrafast", cli_crf=24)
    assert cfg.output_dir == "cli-out"
    assert cfg.h264_encode.preset == "ultrafast"
    assert cfg.h264_encode.crf == 24


def test_invalid_crf_env_raises(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TK_H264_CRF", "not-an-int")
    with pytest.raises(ConfigError, match="TK_H264_CRF must be an integer"):
        load_config()


def test_out_of_range_crf_env_raises(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TK_H264_CRF", "99")
    with pytest.raises(ConfigError, match="between 0 and 51"):
        load_config()


def test_empty_preset_env_raises(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TK_H264_PRESET", "  ")
    with pytest.raises(ConfigError, match="preset must be a non-empty string"):
        load_config()


def test_empty_encoder_env_raises(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TK_H264_ENCODER", "")
    with pytest.raises(ConfigError, match="encoder must be a non-empty string"):
        load_config()


def test_config_defaults_to_default_encode():
    assert Config().h264_encode == DEFAULT_H264_ENCODE


def test_delivery_defaults(monkeypatch):
    _clear_env(monkeypatch)
    cfg = load_config()
    assert cfg.delivery.encoder == "libx264"
    assert cfg.delivery.preset == "slow"
    assert cfg.delivery.crf == 18
    assert cfg.delivery.target_mbps == 20.0
    assert cfg.delivery.maxrate_mbps == 20.0
    assert cfg.delivery.bufsize_mbps == 20.0
    assert cfg.recommended_min_bitrate_mbps == 15
    assert cfg.recommended_max_bitrate_mbps == 20
    assert cfg.optimize_bitrate is None


def test_delivery_env_overrides_defaults(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TK_DELIVERY_TARGET_MBPS", "18.5")
    monkeypatch.setenv("TK_DELIVERY_MAXRATE_MBPS", "18")
    monkeypatch.setenv("TK_DELIVERY_BUFSIZE_MBPS", "18")
    monkeypatch.setenv("TK_DELIVERY_CRF", "22")
    monkeypatch.setenv("TK_DELIVERY_PRESET", "medium")
    monkeypatch.setenv("TK_RECOMMENDED_MIN_BITRATE_MBPS", "18")
    monkeypatch.setenv("TK_RECOMMENDED_MAX_BITRATE_MBPS", "25")
    cfg = load_config()
    assert cfg.delivery.target_mbps == 18.5
    assert cfg.delivery.maxrate_mbps == 18.0
    assert cfg.delivery.bufsize_mbps == 18.0
    assert cfg.delivery.crf == 22
    assert cfg.delivery.preset == "medium"
    assert cfg.recommended_min_bitrate_mbps == 18.0
    assert cfg.recommended_max_bitrate_mbps == 25.0


def test_delivery_cli_flags_beat_env(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TK_DELIVERY_CRF", "22")
    cfg = load_config(cli_delivery_crf=20, cli_target_mbps=15.0, cli_optimize_bitrate=True)
    assert cfg.delivery.crf == 20
    assert cfg.delivery.target_mbps == 15.0
    assert cfg.optimize_bitrate is True


def test_delivery_invalid_target_env_raises(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TK_DELIVERY_TARGET_MBPS", "abc")
    with pytest.raises(ConfigError, match="TK_DELIVERY_TARGET_MBPS must be a number"):
        load_config()


def test_delivery_invalid_crf_env_raises(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TK_DELIVERY_CRF", "99")
    with pytest.raises(ConfigError, match="crf must be between 0 and 51"):
        load_config()


def test_delivery_negative_target_rejected():
    with pytest.raises(ValueError, match="target_mbps must be positive"):
        DeliveryConfig(target_mbps=0)


def test_min_threshold_clamped_below_max(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TK_RECOMMENDED_MAX_BITRATE_MBPS", "10")
    monkeypatch.setenv("TK_RECOMMENDED_MIN_BITRATE_MBPS", "20")
    cfg = load_config()
    assert cfg.recommended_max_bitrate_mbps == 10.0
    assert cfg.recommended_min_bitrate_mbps < cfg.recommended_max_bitrate_mbps


def test_delivery_encode_config_matches_profile():
    cfg = DeliveryConfig(crf=20, preset="medium")
    encode = cfg.encode_config()
    assert encode.crf == 20
    assert encode.preset == "medium"
    assert encode.encoder == "libx264"