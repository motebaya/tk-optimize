#!/usr/bin/env python3
"""tk-cli.py — transform an H.264/HEVC MP4 into the optimized structure.

Usage:
    python tk-cli.py [INPUT.mp4] [--method ask|stealth|shadow|trace]
                     [--output FILE | --output-dir DIR] [--verbose] [--dry-run]
                     [--optimize-bitrate|--no-optimize-bitrate]
                     [--target-bitrate MBPS] [--crf N] [--preset NAME]
                     [--audio-bitrate BPS] [--video-bitrate BPS] [--fps R]

Without an input argument the path is prompted for (questionary).  Three
transformation lineages are available behind neutral names:

  stealth  stream-copy lineage — video is never re-encoded; audio is rebuilt
           and the container gets fake-sample surgery.
  shadow   encode-patch lineage — one rate-capped H.264 encode, a stream-copy
           remux, then the container patch.
  trace    legacy uploader workflow — codec-aware, offers HEVC conversion and
           delivery bitrate optimization interactively.

``--dry-run`` inspects and plans without writing files (trace workflow only).
The output is written to FILE, or DIR (default: TK_OUTPUT_DIR or output/).
"""

import argparse
import os
import re
import sys

import questionary
from rich.console import Console, Group
from rich.text import Text
from rich.align import Align
from rich.rule import Rule

import tk_optimize
from tk_optimize import (
    ConfigError,
    FFmpegNotFoundError,
    FFmpegRunner,
    MODE_CANCELLED,
    MODE_DIRECT,
    MODE_HEVC,
    ParseError,
    UnsupportedCodecError,
    WorkflowError,
    dry_run,
    load_config,
    process_file,
)
from tk_optimize.method_patch import DEFAULT_AUDIO_BITRATE as SHADOW_DEFAULT_AUDIO_BITRATE
from tk_optimize.method_patch import DEFAULT_FPS as SHADOW_DEFAULT_FPS
from tk_optimize.method_patch import DEFAULT_VIDEO_BITRATE as SHADOW_DEFAULT_VIDEO_BITRATE
from tk_optimize.method_patch import MethodPatchError
from tk_optimize.method_patch import run as shadow_run
from tk_optimize.method_stream import DEFAULT_AUDIO_BITRATE as STEALTH_DEFAULT_AUDIO_BITRATE
from tk_optimize.method_stream import MethodStreamError
from tk_optimize.method_stream import run as stealth_run
from tk_optimize.ui import Logger, Message
from tk_optimize.workflow import default_output_path

METHOD_CHOICES = ("stealth", "shadow", "trace")
METHOD_HELP = {
    "stealth": "stream-copy lineage (video untouched, audio rebuild, fake-sample surgery)",
    "shadow": "encode-patch lineage (H.264 re-encode, remux, container patch)",
    "trace": "legacy uploader workflow (interactive codec/bitrate handling)",
}

ENV_HELP = """\
Environment overrides (CLI flags and env vars beat defaults):
  TK_OUTPUT_DIR                  output directory
  TK_H264_ENCODER                HEVC->H.264 encoder (default: libx264)
  TK_H264_PRESET                 HEVC->H.264 preset (default: slow)
  TK_H264_CRF                    HEVC->H.264 CRF, 0-51 (default: 16)
  TK_H264_PIX_FMT                HEVC->H.264 pixel format (default: yuv420p)
  TK_DELIVERY_TARGET_MBPS        delivery target bitrate in Mbps (default: 20)
  TK_DELIVERY_MAXRATE_MBPS       delivery maxrate in Mbps (default: 20)
  TK_DELIVERY_BUFSIZE_MBPS       delivery bufsize in Mbps (default: 20)
  TK_DELIVERY_CRF                delivery CRF, 0-51 (default: 18)
  TK_DELIVERY_PRESET             delivery preset (default: slow)
  TK_RECOMMENDED_MIN_BITRATE_MBPS recommended delivery range minimum
  TK_RECOMMENDED_MAX_BITRATE_MBPS recommended delivery range maximum
  TK_BRANDING                    branding config JSON (lineage methods)
  TK_FFMPEG                      explicit ffmpeg path
  TK_FFPROBE                     explicit ffprobe path"""

