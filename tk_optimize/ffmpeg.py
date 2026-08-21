"""Portable FFmpeg/FFprobe discovery and safe invocation.

Discovery order for each executable:

1. explicit environment override (``TK_FFMPEG`` / ``TK_FFPROBE``)
2. ``PATH`` lookup via :func:`shutil.which`
3. platform-specific fallback directories

All commands are run as argument lists with ``shell=False``.  Result
information is returned as structured :class:`RunResult` objects.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FFMPEG_ENV = "TK_FFMPEG"
FFPROBE_ENV = "TK_FFPROBE"

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")


class FFmpegNotFoundError(Exception):
    """Raised when an FFmpeg/FFprobe executable cannot be located."""


class FFmpegError(Exception):
    """Raised when an FFmpeg/FFprobe invocation fails or returns junk."""


def not_found_message(program):
    """Return the plain, actionable message for a missing binary."""
    env = FFMPEG_ENV if program == "ffmpeg" else FFPROBE_ENV
    return f"{program} not found.\nSet {env} or add {program} to PATH."


def _platform_candidate_dirs(win):
    """Common install locations, checked last."""
    if win:
        return [
            Path.home() / "scoop" / "shims",
            Path(os.environ.get("LOCALAPPDATA", "C:\\Users\\Default"))
            / "Microsoft" / "WinGet" / "Links",
            Path("C:\\Program Files\\ffmpeg\\bin"),
            Path("C:\\Program Files (x86)\\ffmpeg\\bin"),
            Path("C:\\ProgramData\\chocolatey\\bin"),
            Path("C:\\ffmpeg\\bin"),
        ]
    return [
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/opt/ffmpeg/bin"),
        Path("/snap/bin"),
    ]


def _candidate_paths(program, win):
    exe = program + (".exe" if win else "")
    return [d / exe for d in _platform_candidate_dirs(win)]


def find_executable(program, env_var, win=None):
    """Locate ``program``. Returns an absolute path string or ``None``.

    ``win`` forces Windows-style discovery (used by tests); when ``None`` it
    is derived from the running platform.
    """
    if win is None:
        win = os.name == "nt"

    override = os.environ.get(env_var)
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            return str(path)
        raise FFmpegNotFoundError(f"{env_var} set but does not exist: {override}")

    found = shutil.which(program)
    if found:
        return found

    for candidate in _candidate_paths(program, win):
        if candidate.is_file():
            return str(candidate)
    return None


@dataclass
class RunResult:
    """Structured outcome of a subprocess invocation."""

    ok: bool
    returncode: int
    stdout: str
    stderr: str
    cmd: list

    @property
    def stderr_tail(self):
        """Last few non-empty stderr lines, for concise error reports."""
        lines = [ln for ln in self.stderr.splitlines() if ln.strip()]
        return "\n".join(lines[-8:])

    def __bool__(self):
        return self.ok


def _query_version(program, path):
    """Run ``<program> -version`` once and extract ``x.y.z``, or None."""
    try:
        proc = subprocess.run(
            [path, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines()[:3]:
        if line.lstrip().startswith(program):
            match = _VERSION_RE.search(line)
            if match:
                return match.group(0)
    return None


class FFmpegRunner:
    """Resolves and safely runs FFmpeg/FFprobe.

    The runner is lazy: executables are only required when a matching
    ``ffmpeg()`` / ``ffprobe()`` call (or an explicit :meth:`require`)
    happens.  Constructing a runner for an H.264-only pipeline never fails.
    """

    def __init__(self, ffmpeg=None, ffprobe=None):
        self.ffmpeg_path = (
            ffmpeg if ffmpeg is not None else find_executable("ffmpeg", FFMPEG_ENV)
        )
        self.ffprobe_path = (
            ffprobe if ffprobe is not None else find_executable("ffprobe", FFPROBE_ENV)
        )
        self._versions = {}

    def require(self, program):
        """Return the resolved path for ``program`` or raise."""
        path = getattr(self, program + "_path")
        if path:
            return path
        raise FFmpegNotFoundError(not_found_message(program))

    def version(self, program):
        """Return the detected version string (e.g. ``"7.0.2"``) or None.

        Best-effort: runs ``<program> -version`` once and caches the result
        for the lifetime of the runner, so version checks are never repeated.
        Returns None when the binary is missing or the query fails; it never
        raises.
        """
        if program not in self._versions:
            path = getattr(self, program + "_path")
            self._versions[program] = _query_version(program, path) if path else None
        return self._versions[program]

    def _run(self, program, args):
        exe = self.require(program)
        cmd = [exe] + [str(a) for a in args]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            )
        except OSError as exc:
            return RunResult(False, -1, "", f"failed to launch {exe}: {exc}", cmd)
        return RunResult(
            ok=proc.returncode == 0,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            cmd=cmd,
        )

    def ffmpeg(self, args):
        """Run an FFmpeg command (argument list). Returns :class:`RunResult`."""
        return self._run("ffmpeg", args)

    def ffprobe(self, args):
        """Run an FFprobe command (argument list). Returns :class:`RunResult`."""
        return self._run("ffprobe", args)
