"""CLI polish: --help, --verbose, --dry-run, clean errors."""

import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "tk-cli.py")


def _run_cli(args, stdin=None):
    return subprocess.run(
        [sys.executable, CLI] + args,
        capture_output=True,
        text=True,
        input=stdin,
    )


def _copy_fixture(fixture, tmp_path, name="copy.mp4"):
    dest = str(tmp_path / name)
    shutil.copyfile(fixture, dest)
    return dest


def test_cli_help_lists_options():
    proc = _run_cli(["--help"])
    assert proc.returncode == 0
    for token in (
        "MP4 upload optimization",
        "input MP4 file",
        "--output-dir DIR",
        "--verbose",
        "--dry-run",
        "--optimize-bitrate",
        "--no-optimize-bitrate",
        "--target-bitrate MBPS",
        "--crf N",
        "--preset NAME",
        "TK_H264_ENCODER",
        "TK_OUTPUT_DIR",
        "TK_DELIVERY_TARGET_MBPS",
        "TK_RECOMMENDED_MAX_BITRATE_MBPS",
    ):
        assert token in proc.stdout


def test_cli_verbose_h264(h264_fixture, tmp_path):
    copy = _copy_fixture(h264_fixture, tmp_path)
    proc = _run_cli([copy, "--output-dir", str(tmp_path), "--verbose"])
    assert proc.returncode == 0
    assert "[+] completed successfully" in proc.stdout
    assert "[#] codec: H.264/AVC" in proc.stdout
    assert "[#] resolution:" in proc.stdout
    assert "[#] timescale:" in proc.stdout
    assert "[#] duration:" in proc.stdout
    assert "[#] samples:" in proc.stdout
    assert "[#] chunks:" in proc.stdout
    assert "[#] check:" in proc.stdout
    assert os.path.isfile(str(tmp_path / "copy-optimized.mp4"))


def test_cli_dry_run_h264_creates_no_files(h264_fixture, tmp_path):
    copy = _copy_fixture(h264_fixture, tmp_path)
    before = open(copy, "rb").read()
    proc = _run_cli([copy, "--output-dir", str(tmp_path), "--dry-run"])
    assert proc.returncode == 0
    assert "[+] codec: H.264/AVC" in proc.stdout
    assert "[+] route: direct MP4 optimization" in proc.stdout
    assert "[+] samples:" in proc.stdout
    assert "[+] chunks:" in proc.stdout
    assert "[+] dry-run: no files written" in proc.stdout
    # only the copied input exists; no output and no temp files
    assert sorted(os.listdir(str(tmp_path))) == ["copy.mp4"]
    assert open(copy, "rb").read() == before


def test_cli_dry_run_hevc_creates_no_files(hevc_fixture, tmp_path):
    copy = _copy_fixture(hevc_fixture, tmp_path)
    proc = _run_cli([copy, "--output-dir", str(tmp_path), "--dry-run"])
    assert proc.returncode == 0
    assert "[+] codec: HEVC/H.265" in proc.stdout
    assert "[+] route: HEVC -> H.264 -> MP4 optimization" in proc.stdout
    assert "[+] encoder: libx264" in proc.stdout
    assert "[+] preset: slow" in proc.stdout
    assert "[+] CRF: 18" in proc.stdout
    assert "[+] ffmpeg: available" in proc.stdout
    assert "[+] dry-run: no files written" in proc.stdout
    assert sorted(os.listdir(str(tmp_path))) == ["copy.mp4"]


def test_cli_dry_run_no_input_prompt_writes_nothing(h264_fixture, tmp_path, monkeypatch):
    monkeypatch.setenv("TK_OUTPUT_DIR", str(tmp_path))
    copy = _copy_fixture(h264_fixture, tmp_path)
    proc = _run_cli([copy, "--dry-run"])
    assert proc.returncode == 0
    assert "[+] dry-run: no files written" in proc.stdout
    assert sorted(os.listdir(str(tmp_path))) == ["copy.mp4"]


