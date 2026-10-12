"""tk_optimize.branding — brand-token loading for lineage-mimicking outputs.

Some reproduced output structures embed producer-identifying metadata strings.
Those strings are NEVER stored in tracked files: they live in an untracked
JSON (default ``branding.json`` next to the repo root, overridable via the
``TK_BRANDING`` environment variable).  When the file is absent, neutral
placeholder values are used and byte-exact lineage reproduction is not
claimed (structural mode only).

Schema::

    {
      "method_stream": {
        "udta": [["artist", "..."], ["comment", "..."], ...],
        "fake_payload": "0000000400000000"
      },
      "method_patch": {"encoder_tag": "..."}
    }
"""

import json
import os

DEFAULT_FILENAME = "branding.json"

NEUTRAL = {
    "method_stream": {
        "udta": [
            ["artist", "tk-optimize"],
            ["comment", "optimized"],
            ["copyright", "{sha32}"],
            ["te_is_reencode", "1"],
            ["encoder", "Lavf60.16.100"],
        ],
        "fake_payload": "0000000400000000",
    },
    "method_patch": {"encoder_tag": None},
}


class BrandingError(Exception):
    pass


def branding_path(cli_path=None):
    if cli_path:
        return cli_path
    env = os.environ.get("TK_BRANDING")
    if env:
        return env
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, DEFAULT_FILENAME)


def load_branding(cli_path=None):
    """Return (merged_config, is_real_branding). Falls back to NEUTRAL."""
    path = branding_path(cli_path)
    cfg = json.loads(json.dumps(NEUTRAL))
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
        except (OSError, ValueError) as exc:
            raise BrandingError(f"branding file unreadable: {path}: {exc}")
        for method, values in user.items():
            cfg.setdefault(method, {}).update(values)
        return cfg, True
    return cfg, False