console = Console()

LEVEL_STYLES = {
    "info": "cyan",
    "success": "green",
    "warning": "yellow",
    "error": "red",
    "dim": "dim",
    "question": "magenta",
}
LEVEL_SYMBOLS = {
    "info": "[+]",
    "success": "[+]",
    "warning": "[!]",
    "error": "[!]",
    "dim": "[#]",
    "question": "[?]",
}

_WINDOWS_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def console_sink(message, end="\n"):
    """Render a Message with semantic colors; highlight values after ``: ``."""
    style = LEVEL_STYLES.get(message.level, "default")
    symbol = LEVEL_SYMBOLS.get(message.level, "[+]")
    text = Text()
    text.append(symbol + " ", style=style)
    if ": " in message.text:
        head, _, tail = message.text.partition(": ")
        text.append(head + ": ", style=style)
        text.append(tail, style=style + " bold")
    else:
        text.append(message.text, style=style)
    console.print(text, end=end)


def _normalize_input_path(path):
    """Return ``path`` normalized for the current platform.

    Surrounding quotes are stripped so a pasted path like ``"F:\\videos\\clip.mp4"``
    works, and on POSIX systems a bare Windows drive path (``F:\\videos\\clip.mp4``)
    is mapped to its ``/mnt`` mount point.  Returns "" when nothing usable remains.
    """
    if not path:
        return ""
    path = path.strip()
    if len(path) >= 2 and path[0] in "\"'" and path[-1] == path[0]:
        path = path[1:-1].strip()
    if not path:
        return ""
    if os.name != "nt":
        match = _WINDOWS_DRIVE_RE.match(path)
        if match:
            drive, rest = match.group(1).lower(), match.group(2).replace("\\", "/")
            return f"/mnt/{drive}/{rest}"
    return path


_LONG_OP_STARTS = (
    "optimizing video bitrate",
    "converting HEVC -> H.264",
    "transforming...",
)
_LONG_OP_ENDS = (
    "bitrate optimization complete",
    "H.264 preprocessing complete",
)


def _spinner_text(text):
    message = text if text.endswith("...") else f"{text}..."
    return f"[bold green]{message}[/bold green]"


class _CLISink:
    """Console sink that shows long operations as a rich spinner.

    Long-operation start/end messages are matched against
    :data:`_LONG_OP_STARTS` / :data:`_LONG_OP_ENDS`; while a spinner is active
    the matching lines are consumed instead of printed.  Question messages are
    rendered inline (no trailing newline) so ``input()`` reads on the same line.
    Outside of a real terminal every message falls back to the plain
    :func:`console_sink` output.
    """

    def __init__(self, console):
        self.console = console
        self._status = None

    def __call__(self, message):
        if message.level == "question":
            if self.console.is_terminal:
                console_sink(message, end=" ")
            else:
                console_sink(message)
            return
        if self.console.is_terminal:
            text = message.text
            for start in _LONG_OP_STARTS:
                if text.startswith(start):
                    self._start_status(text)
                    return
            for end in _LONG_OP_ENDS:
                if text == end:
                    self._stop_status()
                    return
        console_sink(message)

    def _start_status(self, text):
        if self._status is None:
            self._status = self.console.status(_spinner_text(text), spinner="dots")
            self._status.start()

    def _stop_status(self):
        if self._status is not None:
            self._status.stop()
            self._status = None

    def close(self):
        self._stop_status()


def _emit_error(message):
    """Route a plain multi-line error: first line as error, rest as info."""
    lines = str(message).splitlines()
    for index, line in enumerate(lines):
        console_sink(Message(level="error" if index == 0 else "info", text=line))


def _print_failed_checks(checks):
    for check in checks:
        if check.failed:
            detail = f": {check.detail}" if check.detail else ""
            console_sink(Message("error", f"failed check: {check.name}{detail}"))


