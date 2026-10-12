"""Method 1 — stream-copy lineage (CLI name: ``stealth``).

Formula (reverse-engineered from observed reference outputs):

  input -> sha256(input)[:32] as copyright tag
        -> ffmpeg remux: video STREAM COPY (HDR/SEI/rotation preserved),
           audio re-encoded to AAC and mapped to TWO identical audio tracks
        -> container rebuild: ftyp(28) + moov + free(8) + mdat
        -> surgery on the duplicated audio track: 9*N fake samples of payload
           0000000400000000 appended (stsz entries, stts (9N,1), stsc
           append; mdat gets a materialized contiguous fake tail; stco is
           intentionally NOT extended, mirroring the reference structure)
        -> udta/mdta: artist, comment, copyright, te_is_reencode, encoder

Deterministic for a given ffmpeg build.  Whole-file byte-identity against
reference outputs additionally requires an AAC encoder build whose bitstream
matches: a Linux-built FFmpeg 6.1.x reproduces both reference fixtures
byte-for-byte, while Windows builds differ only in rare AAC bit-allocation
frames (a compiler/ISA artifact, not a code-version one).
"""

import os
import struct
import subprocess
import time
from hashlib import sha256

from .branding import load_branding
from .ffmpeg import FFmpegRunner
from .rebuild import find, make, raw, table_read, table_write, tree

FAKE_MULT = 9
FAKE_SIZE = 8
DEFAULT_AUDIO_BITRATE = "346k"
EXPECTED_LAVF = "Lavf60.16.100"
FTYP_TEMPLATE = bytes.fromhex("0000001c6674797069736f6d0000020069736f6d69736f326d703431")
FREE8 = b"\x00\x00\x00\x08free"


class MethodStreamError(Exception):
    pass


def _stage(log, name, detail=None):
    if log is not None:
        log.info(f"stage: {name}" + (f": {detail}" if detail else ""))


def _stage_done(log, name, detail=None):
    if log is not None:
        log.info(f"stage done: {name}" + (f": {detail}" if detail else ""))


class _timed:
    """Context manager: emit stage begin/done with wall time and size."""

    def __init__(self, log, name, detail=None, watch_path=None):
        self.log, self.name, self.detail, self.watch = log, name, detail, watch_path

    def __enter__(self):
        _stage(self.log, self.name, self.detail)
        self.t0 = time.monotonic()
        return self

    def __exit__(self, *exc):
        parts = [f"{time.monotonic() - self.t0:.1f} s"]
        if self.watch and os.path.isfile(self.watch):
            parts.append(f"{os.path.getsize(self.watch) / 1_000_000:.1f} MB")
        _stage_done(self.log, self.name, ", ".join(parts))
        return False


def sha32(path):
    h = sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:32]


def check_ffmpeg_build(runner, log=None):
    path = runner.require("ffmpeg")
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    head = out.splitlines()[0] if out else ""
    if EXPECTED_LAVF not in out and log is not None:
        log.warning(f"ffmpeg build differs from reference lineage ({EXPECTED_LAVF} expected): '{head}'")
    return path


