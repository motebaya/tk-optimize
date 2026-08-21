"""FFmpeg/FFprobe discovery and runner behaviour."""

import os
from types import SimpleNamespace

import pytest

from tk_optimize import ffmpeg as fmod


def _touch(path):
    path.write_bytes(b"#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def test_env_override_wins_over_path(monkeypatch, tmp_path):
    exe = _touch(tmp_path / "ffmpeg")
    monkeypatch.setenv("TK_FFMPEG", exe)
    monkeypatch.setattr(fmod.shutil, "which", lambda *a, **k: "/fake/ffmpeg")
    assert fmod.find_executable("ffmpeg", "TK_FFMPEG") == exe


def test_env_override_invalid_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("TK_FFMPEG", str(tmp_path / "missing"))
    monkeypatch.setattr(fmod.shutil, "which", lambda *a, **k: "/fake/ffmpeg")
    with pytest.raises(fmod.FFmpegNotFoundError, match="TK_FFMPEG set but does not exist"):
        fmod.find_executable("ffmpeg", "TK_FFMPEG")


def test_path_discovery_when_available(monkeypatch):
    monkeypatch.delenv("TK_FFMPEG", raising=False)
    monkeypatch.setattr(fmod.shutil, "which", lambda name, **k: f"/fake/bin/{name}")
    assert fmod.find_executable("ffmpeg", "TK_FFMPEG") == "/fake/bin/ffmpeg"


def test_missing_returns_none(monkeypatch):
    monkeypatch.delenv("TK_FFMPEG", raising=False)
    monkeypatch.setattr(fmod.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(fmod, "_platform_candidate_dirs", lambda win: [])
    assert fmod.find_executable("ffmpeg", "TK_FFMPEG") is None


def test_missing_message_is_actionable():
    msg = fmod.not_found_message("ffmpeg")
    assert "ffmpeg not found." in msg
    assert "Set TK_FFMPEG or add ffmpeg to PATH." in msg


def test_windows_candidates():
    candidates = fmod._candidate_paths("ffmpeg", win=True)
    assert all(str(c).endswith("ffmpeg.exe") for c in candidates)
    scoop = str(fmod.Path.home() / "scoop" / "shims" / "ffmpeg.exe")
    assert scoop in {str(c) for c in candidates}


def test_windows_missing_returns_none(monkeypatch):
    monkeypatch.delenv("TK_FFMPEG", raising=False)
    monkeypatch.setattr(fmod.shutil, "which", lambda *a, **k: None)
    assert fmod.find_executable("ffmpeg", "TK_FFMPEG", win=True) is None


def test_linux_candidates():
    candidates = fmod._candidate_paths("ffmpeg", win=False)
    assert str(candidates[0]) == "/usr/local/bin/ffmpeg"
    assert "/usr/bin/ffmpeg" in {str(c) for c in candidates}
    assert not any(str(c).endswith(".exe") for c in candidates)


def test_runner_constructs_without_requiring_binaries(monkeypatch):
    monkeypatch.delenv("TK_FFMPEG", raising=False)
    monkeypatch.delenv("TK_FFPROBE", raising=False)
    monkeypatch.setattr(fmod.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(fmod, "_platform_candidate_dirs", lambda win: [])
    runner = fmod.FFmpegRunner()
    assert runner.ffmpeg_path is None and runner.ffprobe_path is None
    with pytest.raises(fmod.FFmpegNotFoundError):
        runner.require("ffmpeg")


def test_runner_uses_argument_list_not_shell(monkeypatch):
    recorded = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        recorded["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="out", stderr="")

    monkeypatch.setattr(fmod.subprocess, "run", fake_run)
    runner = fmod.FFmpegRunner(ffmpeg="/bin/ffmpeg", ffprobe="/bin/ffprobe")
    result = runner.ffmpeg(["-i", "x.mp4"])
    assert result.ok and result.stdout == "out"
    assert recorded["cmd"] == ["/bin/ffmpeg", "-i", "x.mp4"]
    assert recorded["kwargs"].get("shell") is not True


def test_runner_reports_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom\nerror line")

    monkeypatch.setattr(fmod.subprocess, "run", fake_run)
    runner = fmod.FFmpegRunner(ffmpeg="/bin/ffmpeg", ffprobe="/bin/ffprobe")
    result = runner.ffmpeg(["-i", "x"])
    assert not result.ok
    assert result.returncode == 1
    assert "error line" in result.stderr_tail


def test_runner_launch_failure_is_structured(monkeypatch):
    monkeypatch.setattr(
        fmod.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no such file")),
    )
    runner = fmod.FFmpegRunner(ffmpeg="/nonexistent/ffmpeg", ffprobe="/nonexistent/ffprobe")
    result = runner.ffmpeg([])
    assert not result.ok
    assert "failed to launch" in result.stderr


def test_version_discovery(monkeypatch, tmp_path):
    exe = tmp_path / "ffmpeg"
    exe.write_text(
        "#!/bin/sh\n"
        "echo 'ffmpeg version 7.0.2-static Copyright (c) the FFmpeg developers'\n"
    )
    exe.chmod(0o755)
    runner = fmod.FFmpegRunner(ffmpeg=str(exe), ffprobe=None)
    assert runner.version("ffmpeg") == "7.0.2"


def test_version_cached_for_runner_lifetime(monkeypatch, tmp_path):
    counter = tmp_path / "calls.txt"
    exe = tmp_path / "ffmpeg"
    exe.write_text(
        "#!/bin/sh\n"
        f"echo x >> {counter}\n"
        "echo 'ffmpeg version 7.0.2'\n"
    )
    exe.chmod(0o755)
    runner = fmod.FFmpegRunner(ffmpeg=str(exe), ffprobe=None)
    assert runner.version("ffmpeg") == "7.0.2"
    assert runner.version("ffmpeg") == "7.0.2"
    assert runner.version("ffmpeg") == "7.0.2"
    assert counter.read_text().count("x") == 1


def test_version_missing_binary_returns_none(monkeypatch):
    monkeypatch.delenv("TK_FFMPEG", raising=False)
    monkeypatch.delenv("TK_FFPROBE", raising=False)
    monkeypatch.setattr(fmod.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(fmod, "_platform_candidate_dirs", lambda win: [])
    runner = fmod.FFmpegRunner()
    assert runner.version("ffmpeg") is None
    assert runner.version("ffprobe") is None


def test_version_unrunnable_returns_none(monkeypatch, tmp_path):
    exe = tmp_path / "ffmpeg"
    exe.write_text("#!/bin/sh\nexit 1\n")
    exe.chmod(0o755)
    runner = fmod.FFmpegRunner(ffmpeg=str(exe), ffprobe=None)
    assert runner.version("ffmpeg") is None