def _format_size(path):
    size = os.path.getsize(path)
    if size >= 1_000_000_000:
        return f"{size / 1_000_000_000:.1f} GB"
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.1f} KB"
    return f"{size} B"


def _report_ffmpeg_availability():
    try:
        FFmpegRunner().require("ffmpeg")
        console_sink(Message("info", "ffmpeg: available"))
    except FFmpegNotFoundError as exc:
        _emit_error(str(exc))


def _is_interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt_input_path():
    """Ask for the input path via questionary; returns None when not given."""
    try:
        return questionary.text("Input video path:").ask()
    except (KeyboardInterrupt, EOFError):
        return None


def _choose_method(requested):
    """Resolve the transformation lineage to run, prompting when asked."""
    if requested != "ask":
        return requested
    if not _is_interactive():
        console_sink(
            Message("dim", "non-interactive: defaulting to the trace workflow (use --method)")
        )
        return "trace"
    choices = [
        questionary.Choice(f"{name} — {METHOD_HELP[name]}", value=name)
        for name in METHOD_CHOICES
    ]
    try:
        return questionary.select("Transformation method:", choices=choices).ask()
    except (KeyboardInterrupt, EOFError):
        return None


def _confirm_answer():
    """Questionary-backed replacement for the plain ``input()`` confirm."""
    try:
        answer = questionary.text("").ask()
    except (KeyboardInterrupt, EOFError):
        return False
    if answer is None:
        return False
    return answer.strip().lower() not in ("n", "no", "0")


def _piped_confirm():
    """Read one answer line from a piped stdin with the same semantics."""
    line = sys.stdin.readline()
    if not line:
        return False
    return line.strip().lower() not in ("n", "no", "0")


def _confirm_overwrite(output):
    if not _is_interactive():
        console_sink(Message("error", f"output already exists: {output}"))
        console_sink(Message("info", "choose another path with --output or remove the file"))
        return False
    try:
        return bool(
            questionary.confirm(
                f"Output already exists: {output}. Overwrite?"
            ).ask()
        )
    except (KeyboardInterrupt, EOFError):
        return False


def _report_lineage(info, verbose=False):
    console_sink(Message("success", "transformation: complete"))
    console_sink(Message("info", f"output: {info['output']}"))
    console_sink(Message("info", f"size: {_format_size(info['output'])}"))
    console_sink(Message("info", f"sha256: {info['sha256']}"))
    console_sink(Message("info", f"copyright tag: {info['copyright32']}"))
    if verbose:
        logger = Logger(sink=console_sink)
        for key, value in sorted(info.items()):
            if key not in ("output", "sha256", "copyright32", "branding"):
                logger.dim(f"{key}: {value}")


