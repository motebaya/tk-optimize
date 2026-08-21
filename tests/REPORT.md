# Test Report — tk-optimize

Status: **49 passed, 0 failed** on the final revision.

## Environment

- Platform: Linux
- Python: 3.10 (tests ran under `pytest 9.1.1` via the `rtk` wrapper)
- `ffmpeg` 7.0.2 / `ffprobe` present on `PATH` at `/usr/local/bin`
- `libx265` encoder available → HEVC fixtures auto-generated
- Working directory: `tk-optimize/` (see `pytest.ini`: `pythonpath = .`, `testpaths = tests`)

## How to run

```bash
cd tk-optimize
pytest -q                # or: pytest tests/
```

`tests/conftest.py` generates the media fixtures with `ffmpeg` on first use:

- `tests/fixtures/small.mp4` — H.264/AAC, 64×64, 1 s (10 samples, 10 chunks)
- `tests/fixtures/audio-only.mp4` — AAC, no video (rejection path)
- `tests/fixtures/small-hevc.mp4` — HEVC/H.265+AAC, 64×64, 0.5 s
- `tests/fixtures/small-mpeg4.mp4` — MPEG-4 Part 2, no audio (unsupported codec)

If `ffmpeg` (or the relevant encoder) is unavailable, the affected fixtures
skip; the rest still run.

## Test inventory

### `tests/test_boxes.py` — box primitives (7 tests)

| Test                                    | What it checks                                                 |
| --------------------------------------- | -------------------------------------------------------------- |
| `test_parse_box_32bit`                  | size/type/header/payload/end for a 32-bit box                  |
| `test_parse_box_64bit`                  | 64-bit `largesize` header parsing (16-byte header)             |
| `test_parse_box_size_zero_runs_to_end`  | `size == 0` box runs to the buffer end                         |
| `test_parse_box_truncated_returns_none` | truncated header → `None` (no crash)                           |
| `test_iter_boxes_stops_at_garbage`      | iteration halts at non-box data                                |
| `test_find_child_and_box_bytes`         | `find_child` walks `moov → udta → meta(fullbox) → ilst → ©too` |
| `test_make_box_roundtrip`               | `make_box` → `parse_box` round-trip preserves payload          |

### `tests/test_ffmpeg.py` — discovery & runner (12 tests)

| Test                                                | What it checks                                                       |
| --------------------------------------------------- | -------------------------------------------------------------------- |
| `test_env_override_wins_over_path`                  | `TK_FFMPEG` beats `PATH` discovery                                   |
| `test_env_override_invalid_raises`                  | invalid override → clear `FFmpegNotFoundError`                       |
| `test_path_discovery_when_available`                | `shutil.which` PATH discovery works                                  |
| `test_missing_returns_none`                         | no env / PATH / fallback → `None`                                    |
| `test_missing_message_is_actionable`                | `[!] ffmpeg not found.` / `[+] Set TK_FFMPEG or add ffmpeg to PATH.` |
| `test_windows_candidates`                           | Windows fallbacks include `scoop\shims\ffmpeg.exe` etc.              |
| `test_windows_missing_returns_none`                 | Windows discovery returns `None` when nothing exists                 |
| `test_linux_candidates`                             | Linux fallbacks include `/usr/local/bin/ffmpeg`, no `.exe`           |
| `test_runner_constructs_without_requiring_binaries` | runner is lazy; missing binary raises only on use                    |
| `test_runner_uses_argument_list_not_shell`          | command is an argument list, never `shell=True`                      |
| `test_runner_reports_failure`                       | non-zero exit surfaces `stderr_tail` in a `RunResult`                |
| `test_runner_launch_failure_is_structured`          | launch errors are captured, not raised raw                           |

### `tests/test_preprocess.py` — HEVC → H.264 (8 tests)

| Test                                                 | What it checks                                                                 |
| ---------------------------------------------------- | ------------------------------------------------------------------------------ |
| `test_build_args_uses_libx264_defaults`              | `libx264 / preset slow / crf 16 / pix_fmt yuv420p`, `-c:a copy`, mapping flags |
| `test_build_args_preserves_colour_metadata`          | `-color_primaries/-color_trc/-colorspace/-color_range` from source             |
| `test_build_args_does_not_change_geometry_or_timing` | no `-vf`/filters/`-r`/SAR changes; `-noautorotate` present                     |
| `test_build_args_preserves_interlaced_flag`          | interlaced source → `-flags +ildct+ilme`, `-top`                               |
| `test_display_frame_rate`                            | `"30/1"` → `30 fps`, `"24000/1001"` → `23.976 fps`                             |
| `test_hevc_to_h264_preserves_properties`             | end-to-end: output is H.264, same resolution, frame rate, audio                |
| `test_hevc_to_h264_does_not_swap_dimensions`         | non-square 96×64 source stays 96×64 (no autorotate/resize)                     |
| `test_hevc_to_h264_rejects_avc_source`               | converting an H.264 source fails with `expected HEVC`                          |

### `tests/test_workflow.py` — routing & CLI (10 tests)

