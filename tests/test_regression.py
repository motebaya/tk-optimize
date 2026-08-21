"""Regression against the Phase 04 reference pair.

Requires the original fixtures (upload.mp4 / download.mp4). Skipped when
the fixtures are absent or the reference hash does not match, so the
suite stays green on machines without the large inputs.
"""

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tk_optimize import transform_bytes, parse_file

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPLOAD = os.environ.get("TKR_UPLOAD", os.path.join(REPO_ROOT, "upload.mp4"))
DOWNLOAD = os.environ.get("TKR_DOWNLOAD", os.path.join(REPO_ROOT, "download.mp4"))

UPLOAD_SHA256 = "4709450655150410f0368539450b9a95dce238dc43487e0eb9d66e08578246f5"
DOWNLOAD_SHA256 = "4f9cf05e53a26e97c587c19dbf380d84cc2b7473aaf953aad2bf2ef6c49d23d6"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(scope="module")
def reference_pair():
    if not (os.path.isfile(UPLOAD) and os.path.isfile(DOWNLOAD)):
        pytest.skip("reference fixtures upload.mp4/download.mp4 not present")
    if _sha256(UPLOAD) != UPLOAD_SHA256:
        pytest.skip("upload.mp4 does not match the reference hash")
    return UPLOAD, DOWNLOAD


def test_byte_identical_to_reference(reference_pair, tmp_path):
    upload, download = reference_pair
    out = tmp_path / "out.mp4"
    pf = parse_file(upload)
    out.write_bytes(transform_bytes(pf))
    assert out.read_bytes() == open(download, "rb").read()
    assert _sha256(str(out)) == DOWNLOAD_SHA256


def test_reference_hashes(reference_pair):
    upload, download = reference_pair
    assert _sha256(upload) == UPLOAD_SHA256
    assert _sha256(download) == DOWNLOAD_SHA256