def run(input_path, output_path, ffmpeg=None, audio_bitrate=DEFAULT_AUDIO_BITRATE,
        branding_file=None, log=None, verbose=False):
    """Execute the full stream-copy method; returns a result dict.

    ``log`` receives stage begin/done lines (``stage:`` / ``stage done:``)
    plus ``verbose`` diagnostics for the Python-side work only; FFmpeg's own
    output is never forwarded.
    """
    dbg = log.dim if (log is not None and verbose) else (lambda _m: None)
    cfg, real_branding = load_branding(branding_file)
    ms = cfg["method_stream"]
    fake_payload = bytes.fromhex(ms["fake_payload"])
    if len(fake_payload) != FAKE_SIZE:
        raise MethodStreamError("branding fake_payload must be 8 bytes")

    with _timed(log, "hash input", watch_path=input_path):
        copyright32 = sha32(input_path)
    dbg(f"copyright tag: {copyright32}")

    runner = ffmpeg if isinstance(ffmpeg, FFmpegRunner) else FFmpegRunner(ffmpeg=ffmpeg)
    ffpath = check_ffmpeg_build(runner, log)
    dbg(f"ffmpeg: {ffpath}")
    dbg(f"ffmpeg version: {runner.version('ffmpeg')}")

    udta_entries = [[name, value.replace("{sha32}", copyright32)] for name, value in ms["udta"]]
    dbg("udta entries: " + ", ".join(name for name, _ in udta_entries))

    audio_path = output_path + ".audio.tmp.m4a"
    cmd = [ffpath, "-hide_banner", "-loglevel", "error", "-y", "-i", input_path,
           "-vn", "-c:a", "aac", "-b:a", audio_bitrate, audio_path]
    dbg("audio argv: " + " ".join(cmd))
    with _timed(log, "audio encode", f"AAC @{audio_bitrate} (FFmpeg)",
                watch_path=audio_path):
        proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.isfile(audio_path):
        if os.path.exists(audio_path):
            os.remove(audio_path)
        raise MethodStreamError(f"ffmpeg audio encode failed: {proc.stderr.strip()[-400:]}")

    base_path = output_path + ".remux.tmp.mp4"
    cmd = [ffpath, "-hide_banner", "-loglevel", "error", "-y",
           "-i", input_path, "-i", audio_path,
           "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
           "-f", "mp4", base_path]
    dbg("merge argv: " + " ".join(cmd))
    with _timed(log, "merge", "video stream copy + audio copy (FFmpeg)",
                watch_path=base_path):
        proc = subprocess.run(cmd, capture_output=True, text=True)
    os.remove(audio_path)
    if proc.returncode != 0 or not os.path.isfile(base_path):
        if os.path.exists(base_path):
            os.remove(base_path)
        raise MethodStreamError(f"ffmpeg merge failed: {proc.stderr.strip()[-400:]}")

    try:
        with _timed(log, "audio surgery",
                    f"duplicate track + {FAKE_MULT}x fake samples",
                    watch_path=output_path):
            info = surgery(base_path, output_path, udta_entries, fake_payload,
                           log=log, verbose=verbose)
    finally:
        if os.path.exists(base_path):
            os.remove(base_path)

    _stage(log, "hash output")
    t0 = time.monotonic()
    out_sha = sha256(open(output_path, "rb").read()).hexdigest()
    _stage_done(log, "hash output",
                f"{time.monotonic() - t0:.1f} s, sha256={out_sha}")
    info.update({"copyright32": copyright32, "output": output_path,
                 "sha256": out_sha,
                 "branding": "real" if real_branding else "neutral"})
    return info


# --- surgery ---------------------------------------------------------------

def _handler_of(data, trak):
    for c in trak.children:
        if c.typ == "mdia":
            for cc in c.children:
                if cc.typ == "hdlr":
                    return data[cc.payload_off + 8:cc.payload_off + 12].decode("latin1")
    return None


def _collect(data, box, typ, out):
    if box.typ == typ:
        out.append(box)
    if box.children:
        for c in box.children:
            _collect(data, c, typ, out)
    return out


def _mvhd_v1(timescale):
    """Reference writes a v1 mvhd: times zero, duration = all-F, identity matrix."""
    body = bytearray()
    body += b"\x01\x00\x00\x00"
    body += b"\x00" * 8  # creation_time
    body += b"\x00" * 8  # modification_time
    body += struct.pack(">I", timescale)
    body += b"\xff" * 8  # duration (unknown marker)
    body += struct.pack(">I", 0x00010000)  # rate 1.0
    body += struct.pack(">H", 0x0100) + b"\x00\x00"  # volume 1.0
    body += b"\x00" * 8  # reserved
    body += bytes.fromhex("00010000" "00000000" "00000000"
                          "00000000" "00010000" "00000000"
                          "00000000" "00000000" "40000000")
    body += b"\x00" * 24  # pre_defined
    body += struct.pack(">I", 4)  # next_track_ID
    return make("mvhd", bytes(body))


def _rename_hdlr(hdlr_bytes, name):
    handler = hdlr_bytes[16:20]
    body = b"\x00" * 8 + handler + b"\x00" * 12 + name.encode("latin1") + b"\x00"
    return make("hdlr", body)


def _patch_elst(elst_bytes, seg_dur):
    b = bytearray(elst_bytes)
    ver = b[8]
    if ver == 0:
        struct.pack_into(">I", b, 16, seg_dur)
        struct.pack_into(">I", b, 20, 0)  # media_time
    else:
        struct.pack_into(">Q", b, 16, seg_dur)
        struct.pack_into(">Q", b, 24, 0)
    return bytes(b)


