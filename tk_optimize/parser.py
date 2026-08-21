"""Input validation and MP4 structure extraction.

Parses a source MP4, validates that it contains exactly one H.264/AVC video
track, and extracts the sample tables needed by the transformer.

All values are derived dynamically from the input; nothing here is
reference-specific.
"""

import os
import struct

from . import boxes


class ParseError(Exception):
    """Raised when the input cannot be transformed."""


H264_ENTRY_TYPES = ("avc1", "avc2", "avc3")
HEVC_ENTRY_TYPES = ("hvc1", "hev1")
SUPPORTED_INPUT_CODECS = H264_ENTRY_TYPES + HEVC_ENTRY_TYPES
VIDEO_ENTRY_TYPES = SUPPORTED_INPUT_CODECS + ("av01", "vp09", "mp4v")


def is_h264(codec: str) -> bool:
    """True if the sample-entry type is AVC/H.264 (directly transformable)."""
    return codec in H264_ENTRY_TYPES


def is_hevc(codec: str) -> bool:
    """True if the sample-entry type is HEVC/H.265 (needs preprocessing)."""
    return codec in HEVC_ENTRY_TYPES


def is_supported_input_codec(codec: str) -> bool:
    """True if the codec can be processed, directly or via preprocessing."""
    return codec in SUPPORTED_INPUT_CODECS


def is_transformable(codec: str) -> bool:
    """True if the codec can be transformed without preprocessing.

    Only AVC/H.264 inputs are transformed directly; HEVC must first be
    converted to H.264, so it is *supported* but not *transformable*.
    """
    return is_h264(codec)


def codec_label(codec):
    """Friendly label for a sample-entry type."""
    if is_h264(codec):
        return "H.264/AVC"
    if is_hevc(codec):
        return "HEVC/H.265"
    return codec


class SampleTable:
    """Parsed stts / stsc / stsz / stco|co64 for a single track."""

    __slots__ = (
        "stts",
        "stsc",
        "sample_sizes",
        "chunk_offsets",
        "offset_box",
        "sample_count",
        "chunk_count",
    )

    def __init__(self, stts, stsc, sample_sizes, chunk_offsets, offset_box):
        self.stts = stts
        self.stsc = stsc
        self.sample_sizes = sample_sizes
        self.chunk_offsets = chunk_offsets
        self.offset_box = offset_box
        self.sample_count = len(sample_sizes)
        self.chunk_count = len(chunk_offsets)


class Track:
    __slots__ = (
        "track_id",
        "is_video",
        "codec",
        "width",
        "height",
        "timescale",
        "duration",
        "table",
        "stsd",
        "mdhd",
    )

    def __init__(self):
        self.track_id = None
        self.is_video = False
        self.codec = None
        self.width = 0
        self.height = 0
        self.timescale = 0
        self.duration = 0
        self.table = None
        self.stsd = None
        self.mdhd = None


class ParsedFile:
    __slots__ = (
        "path",
        "data",
        "tops",
        "ftyp",
        "moov",
        "mdat",
        "tracks",
        "video",
        "audio_tracks",
        "mdat_payload_offset",
        "mdat_payload",
    )

    def __init__(self):
        self.path = None
        self.data = None
        self.tops = None
        self.ftyp = None
        self.moov = None
        self.mdat = None
        self.tracks = []
        self.video = None
        self.audio_tracks = []
        self.mdat_payload_offset = 0
        self.mdat_payload = b""


def _require(cond, message):
    if not cond:
        raise ParseError(message)


def _read_track_header(data, tkhd):
    payload = tkhd.payload_offset
    version = data[payload]
    track_id = boxes.u32(data, payload + 20) if version == 1 else boxes.u32(data, payload + 12)
    if version == 1:
        duration = boxes.u64(data, payload + 28)
    else:
        duration = boxes.u32(data, payload + 20)
    return track_id, duration


def _read_mdhd(data, mdhd):
    payload = mdhd.payload_offset
    version = data[payload]
    if version == 1:
        timescale = boxes.u32(data, payload + 20)
        duration = boxes.u64(data, payload + 24)
    else:
        timescale = boxes.u32(data, payload + 12)
        duration = boxes.u32(data, payload + 16)
    return timescale, duration


def _read_stts(data, stts):
    payload = stts.payload_offset
    count = boxes.u32(data, payload + 4)
    entries = []
    for i in range(count):
        off = payload + 8 + 8 * i
        entries.append(struct.unpack(">II", data[off : off + 8]))
    return entries


def _read_stsc(data, stsc):
    payload = stsc.payload_offset
    count = boxes.u32(data, payload + 4)
    entries = []
    for i in range(count):
        off = payload + 8 + 12 * i
        entries.append(struct.unpack(">III", data[off : off + 12]))
    return entries


def _read_stsz(data, stsz):
    payload = stsz.payload_offset
    sample_size = boxes.u32(data, payload + 4)
    count = boxes.u32(data, payload + 8)
    if sample_size != 0:
        sizes = [sample_size] * count
    else:
        sizes = list(struct.unpack(f">{count}I", data[payload + 12 : payload + 12 + 4 * count]))
    return sizes


def _read_stco(data, stco):
    payload = stco.payload_offset
    count = boxes.u32(data, payload + 4)
    return list(struct.unpack(f">{count}I", data[payload + 8 : payload + 8 + 4 * count]))


