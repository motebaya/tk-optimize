"""The MP4 upload optimization transformation.

Applies a byte-level transformation: for the H.264 video track it appends
N*9 extra 8-byte samples (all pointing at a shared 8-byte tail chunk),
rewrites the sample tables (stts/stsc/stsz/stco|co64), relocates moov
before mdat, shifts every original chunk offset, patches the stsd bitrates,
and rewrites the udta/.too encoder marker to "Lavf60.16.100".

Everything is derived dynamically from the input; no reference-specific
values are hard-coded.
"""

import os
import struct

from . import boxes
from .boxes import iter_boxes, make_box, parse_box
from .parser import H264_ENTRY_TYPES, ParseError

# Immutable transformation invariants.  These are proven values, not tuning
# knobs: changing any of them changes the transform semantics and the
# byte-identical Phase 04 output, so they are deliberately not configurable.
SHARED_SAMPLE_PAYLOAD = bytes.fromhex("0000000400000000")
TARGET_ENCODER_MARKER = "Lavf60.16.100"
EXTRA_SAMPLE_MULTIPLIER = 9
FREE_BOX = b"\x00\x00\x00\x08free"

# The encoder marker lives in exactly one place: the `data` box of the
# `moov/udta/meta/ilst/(c)too` chain.  Nothing else is scanned or patched.
TOO_DATA_PATH = ("moov", "udta", "meta", "ilst", "\xa9too", "data")

CONTAINER_TYPES = {
    "moov",
    "trak",
    "edts",
    "mdia",
    "minf",
    "stbl",
    "stsd",
    "dinf",
    "udta",
    "meta",
    "ilst",
    "mvex",
    "tref",
    "mfra",
    "meco",
}


def _is_container(box):
    if box.type in CONTAINER_TYPES:
        return True
    return box.type.startswith("\xa9")


class TransformError(Exception):
    """Raised when the output cannot be produced or written."""


def _track_bitrate(track):
    """Average bitrate derived from the sample table and mdhd timescale."""
    total = sum(track.table.sample_sizes)
    if track.duration <= 0:
        raise ParseError(
            f"track {track.track_id} has zero mdhd duration; cannot compute bitrate"
        )
    return total * 8 * track.timescale // track.duration


def _patch_encoder_marker(payload):
    """Replace the encoder-version suffix in the .too data payload.

    Only called on the ``data`` box at ``moov/udta/meta/ilst/(c)too``.
    Returns the payload unchanged if it has no ``Lavf`` string.
    """
    i = payload.find(b"Lavf")
    if i < 0:
        return payload
    j = i + 4
    while j < len(payload) and (payload[j : j + 1] in b"0123456789."):
        j += 1
    if j == i + 4:
        return payload
    return payload[:i] + TARGET_ENCODER_MARKER.encode() + payload[j:]


def _esds_bitrate_offsets(payload):
    """Locate (max_bitrate, avg_bitrate) offsets within an esds box payload."""
    p = payload
    if len(p) < 4:
        raise ParseError("esds payload too short")
    i = 4

    def read_length(i):
        length = 0
        for _ in range(4):
            if i >= len(p):
                raise ParseError("esds: truncated descriptor length")
            b = p[i]
            i += 1
            length = (length << 7) | (b & 0x7F)
            if not (b & 0x80):
                return length, i
        raise ParseError("esds: descriptor length too long")

    if i >= len(p) or p[i] != 0x03:
        raise ParseError("esds: first descriptor is not ES_Descriptor")
    i += 1
    _, i = read_length(i)
    if i + 3 > len(p):
        raise ParseError("esds: ES_Descriptor truncated")
    i += 3  # ES_ID(2) + flags(1)
    flags = p[i - 1]
    if flags & 0x80:
        i += 2
    if flags & 0x40:
        if i >= len(p):
            raise ParseError("esds: truncated URL flag")
        i += 1 + p[i]
    if flags & 0x20:
        i += 2
    if i >= len(p) or p[i] != 0x04:
        raise ParseError("esds: no DecoderConfigDescriptor")
    i += 1
    _, i = read_length(i)
    # content: OTI(1) streamType(1) bufferSize(3) maxBitrate(4) avgBitrate(4)
    if i + 9 > len(p):
        raise ParseError("esds: DecoderConfigDescriptor truncated")
    return i + 5, i + 9