def test_cli_unsupported_codec_message(mpeg4_fixture, tmp_path):
    copy = _copy_fixture(mpeg4_fixture, tmp_path)
    proc = _run_cli([copy, "--output-dir", str(tmp_path)])
    assert proc.returncode != 0
    assert "[!] unsupported video codec" in proc.stdout
    assert "[+] supported codecs: H.264/AVC and HEVC/H.265" in proc.stdout
    assert "Traceback" not in proc.stdout


def test_cli_output_exists_hint(h264_fixture, tmp_path):
    copy = _copy_fixture(h264_fixture, tmp_path)
    with open(str(tmp_path / "copy-optimized.mp4"), "wb") as fh:
        fh.write(b"occupied")
    proc = _run_cli([copy, "--output-dir", str(tmp_path)])
    assert proc.returncode != 0
    assert "[!] output already exists" in proc.stdout
    assert "[+] choose another output directory or remove the existing file" in proc.stdout
    assert "Traceback" not in proc.stdout


def test_cli_missing_file_message(tmp_path):
    proc = _run_cli([str(tmp_path / "nope.mp4")])
    assert proc.returncode != 0
    assert "[!] file not found" in proc.stdout
    assert "[+] check the path and try again" in proc.stdout


def test_cli_error_modes_have_no_traceback(hevc_fixture, tmp_path, monkeypatch):
    copy = _copy_fixture(hevc_fixture, tmp_path)
    monkeypatch.setenv("TK_FFMPEG", str(tmp_path / "missing-ffmpeg"))
    monkeypatch.setenv("TK_FFPROBE", str(tmp_path / "missing-ffprobe"))
    proc = _run_cli([copy, "--output-dir", str(tmp_path)], stdin="y\n")
    assert proc.returncode != 0
    assert "Traceback" not in proc.stdout + proc.stderr