| Test                                      | What it checks                                                   |
| ----------------------------------------- | ---------------------------------------------------------------- |
| `test_h264_bypasses_ffmpeg`               | H.264 path never constructs an `FFmpegRunner`                    |
| `test_hevc_routes_to_conversion`          | HEVC path converts, transforms, all checks pass                  |
| `test_hevc_rejection_cancels_cleanly`     | user decline → cancelled, no output                              |
| `test_hevc_missing_ffmpeg_is_clear_error` | missing FFmpeg raises the actionable error                       |
| `test_unsupported_codec_rejected`         | `mp4v` → `UnsupportedCodecError`                                 |
| `test_default_output_path`                | `<stem>-optimized.mp4` naming                                    |
| `test_cli_h264_direct`                    | CLI: H.264 direct, `[+] input codec: H.264/AVC`, output produced |
| `test_cli_hevc_reject_no_output`          | CLI: decline → exit 0, no output file                            |
| `test_cli_hevc_accept_convert`            | CLI: accept → converts + transforms; source untouched            |
| `test_cli_missing_ffmpeg_message`         | CLI: bad `TK_FFMPEG` → exit 1, actionable message                |

### `tests/test_regression.py` — byte-identical proof (2 tests)

Skipped automatically unless `upload.mp4`/`download.mp4` are present and
`upload.mp4` matches its reference hash.

| Test                               | What it checks                                                           |
| ---------------------------------- | ------------------------------------------------------------------------ |
| `test_byte_identical_to_reference` | output bytes equal `download.mp4` exactly; SHA-256 = `4f9cf05e…49d23d6`  |
| `test_reference_hashes`            | `upload.mp4` = `4709450655…78246f5`, `download.mp4` = `4f9cf05e…49d23d6` |

### `tests/test_structural.py` — generalisation & safety (5 tests)

Runs against a **fresh** ffmpeg-generated H.264+AAC file (mdat-before-moov,
10 samples, 10 chunks) — proving the transformation is dynamic, not fitted to
the reference pair.

| Test                                               | What it checks                                                                                                                                                                                                   |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_transform_generalizes_and_verifies`          | output layout `ftyp/free/moov/mdat`; `N*10` samples; stts appended delta; stsc `(chunk_count+1,1,1)`; filler sizes `8`; original offsets shifted; extra chunks → shared `tail_abs`; mdat preserved + 8-byte tail |
| `test_transform_is_deterministic`                  | two runs over the same input produce identical bytes                                                                                                                                                             |
| `test_too_string_marker`                           | `Lavf60.16.100` present; source muxer version absent                                                                                                                                                             |
| `test_transform_file_output_path_and_no_overwrite` | `<stem>-optimized.mp4` created; second run refuses; source untouched                                                                                                                                             |
| `test_transform_file_rejects_source_as_output`     | refusing to write over the input                                                                                                                                                                                 |

### `tests/test_rejection.py` — clean failures (5 tests)

| Test                                     | What it checks                                          |
| ---------------------------------------- | ------------------------------------------------------- |
| `test_missing_file`                      | `ParseError: file not found`                            |
| `test_garbage_file`                      | `ParseError: not an MP4 container (no top-level boxes)` |
| `test_audio_only_rejected`               | no video track → `ParseError: no video track found`     |
| `test_cli_missing_input_exits_nonzero`   | CLI exits non-zero with the message                     |
| `test_cli_existing_output_exits_nonzero` | CLI refuses to overwrite an existing output             |

## Results

```
49 tests collected
49 passed, 0 failed
```

## Results over the development session

| Stage                       | Result    | Notes                                                  |
| --------------------------- | --------- | ------------------------------------------------------ |
| H.264 feature complete      | 19 passed | box/structural/regression/rejection suite              |
| + FFmpeg runner & discovery | 49 passed | new `test_ffmpeg.py`                                   |
| + HEVC preprocessing        | 49 passed | new `test_preprocess.py`                               |
| + workflow routing + CLI    | 49 passed | new `test_workflow.py`; rejection messages updated     |
| + `-noautorotate` fix       | 49 passed | conversion no longer applies source rotation to pixels |

## Manual verification (recorded in `WALKTHROUGH.md`)

- **H.264 regression (unchanged)** — `tk-cli.py upload.mp4` produces
  `upload-optimized.mp4` **byte-identical** to `download.mp4`; all 13
  verifier checks pass; SHA-256 matches `4f9cf05e…49d23d6`.
- **HEVC samples** — `sample1/2/3.mp4` (real camera HEVC, 1080p@30, hvc1)
  each ran the full CLI workflow: detected `HEVC/H.265`, conversion accepted,
  FFmpeg at `/usr/local/bin/ffmpeg`, resolution/rate/audio preserved, all 13
  structural checks passed. Outputs: `sample1-optimized.mp4`,
  `sample2-optimized.mp4`, `sample3-optimized.mp4`.
- **Orientation preserved** — the samples carry `rotation=-90` metadata;
  `-noautorotate` keeps the native 1920×1080 geometry and preserves the
  rotation metadata for players instead of rotating pixels.
- **Cleanup** — no intermediate files left in temp directories after success.

## Known limitations

- The **64-bit (largesize) mdat header** path (>4 GiB payload) is implemented
  and unit-tested at the box level (`test_parse_box_64bit`) but not exercised
  end-to-end — generating a >4 GiB fixture is impractical.
- Reference fixtures are large; regression tests skip rather than download
  anything.
- HEVC preprocessing requires FFmpeg + FFprobe (resolved via
  `TK_FFMPEG`/`TK_FFPROBE`, then `PATH`, then platform fallbacks). The
  H.264 path has no FFmpeg dependency.
- No SHA-256 assertions are made on `sample1/2/3.mp4` outputs: those files
  have no download reference artifacts, so only structural and
  preprocessing behaviour is verified.