def _entry_children_offset(box_type):
    """Byte offset from a sample entry's payload to its child boxes.

    H.264 visual entries carry 78 bytes of fixed fields before their child
    boxes; everything else handled here (audio entries) uses 28 bytes.
    """
    return 78 if box_type in H264_ENTRY_TYPES else 28


def _patch_stsd(data, stsd_box, sample_type, bitrate, patch_esds):
    """Rebuild an stsd payload, patching btrt (and esds) bitrates in the
    sample entry of `sample_type`."""
    payload = data[stsd_box.payload_offset : stsd_box.end]
    pos = 8
    out = bytearray(payload[:8])
    while pos < len(payload):
        box = parse_box(payload, pos, len(payload))
        if box is None:
            break
        raw = payload[box.offset : box.end]
        if box.type == sample_type:
            children_start = box.payload_offset + _entry_children_offset(box.type)
            for c in iter_boxes(payload, children_start, box.end):
                if c.type == "btrt":
                    new_btrt = make_box("btrt", struct.pack(">III", 0, bitrate, bitrate))
                    rel = c.offset - box.offset
                    raw = raw[:rel] + new_btrt + raw[rel + c.size :]
                elif patch_esds and c.type == "esds":
                    es_payload = payload[c.payload_offset : c.end]
                    offs = _esds_bitrate_offsets(es_payload)
                    new_es = bytearray(es_payload)
                    for off in offs:
                        new_es[off : off + 4] = struct.pack(">I", bitrate)
                    new_esds = make_box("esds", bytes(new_es))
                    rel = c.offset - box.offset
                    raw = raw[:rel] + new_esds + raw[rel + c.size :]
        out += raw
        pos = box.end
    return make_box("stsd", bytes(out))


def build_moov(pf):
    """Build the new moov box (with placeholder stco/co64 entries).

    Returns (moov_bytes, slots) where `slots` is a list of
    (payload_offset, track_id, offset_box_type) identifying every
    stco/co64 box that must be finalised after the file layout is known.
    """
    data = pf.data
    video = pf.video
    n = video.table.sample_count
    k = n * EXTRA_SAMPLE_MULTIPLIER
    first_added_chunk = video.table.chunk_count + 1

    stts = video.table.stts
    last_delta = stts[-1][1]
    new_stts = (
        struct.pack(">II", 0, len(stts) + 1)
        + b"".join(struct.pack(">II", sc, sd) for sc, sd in stts)
        + struct.pack(">II", k, last_delta)
    )

    new_stsc = (
        struct.pack(">II", 0, len(video.table.stsc) + 1)
        + b"".join(struct.pack(">III", *e) for e in video.table.stsc)
        + struct.pack(">III", first_added_chunk, 1, 1)
    )

    new_stsz = (
        struct.pack(">III", 0, 0, n + k)
        + struct.pack(f">{n}I", *video.table.sample_sizes)
        + struct.pack(f">{k}I", *([8] * k))
    )

    fmt = "Q" if video.table.offset_box == "co64" else "I"
    video_count = video.table.chunk_count + k
    placeholder_offsets = struct.pack(f">{video_count}{fmt}", *([0] * video_count))
    new_stco = struct.pack(">II", 0, video_count) + placeholder_offsets

    stts_payloads = {"stts": new_stts, "stsc": new_stsc, "stsz": new_stsz}
    slots = []

    def transform(box, ctx, base, path):
        # base = offset of this box's payload within the new moov payload
        t = box.type
        track_id = ctx.get("track_id")
        if t == "data" and path == TOO_DATA_PATH:
            return make_box(t, _patch_encoder_marker(data[box.payload_offset : box.end]))
        if track_id == video.track_id:
            if t in stts_payloads:
                return make_box(t, stts_payloads[t])
            if t in ("stco", "co64"):
                slots.append((base - box.header, track_id, t))
                return make_box(t, new_stco)
            if t == "stsd":
                return _patch_stsd(
                    data, box, video.codec, _track_bitrate(video), patch_esds=False
                )
        elif t in ("stco", "co64") and any(
            a.track_id == track_id for a in ctx.get("audio_tracks", [])
        ):
            slots.append((base - box.header, track_id, t))
            return make_box(t, data[box.payload_offset : box.end])
        elif t == "stsd":
            for a in ctx.get("audio_tracks", []):
                if a.track_id == track_id:
                    return _patch_stsd(
                        data, box, a.codec, _track_bitrate(a), patch_esds=True
                    )
        return None

    def rebuild(box, ctx, base, path=()):
        # base = offset of this box's payload within the new moov payload
        replacement = transform(box, ctx, base, path)
        if replacement is not None:
            return replacement
        prefix = b""
        start = box.payload_offset
        if box.type == "meta":
            prefix = data[start : start + 4]
            start += 4
        children = list(iter_boxes(data, start, box.end)) if _is_container(box) else []
        if not children:
            return boxes.box_bytes(data, box)
        parts = []
        emitted = len(prefix)
        off = start
        for child in children:
            parts.append(data[off : child.offset])
            emitted += len(parts[-1])
            child_ctx = ctx
            if child.type == "trak":
                child_ctx = dict(ctx)
                child_ctx["track_id"] = _trak_track_id(data, child)
            child_start = base + emitted
            parts.append(
                rebuild(child, child_ctx, child_start + child.header, path + (child.type,))
            )
            emitted += len(parts[-1])
            off = child.end
        parts.append(data[off : box.end])
        return make_box(box.type, prefix + b"".join(parts))

    ctx = {
        "video": video,
        "audio_tracks": pf.audio_tracks,
        "track_id": None,
    }
    moov_payload = rebuild(pf.moov, ctx, 0, path=("moov",))
    return moov_payload, slots