def _run_lineage(method, path, args, config):
    output = args.output or default_output_path(path, config.output_dir)
    out_dir = os.path.dirname(output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(output) and not _confirm_overwrite(output):
        return 1
    logger = Logger(sink=console_sink)
    try:
        if method == "stealth":
            info = stealth_run(
                path,
                output,
                audio_bitrate=args.audio_bitrate or STEALTH_DEFAULT_AUDIO_BITRATE,
                log=logger,
            )
        else:
            info = shadow_run(
                path,
                output,
                video_bitrate=args.video_bitrate or SHADOW_DEFAULT_VIDEO_BITRATE,
                audio_bitrate=args.audio_bitrate or SHADOW_DEFAULT_AUDIO_BITRATE,
                fps=args.fps or SHADOW_DEFAULT_FPS,
                log=logger,
            )
    except (MethodStreamError, MethodPatchError) as exc:
        _emit_error(str(exc))
        return 1
    except FFmpegNotFoundError as exc:
        _emit_error(str(exc))
        return 1
    _report_lineage(info, verbose=args.verbose)
    return 0


def _run_dry_run(path, config, verbose):
    logger = Logger(sink=console_sink)
    try:
        result = dry_run(path, config=config, log=logger)
    except UnsupportedCodecError as exc:
        _emit_error(str(exc))
        return 1
    except ParseError as exc:
        console_sink(Message("error", str(exc)))
        console_sink(
            Message("info", "input must be an MP4 with an H.264/AVC or HEVC/H.265 video track")
        )
        return 1

    if verbose:
        logger.dim(f"samples: {result.input_samples}")
        logger.dim(f"chunks: {result.input_chunks}")

    if result.mode == MODE_DIRECT:
        console_sink(Message("info", "codec: H.264/AVC"))
        if result.delivery_encoded:
            console_sink(Message("info", "route: H.264 -> delivery encode -> MP4 optimization"))
        else:
            console_sink(Message("info", "route: direct MP4 optimization"))
        console_sink(Message("info", f"samples: {result.input_samples}"))
        console_sink(Message("info", f"chunks: {result.input_chunks}"))
    else:
        console_sink(Message("info", "codec: HEVC/H.265"))
        console_sink(Message("info", "route: HEVC -> H.264 -> MP4 optimization"))
        console_sink(Message("info", f"encoder: {config.delivery.encoder}"))
        console_sink(Message("info", f"preset: {config.delivery.preset}"))
        console_sink(Message("info", f"CRF: {config.delivery.crf}"))
        _report_ffmpeg_availability()
    console_sink(Message("info", f"output: {result.output_path}"))
    console_sink(Message("info", "dry-run: no files written"))
    return 0


def _print_summary(result):
    passed = sum(1 for c in result.checks if c.ok)
    if result.mode == MODE_HEVC:
        console_sink(Message("info", "preprocessing: HEVC -> H.264"))
    if result.delivery_encoded:
        console_sink(Message("success", "bitrate optimization: delivery encode applied"))
    console_sink(Message("info", "transformation: complete"))
    console_sink(Message("info", f"verification: {passed}/{len(result.checks)} passed"))
    console_sink(Message("info", f"output: {result.output_path}"))
    console_sink(Message("info", f"size: {_format_size(result.output_path)}"))
    console_sink(Message("success", "completed successfully"))


def _optimize_flag(args):
    if args.optimize_bitrate:
        return True
    if args.no_optimize_bitrate:
        return False
    return None

def print_intro(console: Console) -> None:
    title = Text()
    title.append("tk-optimize", style="bold bright_cyan")
    title.append("  ", style="dim")
    title.append("MP4 upload optimization", style="white")

    meta = Text()
    meta.append("by ", style="dim")
    meta.append("motebaya", style="bold")
    meta.append("  ·  ", style="dim")
    meta.append("08-2026", style="bright_cyan")
    meta.append("  ·  ", style="dim")
    meta.append(f"v{tk_optimize.__version__}", style="dim")

    repo = Text(
        "@github.com/motebaya/tk-optimize",
        style="cyan",
    )

    content = Group(
        Align.left(title),
        Align.left(meta),
        Align.left(repo),
    )

    console.print()
    console.print(
        Align.left(
            content,
            pad=True,
        )
    )

    console.print()

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="tk-cli.py",
        description="MP4 upload optimization",
        epilog=ENV_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", nargs="?", default=None, help="input MP4 file")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {tk_optimize.__version__}",
    )
    parser.add_argument(
        "--method",
        choices=["ask", *METHOD_CHOICES],
        default="ask",
        metavar="NAME",
        help="transformation lineage: "
        "stealth (stream-copy), shadow (encode-patch), trace (legacy workflow); "
        "default: ask (prompt)",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="FILE",
        help="explicit output file for the stealth/shadow lineages",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help="output directory (default: $TK_OUTPUT_DIR or output/)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable diagnostic output",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect and plan without writing files",
    )
    opt_group = parser.add_mutually_exclusive_group()
    opt_group.add_argument(
        "--optimize-bitrate",
        action="store_true",
        help="always apply delivery bitrate optimization",
    )
    opt_group.add_argument(
        "--no-optimize-bitrate",
        action="store_true",
        help="never apply delivery bitrate optimization",
    )
    parser.add_argument(
        "--target-bitrate",
        default=None,
        type=float,
        metavar="MBPS",
        help="delivery target bitrate in Mbps (default: 20)",
    )
    parser.add_argument(
        "--crf",
        default=None,
        type=int,
        metavar="N",
        help="delivery encode CRF, 0-51 (default: 18)",
    )
    parser.add_argument(
        "--preset",
        default=None,
        metavar="NAME",
        help="delivery encode preset (default: slow)",
    )
    parser.add_argument(
        "--audio-bitrate",
        default=None,
        metavar="BPS",
        help="audio bitrate for the stealth/shadow lineages "
        f"(default: {STEALTH_DEFAULT_AUDIO_BITRATE} stealth, "
        f"{SHADOW_DEFAULT_AUDIO_BITRATE} shadow)",
    )
    parser.add_argument(
        "--video-bitrate",
        default=None,
        metavar="BPS",
        help=f"video bitrate for the shadow lineage (default: {SHADOW_DEFAULT_VIDEO_BITRATE})",
    )
    parser.add_argument(
        "--fps",
        default=None,
        metavar="R",
        help=f"video frame-rate fraction for the shadow lineage (default: {SHADOW_DEFAULT_FPS})",
    )
    args = parser.parse_args(argv)

    # console.print("tk-optimize", style="bold")
    # console.print("MP4 upload optimization", style="bold cyan")
    print_intro(console)

    path = args.input
    if path is None:
        if not _is_interactive():
            console_sink(Message("error", "no input path given"))
            console_sink(Message("info", "pass the input MP4 as a positional argument"))
            return 1
        path = _prompt_input_path()
        if not path:
            console_sink(Message("error", "no input path given"))
            return 1
    path = _normalize_input_path(path)
    if not path:
        console_sink(Message("error", "no input path given"))
        return 1

    if not os.path.isfile(path):
        console_sink(Message("error", f"file not found: {path}"))
        console_sink(Message("info", "check the path and try again"))
        return 1

    try:
        config = load_config(
            cli_output_dir=args.output_dir,
            cli_optimize_bitrate=_optimize_flag(args),
            cli_target_mbps=args.target_bitrate,
            cli_delivery_crf=args.crf,
            cli_delivery_preset=args.preset,
        )
    except ConfigError as exc:
        console_sink(Message("error", str(exc)))
        return 1

    method = _choose_method(args.method)
    if method is None:
        return 1

    if args.dry_run and method == "trace":
        return _run_dry_run(path, config, args.verbose)
    if args.dry_run:
        console_sink(
            Message(
                "warning",
                f"--dry-run is only implemented for the trace workflow (got --method {method})",
            )
        )
        console_sink(Message("info", "no files written"))
        return 1
    if method != "trace":
        return _run_lineage(method, path, args, config)

    sink = _CLISink(console)
    confirm = _confirm_answer if _is_interactive() else _piped_confirm
    try:
        result = process_file(
            path,
            log=Logger(sink=sink),
            config=config,
            verbose=args.verbose,
            confirm=confirm,
        )
    except UnsupportedCodecError as exc:
        _emit_error(str(exc))
        return 1
    except FFmpegNotFoundError as exc:
        _emit_error(str(exc))
        return 1
    except ParseError as exc:
        console_sink(Message("error", str(exc)))
        console_sink(
            Message("info", "input must be an MP4 with an H.264/AVC or HEVC/H.265 video track")
        )
        return 1
    except WorkflowError as exc:
        console_sink(Message("error", str(exc)))
        if "output already exists" in str(exc):
            console_sink(Message("info", "choose another output directory or remove the existing file"))
        if exc.intermediate_path:
            console_sink(Message("warning", f"intermediate file kept at: {exc.intermediate_path}"))
            console_sink(Message("info", "delete it when it is no longer needed"))
        if exc.checks:
            _print_failed_checks(exc.checks)
        return 1
    except ConfigError as exc:
        console_sink(Message("error", str(exc)))
        return 1
    except Exception as exc:
        console_sink(Message("error", f"unexpected error: {exc}"))
        if args.verbose:
            raise
        return 1
    finally:
        sink.close()

    if result.mode == MODE_CANCELLED:
        return 0

    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))