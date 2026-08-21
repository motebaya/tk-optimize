"""Post-transform structural verification.

Re-parses the output file and checks every structural invariant of the
transformation against the parsed input. Does not decode media.
"""

import struct

from . import boxes
from .boxes import iter_boxes
from .parser import parse_file, ParseError
from .transformer import (
    SHARED_SAMPLE_PAYLOAD,
    TARGET_ENCODER_MARKER,
    EXTRA_SAMPLE_MULTIPLIER,
)


class Check:
    __slots__ = ("name", "ok", "detail")

    def __init__(self, name, ok, detail=""):
        self.name = name
        self.ok = bool(ok)
        self.detail = detail

    @property
    def failed(self):
        return not self.ok


def _too_string(data, moov):
    """Extract the udta/meta/ilst/a9too/data string if present."""
    udta = boxes.find_child(data, moov, "udta")
    if udta is None:
        return None
    meta = boxes.find_child(data, udta, "meta")
    if meta is None:
        return None
    for child in iter_boxes(data, meta.payload_offset + 4, meta.end):
        if child.type != "ilst":
            continue
        for item in iter_boxes(data, child.payload_offset, child.end):
            if item.type != "\xa9too":
                continue
            dbox = boxes.find_child(data, item, "data")
            if dbox is not None:
                return data[dbox.payload_offset : dbox.end]
    return None


def verify_output(pf_in, out_path):
    """Verify the transformed file. Returns a list of Check objects."""
    checks = []
    pf = parse_file(out_path)

    tops = [b.type for b in pf.tops]
    checks.append(Check("top-level boxes", tops == ["ftyp", "free", "moov", "mdat"], tops))

    free = next(b for b in pf.tops if b.type == "free")
    checks.append(Check("free box is 8 bytes", free.size == 8, free.size))

    checks.append(
        Check(
            "picture payload preserved",
            pf.mdat_payload[: len(pf_in.mdat_payload)] == pf_in.mdat_payload,
            f"{len(pf_in.mdat_payload)} bytes",
        )
    )
    checks.append(
        Check(
            "shared tail appended once",
            pf.mdat_payload[len(pf_in.mdat_payload) :] == SHARED_SAMPLE_PAYLOAD,
            pf.mdat_payload[len(pf_in.mdat_payload) :],
        )
    )

    video_in, video_out = pf_in.video, pf.video
    n = video_in.table.sample_count
    k = n * EXTRA_SAMPLE_MULTIPLIER

    checks.append(Check("video codec unchanged", video_out.codec == video_in.codec, video_out.codec))
    checks.append(
        Check(
            "video dimensions unchanged",
            (video_out.width, video_out.height) == (video_in.width, video_in.height),
            f"{video_out.width}x{video_out.height}",
        )
    )
    checks.append(
        Check(
            "video timescale unchanged",
            video_out.timescale == video_in.timescale,
            video_out.timescale,
        )
    )
    checks.append(
        Check(
            "video duration unchanged",
            video_out.duration == video_in.duration,
            video_out.duration,
        )
    )

    checks.append(
        Check(
            "sample count",
            video_out.table.sample_count == n * 10,
            video_out.table.sample_count,
        )
    )
    checks.append(
        Check(
            "stts entries preserved",
            video_out.table.stts[:-1] == video_in.table.stts,
            "ok",
        )
    )
    checks.append(
        Check(
            "stts appended entry",
            video_out.table.stts[-1] == (k, video_in.table.stts[-1][1]),
            video_out.table.stts[-1],
        )
    )
    checks.append(
        Check(
            "stsc entries preserved",
            video_out.table.stsc[:-1] == video_in.table.stsc,
            "ok",
        )
    )
    checks.append(
        Check(
            "stsc appended entry",
            video_out.table.stsc[-1] == (video_in.table.chunk_count + 1, 1, 1),
            video_out.table.stsc[-1],
        )
    )
    checks.append(
        Check(
            "sample sizes preserved",
            video_out.table.sample_sizes[:n] == video_in.table.sample_sizes,
            "ok",
        )
    )
    checks.append(
        Check(
            "extra sample sizes are 8",
            all(s == 8 for s in video_out.table.sample_sizes[n:]),
            video_out.table.sample_sizes[n : n + 4],
        )
    )

    shift = pf.mdat_payload_offset - pf_in.mdat_payload_offset
    tail_abs = pf.mdat_payload_offset + len(pf_in.mdat_payload)
    checks.append(Check("payload shift", shift > 0, shift))

    orig_offsets = video_out.table.chunk_offsets[: video_in.table.chunk_count]
    checks.append(
        Check(
            "original chunk offsets shifted",
            orig_offsets == [o + shift for o in video_in.table.chunk_offsets],
            "ok",
        )
    )
    extra_offsets = video_out.table.chunk_offsets[video_in.table.chunk_count :]
    checks.append(Check("extra chunk count", len(extra_offsets) == k, len(extra_offsets)))
    checks.append(
        Check(
            "extra chunks point at shared tail",
            all(o == tail_abs for o in extra_offsets),
            tail_abs,
        )
    )

    for a_in, a_out in zip(pf_in.audio_tracks, pf.audio_tracks):
        checks.append(
            Check(
                f"audio track {a_in.track_id} offsets shifted",
                a_out.table.chunk_offsets
                == [o + shift for o in a_in.table.chunk_offsets],
                "ok",
            )
        )

    too = _too_string(pf.data, pf.moov)
    if too is not None:
        checks.append(
            Check(
                ".too is " + TARGET_ENCODER_MARKER,
                TARGET_ENCODER_MARKER.encode() in too,
                too,
            )
        )

    return checks


def verify_file(pf_in, out_path):
    """Run verification and return (all_ok, checks)."""
    try:
        checks = verify_output(pf_in, out_path)
    except (ParseError, OSError) as exc:
        checks = [Check("re-parse of output", False, str(exc))]
    return all(not c.failed for c in checks), checks