def _patch_sbgp_count(sbgp_bytes, add):
    """Grow the roll-group sample count (the field after grouping_type+entry_count).

    Layout: header(8) verflags(4) grouping_type(4) entry_count(4)
    sample_count(4) group_description_index(4).
    """
    b = bytearray(sbgp_bytes)
    if int.from_bytes(b[8:12], "big") & 0xFFFFFF:
        raise MethodStreamError("unsupported sbgp version/flags")
    pos = b.find(b"roll")
    if pos < 0:
        raise MethodStreamError("unsupported sbgp grouping_type")
    n = struct.unpack_from(">I", b, pos + 8)[0]
    struct.pack_into(">I", b, pos + 8, n + add)
    return bytes(b)


def _ceil_ms(media_dur, media_ts, movie_ts):
    v = media_dur * movie_ts
    return -(-v // media_ts)


def _mdhd_bytes(mdhd_raw, media_dur=None):
    """Reference mdhd: zeroed times, language 'und' (0x55c4), duration rewritten.

    A raw box (including its header) is required so the result stays
    byte-comparable; ``media_dur=None`` keeps the muxed duration (video).
    """
    b = bytearray(mdhd_raw)
    ver = b[8]
    w = 8 if ver else 4
    b[12:12 + 2 * w] = b"\x00" * (2 * w)  # creation + modification
    if media_dur is not None:
        struct.pack_into(">Q" if ver else ">I", b, 16 + 2 * w, media_dur)
    b[16 + 3 * w:20 + 3 * w] = b"\x55\xc4\x00\x00"  # language + pre_defined
    return bytes(b)


def _tkhd_bytes(tkhd_raw, duration, track_id=None, alt_group_add=0):
    """Rewrite a tkhd track duration / track_ID / alternate_group."""
    b = bytearray(tkhd_raw)
    ver = b[8]
    w = 8 if ver else 4
    if track_id is not None:
        struct.pack_into(">I", b, 12 + 2 * w, track_id)
    struct.pack_into(">Q" if ver else ">I", b, 20 + 2 * w, duration)
    if alt_group_add:
        off = 30 + 3 * w
        struct.pack_into(">H", b, off, int.from_bytes(b[off:off + 2], "big") + alt_group_add)
    return bytes(b)


def _stsd_patch(stsd_bytes, es_id, bitrate):
    """Rewrite ES_ID and the AAC bitrates inside one raw stsd box.

    The reference computes the bitrates itself (see ``bitrate_for``) and writes
    them twice in esds (DecoderConfigDescriptor max/avg) and twice in btrt.
    """
    out = bytearray(stsd_bytes)
    pos = out.find(b"esds")
    if pos >= 4:
        i = pos + 8  # first descriptor tag
        if out[i] == 0x03:
            i += 1
            while out[i] & 0x80:
                i += 1
            i += 1
            struct.pack_into(">H", out, i, es_id)
            i += 3  # ES_ID(2) + ES_flags(1)
            if out[i] == 0x04:
                i += 1
                while out[i] & 0x80:
                    i += 1
                i += 6  # OTI(1) + streamType(1) + bufferSizeDB(3)
                struct.pack_into(">II", out, i, bitrate, bitrate)
    pos = out.find(b"btrt")
    if pos >= 4:
        struct.pack_into(">II", out, pos + 8, bitrate, bitrate)
    return bytes(out)


def bitrate_for(total_audio_bytes, media_dur, media_timescale, fake_bytes=0, fake_dur=0):
    """Reference bitrate = floor(bytes*8*timescale / media_duration)."""
    return (total_audio_bytes + fake_bytes) * 8 * media_timescale // (media_dur + fake_dur)


def _ser(data, box, patches):
    if id(box) in patches:
        return patches[id(box)]
    if box.children is not None:
        prefix = raw(data, box)[8:12] if box.typ == "meta" else b""
        return make(box.typ, prefix + b"".join(_ser(data, c, patches) for c in box.children))
    return raw(data, box)


def _ser_moov(data, moov, patches, extra_trak=None, udta_bytes=None):
    parts = []
    for c in moov.children:
        if c.typ == "udta" and udta_bytes is not None:
            continue  # replaced with our own, appended last
        parts.append(_ser(data, c, patches))
    if extra_trak is not None:
        parts.append(extra_trak)
    if udta_bytes is not None:
        parts.append(udta_bytes)
    return make("moov", b"".join(parts))


def _expand_samples(data, stbl, payload_off):
    """Per-sample (size, relative offset) in decode order + stts start times."""
    stsc = table_read("stsc", raw(data, _collect(data, stbl, "stsc", [])[0])[8:])["ents"]
    stco = table_read("stco", raw(data, _collect(data, stbl, "stco", [])[0])[8:])["offs"]
    stsz = table_read("stsz", raw(data, _collect(data, stbl, "stsz", [])[0])[8:])["sizes"]
    per_chunk = []
    for k, (firstc, spc, _si) in enumerate(stsc):
        nxt = stsc[k + 1][0] if k + 1 < len(stsc) else len(stco) + 1
        per_chunk.extend([spc] * (nxt - firstc))
    if len(per_chunk) != len(stco) or sum(per_chunk) != len(stsz):
        raise MethodStreamError("audio/video chunk map inconsistent")
    # samples inside a chunk are contiguous
    sizes, offs = [], []
    si = 0
    for nsamp, off in zip(per_chunk, stco):
        pos = off - payload_off
        for _ in range(nsamp):
            sizes.append(stsz[si])
            offs.append(pos)
            pos += stsz[si]
            si += 1
    stts = table_read("stts", raw(data, _collect(data, stbl, "stts", [])[0])[8:])["ents"]
    times, cur = [], 0
    for cnt, delta in stts:
        times.extend(cur + delta * k for k in range(cnt))
        cur += delta * cnt
    return sizes, offs, times


def _timescale_of(data, trak):
    d = raw(data, _collect(data, trak, "mdhd", [])[0])  # full box, header included
    ver = d[8]
    off = 20 if ver == 0 else 28  # timescale follows creation+modification
    return int.from_bytes(d[off:off + 4], "big")


def _stsc_entries(chunk_nsamples):
    """Compress a per-chunk sample-count list into stsc entries."""
    if not chunk_nsamples:
        return [(1, 1, 1)]
    ents = [(1, chunk_nsamples[0], 1)]
    for k, n in enumerate(chunk_nsamples[1:], start=2):
        if n != ents[-1][1]:
            ents.append((k, n, 1))
    return ents


def surgery(base_path, out_path, udta_entries, fake_payload, log=None, verbose=False):
    """Rebuild the container: custom interleave, audio duplication, fake tail, mdta.

    The reference layout is fully re-materialized: chunks are ordered by
    decode-time merge of the two tracks (video wins ties), every audio sample
    gets its own chunk, and each audio chunk is immediately followed by its
    duplicate (track 3 offset = track 2 offset + sample size).  A contiguous
    fake payload (9N x 8 bytes) is appended; track 3 stco is intentionally
    not extended for it.
    """
    dbg = log.dim if (log is not None and verbose) else (lambda _m: None)
    from .rebuild import udta_mdta
    data = open(base_path, "rb").read()
    tops = tree(data, 0, len(data))
    moov = find(tops, "moov")
    mdat = find(tops, "mdat")
    if moov is None or mdat is None:
        raise MethodStreamError("remux output lacks moov/mdat")
    payload_old_off = mdat.payload_off
    dbg(f"container read: {len(data) / 1_000_000:.1f} MB, "
        f"top-level: " + " ".join(f"{b.typ}({b.size})" for b in tops))

    traks = [c for c in moov.children if c.typ == "trak"]
    kinds = [_handler_of(data, t) for t in traks]
    if kinds.count("soun") != 1 or kinds.count("vide") != 1 or len(traks) != 2:
        raise MethodStreamError(f"expected exactly vide+soun traks, got {kinds}")
    video = traks[kinds.index("vide")]
    audio = traks[kinds.index("soun")]
    vstbl = _collect(data, video, "stbl", [])[0]
    astbl = _collect(data, audio, "stbl", [])[0]

    vsz, voff, vtimes = _expand_samples(data, vstbl, payload_old_off)
    asz, aoff, atimes = _expand_samples(data, astbl, payload_old_off)
    vts = _timescale_of(data, video)
    ats = _timescale_of(data, audio)
    if sum(vsz) + sum(asz) != mdat.size - mdat.hdr:
        raise MethodStreamError("unexpected padding in remux mdat")
    n_real = len(asz)
    n_fake = n_real * FAKE_MULT
    dbg(f"tracks: video {len(vsz)} samples, audio {n_real} samples; "
        f"dup audio track -> {n_fake} fake size-{FAKE_SIZE} samples")

    # --- plan chunk layout (dts merge, video wins ties, audio single) ---
    order = []  # ("V", start_idx, count) / ("A", idx)
    i = j = 0
    nv, na = len(vsz), len(asz)
    while i < nv or j < na:
        take_v = j >= na or (i < nv and vtimes[i] * ats <= atimes[j] * vts)
        if take_v:
            if order and order[-1][0] == "V":
                order[-1] = ("V", order[-1][1], order[-1][2] + 1)
            else:
                order.append(("V", i, 1))
            i += 1
        else:
            order.append(("A", j, 1))
            j += 1

    payload = bytearray()
    v_chunk_sizes = []
    v_rel, a2_rel, a3_rel = [], [], []
    for kind, start, count in order:
        if kind == "V":
            v_rel.append(len(payload))
            v_chunk_sizes.append(count)
            for s in range(start, start + count):
                payload += data[payload_old_off + voff[s]: payload_old_off + voff[s] + vsz[s]]
        else:
            a2_rel.append(len(payload))
            chunk = data[payload_old_off + aoff[start]: payload_old_off + aoff[start] + asz[start]]
            payload += chunk + chunk
            a3_rel.append(a2_rel[-1] + asz[start])
    payload += fake_payload * n_fake
    # The fake batch continues the last audio chunk when that chunk is physically
    # last; otherwise a trailing video chunk forces the fakes into a new chunk.
    fake_own_chunk = order[-1][0] != "A"
    fake_rel = len(payload) - n_fake * FAKE_SIZE
    dbg(f"interleave plan: {len(order)} runs, fake_own_chunk={fake_own_chunk}; "
        f"payload rebuilt {len(payload) / 1_000_000:.1f} MB "
        f"(incl. {n_fake * FAKE_SIZE} B fake tail)")

    stats = {}

    def build(payload_start):
        """payload_start = absolute file offset of the first mdat payload byte."""
        patches = {}
        # video: stsc + stco rewritten to our layout
        vstco_box = _collect(data, vstbl, "stco", [])[0]
        dv = table_read("stco", raw(data, vstco_box)[8:])
        dv["offs"] = [payload_start + r for r in v_rel]
        patches[id(vstco_box)] = table_write("stco", dv)
        vstsc_box = _collect(data, vstbl, "stsc", [])[0]
        dsc = table_read("stsc", raw(data, vstsc_box)[8:])
        dsc["ents"] = _stsc_entries(v_chunk_sizes)
        patches[id(vstsc_box)] = table_write("stsc", dsc)
        # audio track2: uniform stts, single-sample chunks, our offsets
        astco_box = _collect(data, astbl, "stco", [])[0]
        da = table_read("stco", raw(data, astco_box)[8:])
        da["offs"] = [payload_start + r for r in a2_rel]
        patches[id(astco_box)] = table_write("stco", da)
        astsc_box = _collect(data, astbl, "stsc", [])[0]
        dasc = table_read("stsc", raw(data, astsc_box)[8:])
        dasc["ents"] = [(1, 1, 1)]
        patches[id(astsc_box)] = table_write("stsc", dasc)
        astts_box = _collect(data, astbl, "stts", [])[0]
        dts = table_read("stts", raw(data, astts_box)[8:])
        unit_delta = dts["ents"][0][1]
        dts["ents"] = [(n_real, unit_delta)]
        patches[id(astts_box)] = table_write("stts", dts)
        # movie header: reference rewrites it as a v1 template
        mvhd = find(moov.children, "mvhd")
        _mvb = raw(data, mvhd)[8:]
        _mvw = 8 if _mvb[0] == 1 else 4
        mvtimescale = int.from_bytes(_mvb[4 + 2 * _mvw:8 + 2 * _mvw], "big")
        patches[id(mvhd)] = _mvhd_v1(mvtimescale)
        # audio mdhd: zero times, uniform duration, 'und' language
        media2 = n_real * unit_delta
        media3 = media2 + n_fake
        dur2 = _ceil_ms(media2, ats, mvtimescale)
        dur3 = _ceil_ms(media3, ats, mvtimescale)
        br2 = bitrate_for(sum(asz), media2, ats)
        br3 = bitrate_for(sum(asz), media2, ats, n_fake * FAKE_SIZE, n_fake)
        stats.update({"movie_timescale": mvtimescale, "track_dur2": dur2,
                      "track_dur3": dur3, "bitrate2": br2, "bitrate3": br3})
        amd = _collect(data, audio, "mdhd", [])[0]
        patches[id(amd)] = _mdhd_bytes(raw(data, amd), media2)
        # audio tkhd duration (creation time stays as the reference keeps it)
        atkhd = _collect(data, audio, "tkhd", [])[0]
        patches[id(atkhd)] = _tkhd_bytes(raw(data, atkhd), dur2)
        # audio edit list: full duration, media_time 0
        for e in _collect(data, audio, "elst", []):
            patches[id(e)] = _patch_elst(raw(data, e), dur2)
        # video mdhd: same header treatment, media duration stays as muxed
        vmd = _collect(data, video, "mdhd", [])[0]
        patches[id(vmd)] = _mdhd_bytes(raw(data, vmd))
        # audio bitrates are recomputed by the reference, per duplicated track
        for box in _collect(data, audio, "stsd", []):
            patches[id(box)] = _stsd_patch(raw(data, box), 2, br2)
        # handler names per reference template
        for t in traks:
            h = _collect(data, t, "hdlr", [])[0]
            nm = {"vide": "VideoHandler", "soun": "SoundHandler"}.get(_handler_of(data, t))
            if nm:
                patches[id(h)] = _rename_hdlr(raw(data, h), nm)
        # duplicated audio trak (track 3)
        cp = dict(patches)
        cp[id(amd)] = _mdhd_bytes(raw(data, amd), media3)
        cp[id(atkhd)] = _tkhd_bytes(raw(data, atkhd), dur3, track_id=3, alt_group_add=1)
        for e in _collect(data, audio, "elst", []):
            cp[id(e)] = _patch_elst(raw(data, e), dur3)
        for box in _collect(data, audio, "stsd", []):
            cp[id(box)] = _stsd_patch(raw(data, box), 3, br3)
        for box in _collect(data, audio, "sbgp", []):
            cp[id(box)] = _patch_sbgp_count(raw(data, box), n_fake)
        d3 = table_read("stco", raw(data, astco_box)[8:])
        d3["offs"] = [payload_start + r for r in a3_rel]
        if fake_own_chunk:
            d3["offs"].append(payload_start + fake_rel)
        cp[id(astco_box)] = table_write("stco", d3)
        astsz_box = _collect(data, astbl, "stsz", [])[0]
        dsz = table_read("stsz", raw(data, astsz_box)[8:])
        dsz["sizes"].extend([FAKE_SIZE] * n_fake)
        cp[id(astsz_box)] = table_write("stsz", dsz)
        dts3 = dict(dts)
        dts3["ents"] = [(n_real, unit_delta), (n_fake, 1)]
        cp[id(astts_box)] = table_write("stts", dts3)
        dasc3 = dict(dasc)
        if fake_own_chunk:
            dasc3["ents"] = [(1, 1, 1), (n_real + 1, n_fake, 1)]
        else:
            dasc3["ents"] = [(1, 1, 1), (n_real, n_fake + 1, 1)]
        cp[id(astsc_box)] = table_write("stsc", dasc3)
        extra = _ser(data, audio, cp)
        udta_bytes = udta_mdta([(n, v) for n, v in udta_entries])
        return _ser_moov(data, moov, patches, extra_trak=extra, udta_bytes=udta_bytes)

    # two passes: the moov size does not depend on the offsets it stores
    m1 = build(0)
    dbg(f"audio bitrate track2/track3: {stats.get('bitrate2')}/{stats.get('bitrate3')} bit/s; "
        f"durations {stats.get('track_dur2')}/{stats.get('track_dur3')} ms")
    payload_start = len(FTYP_TEMPLATE) + len(m1) + len(FREE8) + 8
    moov_new = build(payload_start)
    if len(moov_new) != len(m1):
        raise MethodStreamError("moov size unstable between passes")

    if len(payload) + 8 < 2 ** 32:
        mdat_new = make("mdat", bytes(payload))
    else:
        mdat_new = struct.pack(">IQ", 1, 16 + len(payload)) + b"mdat" + bytes(payload)

    with open(out_path, "wb") as fh:
        fh.write(FTYP_TEMPLATE)
        fh.write(moov_new)
        fh.write(FREE8)
        fh.write(mdat_new)

    return {"n_real": n_real, "n_fake": n_fake, "payload_start": payload_start,
            **stats}
