# Changelog

All notable changes to this project are documented here.
Versions follow Semantic Versioning; the single canonical version string
lives in `tk_optimize/__init__.py` (`__version__`) and is reported by
`tk-cli.py --version`.

The 0.6.x baseline (the legacy `trace` workflow and its test suite) predates
this changelog; this file starts with 0.7.0.

## [0.7.0] - 2026-10-12

### Added
- Three transformation lineages, selectable via `--method` or an
  interactive prompt:
  - `stealth` — stream-copy lineage (video never re-encoded; audio rebuilt;
    fake-sample container surgery). Reproduces its reference fixtures
    byte-for-byte when run with a Linux-built FFmpeg 6.1.x.
  - `shadow` — encode-patch lineage (single rate-capped H.264 encode,
    stream-copy remux, container patch). Reproduces its SDR and HDR10+
    reference fixtures byte-for-byte with an FFmpeg 8.0 build.
  - `trace` — the legacy upload-optimization workflow (unchanged behavior).
- `tk-cli.py --version` and `--method NAME`; `--output`, `--audio-bitrate`,
  `--video-bitrate`, `--fps` for the lineage methods.
- questionary-based interactive prompts (input path, method selection,
  overwrite confirmation); piped stdin remains supported for automation.
- Lineage byte-identity regression tests (`tests/test_lineage_regression.py`),
  auto-skipping when the gitignored fixtures or required FFmpeg builds are
  absent.
- `release-gate.py` — repeatable release-readiness check reporting
  PASS / FAIL / NOT VERIFIED per gate.

## [0.6.0]

Retrospective summary (no changelog was kept at the time): MP4 container
transformer for upload optimization, box parser/serializer, HEVC→H.264
preprocessing and delivery bitrate encode, FFmpeg/FFprobe discovery runner,
post-transform structural verification, configuration layer (defaults/env/CLI),
semantic logging, and the full test suite.