def _read_co64(data, co64):
    payload = co64.payload_offset
    count = boxes.u32(data, payload + 4)
    return list(struct.unpack(f">{count}Q", data[payload + 8 : payload + 8 + 8 * count]))


def _read_visual_dimensions(data, stsd):
    # stsd -> first sample entry (avc1/avc2/avc3)
    entries = list(boxes.iter_boxes(data, stsd.payload_offset + 8, stsd.end))
    _require(entries, "stsd contains no sample entries")
    entry = entries[0]
    payload = entry.payload_offset
    _require(payload + 28 <= entry.end, "sample entry too small for visual dimensions")
    width = struct.unpack(">H", data[payload + 24 : payload + 26])[0]
    height = struct.unpack(">H", data[payload + 26 : payload + 28])[0]
    return entry.type, width, height


def _read_track(data, trak):
    tkhd = boxes.find_child(data, trak, "tkhd")
    _require(tkhd is not None, "trak has no tkhd")
    track = Track()
    track.track_id, _ = _read_track_header(data, tkhd)

    mdia = boxes.find_child(data, trak, "mdia")
    _require(mdia is not None, "trak has no mdia")
    mdhd = boxes.find_child(data, mdia, "mdhd")
    _require(mdhd is not None, "mdia has no mdhd")
    track.mdhd = mdhd
    track.timescale, track.duration = _read_mdhd(data, mdhd)

    minf = boxes.find_child(data, mdia, "minf")
    _require(minf is not None, "mdia has no minf")
    stbl = boxes.find_child(data, minf, "stbl")
    _require(stbl is not None, "minf has no stbl")

    stsd = boxes.find_child(data, stbl, "stsd")
    _require(stsd is not None, "stbl has no stsd")
    track.stsd = stsd

    entries = list(boxes.iter_boxes(data, stsd.payload_offset + 8, stsd.end))
    _require(entries, "stsd has no sample entries")
    entry_type = entries[0].type
    track.codec = entry_type
    track.is_video = entry_type in VIDEO_ENTRY_TYPES

    if track.is_video:
        _, track.width, track.height = _read_visual_dimensions(data, stsd)

    stts = boxes.find_child(data, stbl, "stts")
    stsc = boxes.find_child(data, stbl, "stsc")
    stsz = boxes.find_child(data, stbl, "stsz")
    stco = boxes.find_child(data, stbl, "stco")
    co64 = boxes.find_child(data, stbl, "co64")
    _require(stts is not None, f"track {track.track_id} has no stts")
    _require(stsc is not None, f"track {track.track_id} has no stsc")
    _require(stsz is not None, f"track {track.track_id} has no stsz")
    _require(
        (stco is not None) != (co64 is not None),
        f"track {track.track_id} must have exactly one of stco/co64",
    )

    table = SampleTable(
        stts=_read_stts(data, stts),
        stsc=_read_stsc(data, stsc),
        sample_sizes=_read_stsz(data, stsz),
        chunk_offsets=_read_stco(data, stco) if stco else _read_co64(data, co64),
        offset_box="stco" if stco else "co64",
    )
    _require(table.sample_count > 0, f"track {track.track_id} has no samples")
    _require(table.chunk_count > 0, f"track {track.track_id} has no chunks")
    track.table = table
    return track


def parse_file(path):
    """Validate and parse an input MP4 file. Returns a ParsedFile."""
    _require(os.path.isfile(path), f"file not found: {path}")

    with open(path, "rb") as fh:
        data = fh.read()

    tops = list(boxes.iter_boxes(data, 0, len(data)))
    _require(tops, "not an MP4 container (no top-level boxes)")

    ftyp = next((b for b in tops if b.type == "ftyp"), None)
    _require(ftyp is not None, "not an MP4 container (no ftyp box)")
    moov = next((b for b in tops if b.type == "moov"), None)
    _require(moov is not None, "no moov box found")
    mdat = next((b for b in tops if b.type == "mdat"), None)
    _require(mdat is not None, "no mdat box found")

    pf = ParsedFile()
    pf.path = path
    pf.data = data
    pf.tops = tops
    pf.ftyp = ftyp
    pf.moov = moov
    pf.mdat = mdat
    pf.mdat_payload_offset = mdat.payload_offset
    pf.mdat_payload = data[mdat.payload_offset : mdat.end]

    for trak in boxes.iter_boxes(data, moov.payload_offset, moov.end):
        if trak.type != "trak":
            continue
        track = _read_track(data, trak)
        pf.tracks.append(track)

    _require(pf.tracks, "moov contains no tracks")

    video_tracks = [t for t in pf.tracks if t.is_video]
    _require(len(video_tracks) <= 1, f"expected exactly 1 video track, found {len(video_tracks)}")
    _require(video_tracks, "no video track found")
    pf.video = video_tracks[0]
    pf.audio_tracks = [t for t in pf.tracks if not t.is_video]

    _require(pf.video.width > 0 and pf.video.height > 0, "video track has no width/height")
    _require(pf.video.timescale > 0, "video track has zero timescale")

    return pf


def describe(pf):
    """Return a compact human-readable summary of the parsed input."""
    v = pf.video
    return {
        "path": pf.path,
        "codec": v.codec,
        "width": v.width,
        "height": v.height,
        "timescale": v.timescale,
        "duration": v.duration,
        "samples": v.table.sample_count,
        "chunks": v.table.chunk_count,
        "audio_tracks": len(pf.audio_tracks),
    }