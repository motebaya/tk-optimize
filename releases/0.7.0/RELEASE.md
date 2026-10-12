# Release 0.7.0 — 2026-10-12

## What this release adds

Three reproducible MP4 transformation lineages behind one CLI, plus the
tooling to keep them honest:

| `--method` | Lineage            | Pipeline                                            | Byte-identity status |
|------------|--------------------|-----------------------------------------------------|----------------------|
| `stealth`  | stream-copy        | audio rebuild → stream copy → fake-sample surgery   | Verified on both fixtures with Linux FFmpeg 6.1.3 |
| `shadow`   | encode-patch       | H.264 encode → copy remux → container patch         | Verified on both fixtures with Windows FFmpeg 8.0 |
| `trace`    | legacy workflow    | codec-routed optimize/transform                     | Unchanged            |

## Verified outputs (whole-file SHA-256)

Shadow lineage (FFmpeg 8.0, Windows):
- SDR fixture   → `c4e829372b7a87dab5779e94dc2b07552a83d3f225711f93300d7e09a472bb32`
- HDR10+ fixture→ `9430093987251d1bc84b6194078506e9df2e98e9c246b59311b2a86d00d4c648`

Stealth lineage (FFmpeg 6.1.3, Linux/WSL):
- SDR fixture   → `f576c6b868821312d6cdcf28f5c49f7b22757908340e5816d01c77ffc227c030`
- HDR10+ fixture→ `428ee083d99d1e5e230812b15a4be8757f42aeeb6d5f775a089fcba9610b4973`

Determinism: repeated runs on identical inputs produce identical SHA-256
for both lineages (covered by `tests/test_lineage_regression.py`).

## Toolchain requirements

- `stealth` requires a Linux-built FFmpeg 6.1.x. Windows FFmpeg 6.1.1
  builds differ by rare AAC bit-allocation flips (documented in the
  internal research notes); the difference is two audio frames per file.
- `shadow` requires an FFmpeg 8.0 (`Lavf62.3.100`) build with libx264.
- Set `TK_SHADOW_FFMPEG` / `TK_STEALTH_FFMPEG` to enable the
  byte-identity regression tests; without them the tests skip (a skip is
  not a pass).

## Known limitations

- `--dry-run` is implemented for the `trace` workflow only.
- Interactive prompts require a TTY; non-interactive runs fall back to
  `trace` (or read a piped yes/no line).

## Release readiness

Release readiness is checked before publication: test suite green, branding
policy holds on tracked files, versioning is consistent, and the reproducible
byte-identity checks pass wherever the required toolchain is present. Checks
that cannot run on a given host are reported as NOT VERIFIED, never as passed.
The maintainer-side gate script that automates this lives under `tools/`
(gitignored) and is not part of the shipped tree.