def _trak_track_id(data, trak):
    tkhd = boxes.find_child(data, trak, "tkhd")
    if tkhd is None:
        raise ParseError("trak has no tkhd")
    payload = tkhd.payload_offset
    version = data[payload]
    if version == 1:
        return boxes.u32(data, payload + 20)
    return boxes.u32(data, payload + 12)


def transform_bytes(pf):
    """Transform a parsed file into the full output file bytes."""
    video = pf.video
    if video.codec not in H264_ENTRY_TYPES:
        raise TransformError(
            f"transformation requires H.264/AVC video (found {video.codec}); "
            "convert HEVC/H.265 to H.264 first"
        )
    n = video.table.sample_count
    k = n * EXTRA_SAMPLE_MULTIPLIER

    moov_bytes, slots = build_moov(pf)

    ftyp_raw = boxes.box_bytes(pf.data, pf.ftyp)
    old_payload_offset = pf.mdat_payload_offset

    new_mdat_payload = pf.mdat_payload + SHARED_SAMPLE_PAYLOAD
    if len(new_mdat_payload) + 8 <= 0xFFFFFFFF:
        mdat_header = 8
        mdat_new = make_box("mdat", new_mdat_payload)
    else:
        mdat_header = 16
        mdat_new = (
            struct.pack(">I", 1)
            + b"mdat"
            + struct.pack(">Q", mdat_header + len(new_mdat_payload))
            + new_mdat_payload
        )

    new_payload_offset = len(ftyp_raw) + len(FREE_BOX) + len(moov_bytes) + mdat_header
    shift = new_payload_offset - old_payload_offset
    tail_abs = new_payload_offset + len(pf.mdat_payload)

    out = bytearray(moov_bytes[8:])
    for base, track_id, offset_box in slots:
        if track_id == video.track_id:
            offs = [o + shift for o in video.table.chunk_offsets] + [tail_abs] * k
        else:
            audio = next(t for t in pf.audio_tracks if t.track_id == track_id)
            offs = [o + shift for o in audio.table.chunk_offsets]
        fmt = "Q" if offset_box == "co64" else "I"
        payload = struct.pack(">II", 0, len(offs)) + struct.pack(
            f">{len(offs)}{fmt}", *offs
        )
        new_box = make_box(offset_box, payload)
        if base + len(new_box) > len(out):
            raise TransformError("internal error: stco slot out of bounds")
        out[base : base + len(new_box)] = new_box

    final_moov = make_box("moov", bytes(out))
    if len(final_moov) != len(moov_bytes):
        raise TransformError("internal error: moov size changed during splicing")

    return ftyp_raw + FREE_BOX + final_moov + mdat_new


def transform_file(path, out_path=None):
    """Transform the file at `path`, writing the result.

    Defaults the output path to `<stem>-optimized.mp4`. Refuses to
    overwrite either the source or an existing output file.
    """
    if out_path is None:
        stem, _ = os.path.splitext(path)
        out_path = stem + "-optimized.mp4"
    if os.path.abspath(out_path) == os.path.abspath(path):
        raise TransformError("output path must differ from the input path")
    if os.path.exists(out_path):
        raise TransformError(f"output already exists: {out_path}")

    from .parser import parse_file

    pf = parse_file(path)
    output = transform_bytes(pf)
    with open(out_path, "wb") as fh:
        fh.write(output)
    return out_path