def _load_cli_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("tk_cli", os.path.join(REPO, "tk-cli.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_console_sink_applies_semantic_styles():
    import io
    import re

    from rich.console import Console
    from tk_optimize.ui import Message

    tk_cli = _load_cli_module()
    buffer = io.StringIO()
    original_console = tk_cli.console
    tk_cli.console = Console(file=buffer, force_terminal=True, width=100)
    try:
        tk_cli.console_sink(Message("info", "key: value"))
        tk_cli.console_sink(Message("success", "all good"))
        tk_cli.console_sink(Message("warning", "warn line"))
        tk_cli.console_sink(Message("error", "bad line"))
        tk_cli.console_sink(Message("dim", "detail line"))
        tk_cli.console_sink(Message("question", "ask line"))
    finally:
        tk_cli.console = original_console
    out = buffer.getvalue()
    assert "\x1b[" in out  # semantic ANSI styles applied
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    for token in ("[+]", "[!]", "[#]", "[?]", "key: value", "warn line", "ask line"):
        assert token in plain


def test_console_sink_routes_error_first_line():
    import io

    from rich.console import Console

    tk_cli = _load_cli_module()
    buffer = io.StringIO()
    original_console = tk_cli.console
    tk_cli.console = Console(file=buffer, force_terminal=True, width=100)
    try:
        tk_cli._emit_error("first line\nsecond line")
    finally:
        tk_cli.console = original_console
    out = buffer.getvalue()
    assert "first line" in out
    assert "second line" in out


def test_normalize_input_path_strips_quotes_and_maps_windows_drive():
    tk_cli = _load_cli_module()
    if os.name == "nt":
        assert tk_cli._normalize_input_path('"C:\\foo\\bar.mp4"') == r"C:\foo\bar.mp4"
        assert tk_cli._normalize_input_path("'C:/foo/bar.mp4'") == r"C:/foo/bar.mp4"
    else:
        assert tk_cli._normalize_input_path('"F:\\TopazVideo\\clip.mp4"') == "/mnt/f/TopazVideo/clip.mp4"
        assert tk_cli._normalize_input_path("'F:/TopazVideo/clip.mp4'") == "/mnt/f/TopazVideo/clip.mp4"
        assert tk_cli._normalize_input_path("F:\\TopazVideo\\clip.mp4") == "/mnt/f/TopazVideo/clip.mp4"
        assert tk_cli._normalize_input_path("/mnt/f/x.mp4") == "/mnt/f/x.mp4"
    assert tk_cli._normalize_input_path("") == ""
    assert tk_cli._normalize_input_path("   ") == ""
    assert tk_cli._normalize_input_path('""') == ""


def test_cli_missing_file_reports_normalized_path():
    if os.name == "nt" or not os.path.isdir("/mnt/f"):
        pytest.skip("requires the POSIX /mnt/f mount")
    proc = _run_cli([r'"F:\no\such\file.mp4"'])
    assert proc.returncode != 0
    assert "[!] file not found: /mnt/f/no/such/file.mp4" in proc.stdout
    assert "[+] check the path and try again" in proc.stdout


@pytest.mark.skipif(
    os.name == "nt" or not os.path.isdir("/mnt/f"),
    reason="requires the POSIX /mnt/f mount",
)
def test_cli_accepts_quoted_windows_path(h264_fixture, tmp_path):
    relative = os.path.relpath(h264_fixture, "/mnt/f")
    win_path = "F:" + os.sep + relative.replace("/", "\\")
    proc = _run_cli(
        [f'"{win_path}"', "--output-dir", str(tmp_path), "--dry-run"]
    )
    assert proc.returncode == 0
    assert "[+] codec: H.264/AVC" in proc.stdout
    assert "[+] dry-run: no files written" in proc.stdout


@pytest.mark.skipif(
    os.name == "nt" or not os.path.isdir("/mnt/f"),
    reason="requires the POSIX /mnt/f mount",
)
def test_cli_prompt_accepts_quoted_windows_path(h264_fixture, tmp_path):
    relative = os.path.relpath(h264_fixture, "/mnt/f")
    win_path = "F:" + os.sep + relative.replace("/", "\\")
    proc = _run_cli(
        ["--dry-run", "--output-dir", str(tmp_path)], stdin=f'"{win_path}"\n'
    )
    assert proc.returncode == 0
    assert "[+] dry-run: no files written" in proc.stdout


def test_cli_sink_question_renders_inline():
    import io
    import re as re_mod

    from rich.console import Console
    from tk_optimize.ui import Message

    tk_cli = _load_cli_module()
    buffer = io.StringIO()
    original_console = tk_cli.console
    console = Console(file=buffer, force_terminal=True, width=100)
    tk_cli.console = console
    try:
        sink = tk_cli._CLISink(console)
        sink(Message("question", "optimize video bitrate? [Y/n]:"))
    finally:
        tk_cli.console = original_console
    plain = re_mod.sub(r"\x1b\[[0-9;]*m", "", buffer.getvalue())
    assert plain.endswith("[?] optimize video bitrate? [Y/n]: ")
    assert not plain.rstrip(" ").endswith("\n")


def test_cli_sink_shows_and_stops_spinner_for_long_ops():
    import io

    from rich.console import Console
    from tk_optimize.ui import Message

    tk_cli = _load_cli_module()
    buffer = io.StringIO()
    original_console = tk_cli.console
    console = Console(file=buffer, force_terminal=True, width=100)
    tk_cli.console = console
    try:
        sink = tk_cli._CLISink(console)
        sink(Message("info", "optimizing video bitrate"))
        assert sink._status is not None
        sink(Message("success", "bitrate optimization complete"))
        assert sink._status is None
        sink(Message("info", "transforming..."))
        assert sink._status is not None
        sink.close()
        assert sink._status is None
    finally:
        tk_cli.console = original_console
    assert "\x1b[" in buffer.getvalue()


def test_cli_sink_disabled_outside_terminal():
    import io

    from rich.console import Console
    from tk_optimize.ui import Message

    tk_cli = _load_cli_module()
    buffer = io.StringIO()
    original_console = tk_cli.console
    console = Console(file=buffer)
    tk_cli.console = console
    try:
        sink = tk_cli._CLISink(console)
        sink(Message("info", "optimizing video bitrate"))
        assert sink._status is None
        sink(Message("question", "optimize video bitrate? [Y/n]:"))
    finally:
        tk_cli.console = original_console
    plain = buffer.getvalue()
    assert "[+] optimizing video bitrate" in plain
    assert plain.rstrip("\n").endswith("[?] optimize video bitrate? [Y/n]:")