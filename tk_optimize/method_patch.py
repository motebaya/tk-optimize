"""Method 3 — encode-patch lineage (CLI name: ``shadow``).

Formula (reverse-engineered from observed reference outputs):

  input -> ffmpeg-8.0 encode pass:
            video: libx264, preset faster, forced yuv420p (10-bit HDR input
                   is converted to 8-bit, losing HDR in the reference line),
                   ABR bitrate, odd -r fraction (990899/16517 observed),
                   threads 4, preset-default x264 settings (weightp=1)
            audio: native AAC 128k  (byte-identical to reference with ffmpeg 8.0)
          -> ffmpeg stream-copy remux (-c copy +faststart) that re-packs the
             chunk interleave into the reference layout
          -> container patch:
               top level ftyp(32) + free(8) + moov + mdat
               mvhd/mdhd/tkhd creation+modification times zeroed
               mdhd language set to "und" on both tracks
               stsd btrt max/avg recomputed as 8*real_bytes*timescale//duration
               handlers renamed VideoHandler / SoundHandler
               video stts split so the last real sample gets delta 1
               round(17/3 * n_video_samples) fake size-8 samples appended to
               the VIDEO track, all pointing at one shared 8-byte tail region
               (fake payload materialized as the final 8 bytes of mdat;
               stsc gains a (real_chunks+1, 1, 1) entry; stts untouched by fakes)
               x264 SEI banner trimmed to the reference tag (length fields fixed)

Verified: both reference fixtures are reproduced whole-file byte-identically
and deterministically across repeat runs when the encode+remux run on the same
ffmpeg 8.0 (Lavf62.3.100) build:
  SDR  sha256 c4e829372b7a87dab5779e94dc2b07552a83d3f225711f93300d7e09a472bb32
  HDR10+ sha256 9430093987251d1bc84b6194078506e9df2e98e9c246b59311b2a86d00d4c648
The encode stage must force ``-pix_fmt yuv420p`` (the reference downsamples
10-bit HDR to 8-bit) and use preset-default x264 settings (weightp=1); an
explicit weightp=2 override makes the bitstream diverge.  The intermediate
``-c copy +faststart`` remux re-packs the chunk interleave into the layout the
reference uses; surgery alone (encode -> patch) does not reproduce it.
"""

import os
import re
import struct
import subprocess
from hashlib import sha256

from .branding import load_branding
from .ffmpeg import FFmpegRunner
from .method_stream import _stage, _stage_done, _timed, sha32
from .rebuild import find, make, raw, table_read, table_write, tree

EXPECTED_LAVF_PATCH = "Lavf62.3.100"
DEFAULT_VIDEO_BITRATE = "34949k"
DEFAULT_AUDIO_BITRATE = "128k"
DEFAULT_FPS = "990899/16517"
FAKE_SIZE = 8
FREE8 = b"\x00\x00\x00\x08free"

_BANNER = re.compile(rb"(x264 - core \d+) r[0-9a-zA-Z.]+ [0-9a-fA-F]+( - H\.264)")


class MethodPatchError(Exception):
    pass


def _collect(boxes, typ, stop_types=()):
    out = []

    def walk(bs):
        for b in bs:
            if b.typ == typ:
                out.append(b)
            if b.children is not None and b.typ not in stop_types:
                walk(b.children)
    walk(boxes)
    return out


def _track_kind(data, trak):
    for h in _collect([trak], "hdlr"):
        p = data[h.payload_off:h.end]
        return p[8:12].decode("latin1"), p[24:].rstrip(b"\x00").decode("latin1")
    return None, None


def fake_count(n_real):
    """Lineage rule: forged video samples = round(17/3 * real sample count)."""
    return int(round(n_real * 17 / 3))


def patch_banner(sample):
    """Trim the x264 SEI banner in one access unit.  Returns (bytes, delta)."""
    delta_total = 0
    pos = 0
    parts = []
    while pos + 4 <= len(sample):
        nlen = struct.unpack(">I", sample[pos:pos + 4])[0]
        nal = sample[pos + 4:pos + 4 + nlen]
        if not nal or (nal[0] & 0x1F) != 6 or b"x264 - core " not in nal:
            parts.append(sample[pos:pos + 4 + nlen])
            pos += 4 + nlen
            continue
        m = _BANNER.search(nal)
        if m is None:
            parts.append(sample[pos:pos + 4 + nlen])
            pos += 4 + nlen
            continue
        new_nal = nal[:m.start()] + m.group(1) + m.group(2) + nal[m.end():]
        d = len(nal) - len(new_nal)
        # SEI payload size field: type byte 0x05 then 0xFF-continuation bytes.
        j = new_nal.index(b"\x05", 1) + 1
        while new_nal[j] == 0xFF:
            j += 1
        assert new_nal[j] - d >= 0 and new_nal[j] - d < 0xFF, "banner SEI size underflow"
        new_nal = new_nal[:j] + bytes([new_nal[j] - d]) + new_nal[j + 1:]
        parts.append(struct.pack(">I", nlen - d) + new_nal)
        delta_total += d
        pos += 4 + nlen
    if pos != len(sample):
        raise MethodPatchError("access unit walk did not consume the sample")
    return b"".join(parts), delta_total


def surgery(raw_path, out_path, fake_payload, log=None, verbose=False):
    dbg = log.dim if (log is not None and verbose) else (lambda _m: None)
    data = open(raw_path, "rb").read()
    tops = tree(data, 0, len(data))
    ftyp = find(tops, "ftyp")
    moov = find(tops, "moov")
    mdat = find(tops, "mdat")
    payload_old = mdat.payload_off
    payload = data[payload_old:mdat.end]
    dbg(f"container read: {len(data) / 1_000_000:.1f} MB, "
        f"top-level: " + " ".join(f"{b.typ}({b.size})" for b in tops))

    traks = [c for c in moov.children if c.typ == "trak"]
    tracks = []
    for tr in traks:
        kind, _name = _track_kind(data, tr)
        stbl = _collect([tr], "stbl")[0]
        tables = {b.typ: table_read(b.typ, data[b.payload_off:b.end])
                  for b in stbl.children if b.typ in ("stsc", "stco", "stsz")}
        tracks.append({"rec": tr, "kind": kind, "stbl": stbl, "tables": tables})

    kinds = [t["kind"] for t in tracks]
    if "vide" not in kinds or "soun" not in kinds:
        raise MethodPatchError("expected one video and one audio track")
    video = tracks[kinds.index("vide")]

    # --- chunk walk: expand every track into (first_sample, old_off, sizes) ---
    # The encode output stores the CHUNK index in stsc.first_sample (same
    # producer fingerprint as the reference files); expand accordingly.
    def chunks_of(t):
        tbl = t["tables"]
        sizes, offs, stsc = tbl["stsz"]["sizes"], tbl["stco"]["offs"], tbl["stsc"]["ents"]
        npc_list = []
        for k, (fc, npc, _si) in enumerate(stsc):
            nxt = stsc[k + 1][0] if k + 1 < len(stsc) else len(offs) + 1
            npc_list.extend([npc] * (nxt - fc))
        if len(npc_list) != len(offs) or sum(npc_list) != len(sizes):
            raise MethodPatchError("inconsistent chunk layout in encode output")
        chunks = []
        s = 0
        for ci, npc in enumerate(npc_list):
            chunks.append((s + 1, offs[ci], sizes[s:s + npc]))
            s += npc
        return chunks

    allc = []
    for t in tracks:
        for (s, off, szs) in chunks_of(t):
            allc.append((off, t, s, szs))
    allc.sort(key=lambda x: x[0])
    span = sum(sum(c[3]) for c in allc)
    if span != len(payload):
        raise MethodPatchError(f"unexpected padding in remux mdat ({span} vs {len(payload)})")
    dbg(f"chunk walk: {len(allc)} chunks, {len(payload) / 1_000_000:.1f} MB payload, no padding")

    # --- rebuild payload (banner-patched video chunks) ---
    out_payload = bytearray()
    banner_hits, banner_bytes = 0, 0
    for ti, t in enumerate(tracks):
        t["ti"] = ti
    for off, t, s, szs in allc:
        base = off - payload_old
        chunk = payload[base:base + sum(szs)]
        if t["kind"] == "vide":
            pos = 0
            new_sizes = []
            buf = bytearray()
            for z in szs:
                sample = chunk[pos:pos + z]
                patched, d = patch_banner(sample)
                if d:
                    banner_hits += 1
                    banner_bytes += d
                buf += patched
                new_sizes.append(z - d)
                pos += z
            t.setdefault("chunks", []).append((s, len(out_payload), buf, new_sizes))
        else:
            t.setdefault("chunks", []).append((s, len(out_payload), chunk, szs))
        out_payload += (buf if t["kind"] == "vide" else chunk)
    dbg(f"SEI banner trim: {banner_hits} patched, {banner_bytes} bytes removed")

    # per-track rebuilt tables
    for t in tracks:
        t["chunks"].sort(key=lambda c: c[1])
        offs = [c[1] for c in t["chunks"]]
        sizes = []
        for c in t["chunks"]:
            sizes.extend(c[3])
        t["new_stco"] = offs
        t["new_sizes"] = sizes
        t["real_bytes"] = sum(sizes)
        if t["kind"] != "vide":
            continue
        n_real = len(sizes)
        n_fake = fake_count(n_real)
        t["n_fake"] = n_fake
        t["new_sizes"] = sizes + [FAKE_SIZE] * n_fake
    for t in tracks:
        extra = f", + {t['n_fake']} fake size-{FAKE_SIZE} samples" if t["kind"] == "vide" else ""
        dbg(f"track {t['kind']}: {len(t['new_sizes']) - t.get('n_fake', 0)} real samples "
            f"({t['real_bytes'] / 1_000_000:.1f} MB), {len(t['new_stco'])} chunks{extra}")

    fake_tail_rel = len(out_payload)
    out_payload += fake_payload
    assert len(fake_payload) == FAKE_SIZE

    def btrt_patch(stsd_bytes, value):
        pos, hits = 0, 0
        out = bytearray(stsd_bytes)
        while True:
            j = out.find(b"btrt", pos)
            if j < 0:
                break
            if struct.unpack(">I", out[j - 4:j])[0] == 20:
                out[j + 8:j + 16] = struct.pack(">II", value, value)
                hits += 1
            pos = j + 4
        if hits != 1:
            raise MethodPatchError(f"expected exactly one btrt in stsd, found {hits}")
        return bytes(out)

    # --- build new top-level ---
    def build(payload_start):
        moov_bytes = bytearray()
        for b in moov.children:
            if b.typ == "mvhd":
                p = bytearray(data[b.payload_off:b.end])
                p[4:12] = b"\x00" * 8
                moov_bytes += make("mvhd", bytes(p))
            elif b.typ == "trak":
                kind, _ = _track_kind(data, b)
                t = tracks[kinds.index(kind)]
                stbl = t["stbl"]
                mdhd_b = _collect([b], "mdhd")[0]
                mp = data[mdhd_b.payload_off:mdhd_b.end]
                ts, dur = struct.unpack(">II", mp[12:20])
                btrt_value = 8 * t["real_bytes"] * ts // dur
                t["btrt_value"] = btrt_value
                stco = {"vf": t["tables"]["stco"]["vf"],
                        "offs": [payload_start + o for o in t["new_stco"]]}
                if t["kind"] == "vide":
                    stco["offs"] += [payload_start + fake_tail_rel] * t["n_fake"]
                stsz = {"vf": t["tables"]["stsz"]["vf"], "unit": 0,
                        "sizes": t["new_sizes"]}
                stsc_ents = list(t["tables"]["stsc"]["ents"])
                if t["kind"] == "vide":
                    stsc_ents.append((len(t["new_stco"]) + 1, 1, 1))
                stsc = {"vf": t["tables"]["stsc"]["vf"], "ents": stsc_ents}
                stts_b = None
                for tb in stbl.children:
                    if tb.typ == "stts":
                        d = table_read("stts", data[tb.payload_off:tb.end])
                        if t["kind"] == "vide" and len(d["ents"]) == 1 and d["ents"][0][0] > 1:
                            c, dv = d["ents"][0]
                            d["ents"] = [(c - 1, dv), (1, 1)]
                        stts_b = table_write("stts", d)
                new_tbl = {"stco": table_write("stco", stco),
                           "stsz": table_write("stsz", stsz),
                           "stsc": table_write("stsc", stsc)}
                body = bytearray()
                for cb in stbl.children:
                    if cb.typ in new_tbl:
                        body += new_tbl[cb.typ]
                    elif cb.typ == "stts":
                        body += stts_b
                    elif cb.typ == "stsd":
                        body += btrt_patch(raw(data, cb), btrt_value)
                    else:
                        body += raw(data, cb)
                stbl_bytes = make("stbl", bytes(body))
                trk_body = bytearray()
                for bb in b.children:
                    if bb.typ == "tkhd":
                        p = bytearray(data[bb.payload_off:bb.end])
                        p[4:12] = b"\x00" * 8
                        trk_body += make("tkhd", bytes(p))
                    elif bb.typ == "mdia":
                        md_body = bytearray()
                        for mb in bb.children:
                            if mb.typ == "mdhd":
                                p = bytearray(data[mb.payload_off:mb.end])
                                p[4:12] = b"\x00" * 8
                                p[20:22] = b"\x55\xc4"  # language "und"
                                md_body += make("mdhd", bytes(p))
                            elif mb.typ == "hdlr":
                                p = bytearray(data[mb.payload_off:mb.end])
                                name = {"vide": b"VideoHandler\x00",
                                        "soun": b"SoundHandler\x00"}[kind]
                                md_body += make("hdlr", bytes(p[:24]) + name)
                            elif mb.typ == "minf":
                                mf_body = bytearray()
                                for mfb in mb.children:
                                    if mfb.typ == "stbl":
                                        mf_body += stbl_bytes
                                    else:
                                        mf_body += raw(data, mfb)
                                md_body += make("minf", bytes(mf_body))
                            else:
                                md_body += raw(data, mb)
                        trk_body += make("mdia", bytes(md_body))
                    else:
                        trk_body += raw(data, bb)
                moov_bytes += make("trak", bytes(trk_body))
            else:
                moov_bytes += raw(data, b)
        mdat_size = 8 + len(out_payload)
        return bytes(moov_bytes), mdat_size

    m1, _ = build(0)
    for t in tracks:
        dbg(f"btrt {t['kind']}: {t.get('btrt_value')} bit/s (stsd recomputed)")
    payload_start = ftyp.size + len(FREE8) + 8 + len(m1) + 8
    moov_bytes, mdat_size = build(payload_start)
    moov_box = make("moov", moov_bytes)
    total = ftyp.size + len(FREE8) + len(moov_box) + mdat_size
    mdat_box_hdr = struct.pack(">I", mdat_size) + b"mdat"

    with open(out_path, "wb") as fh:
        fh.write(raw(data, ftyp))
        fh.write(FREE8)
        fh.write(moov_box)
        fh.write(mdat_box_hdr)
        fh.write(bytes(out_payload))

    v = video
    stats = {
        "video_samples": len(v["new_sizes"]) - v["n_fake"],
        "video_fakes": v["n_fake"],
        "audio_samples": len(tracks[kinds.index("soun")]["new_sizes"]),
        "payload_start": payload_start,
        "total_size": total,
    }
    return stats


def check_ffmpeg_build(runner, log=None):
    path = runner.require("ffmpeg")
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    head = out.splitlines()[0] if out else ""
    if EXPECTED_LAVF_PATCH not in out and log is not None:
        log.warning(f"ffmpeg build differs from reference lineage ({EXPECTED_LAVF_PATCH} expected): '{head}'")
    return path


def run(input_path, output_path, ffmpeg=None, video_bitrate=DEFAULT_VIDEO_BITRATE,
        audio_bitrate=DEFAULT_AUDIO_BITRATE, fps=DEFAULT_FPS,
        branding_file=None, log=None, verbose=False):
    """Execute the full encode-patch method; returns a result dict.

    ``log`` receives stage begin/done lines (``stage:`` / ``stage done:``)
    plus ``verbose`` diagnostics for the Python-side work only; FFmpeg's own
    output is never forwarded.
    """
    logger = log
    dbg = logger.dim if (logger is not None and verbose) else (lambda _m: None)
    cfg, real_branding = load_branding(branding_file)
    fake_payload = bytes.fromhex(cfg["method_stream"]["fake_payload"])
    if len(fake_payload) != FAKE_SIZE:
        raise MethodPatchError("branding fake_payload must be 8 bytes")

    with _timed(logger, "hash input", watch_path=input_path):
        copyright32 = sha32(input_path)
    dbg(f"copyright tag: {copyright32}")

    runner = ffmpeg if isinstance(ffmpeg, FFmpegRunner) else FFmpegRunner(ffmpeg=ffmpeg)
    ffpath = check_ffmpeg_build(runner, logger)
    dbg(f"ffmpeg: {ffpath}")
    dbg(f"ffmpeg version: {runner.version('ffmpeg')}")

    tmp_path = output_path + ".encode.tmp.mp4"
    cmd = [ffpath, "-hide_banner", "-loglevel", "error", "-y", "-i", input_path,
           "-c:v", "libx264", "-preset", "faster", "-pix_fmt", "yuv420p",
           "-b:v", video_bitrate, "-threads", "4", "-r", fps,
           "-c:a", "aac", "-b:a", audio_bitrate,
           "-movflags", "+faststart", tmp_path]
    dbg("encode argv: " + " ".join(cmd))
    with _timed(logger, "encode",
                f"libx264 @{video_bitrate} r={fps} + AAC @{audio_bitrate} (FFmpeg)",
                watch_path=tmp_path):
        proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.isfile(tmp_path):
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise MethodPatchError(f"ffmpeg encode failed: {proc.stderr.strip()[-400:]}")

    remux_path = output_path + ".remux.tmp.mp4"
    try:
        cmd = [ffpath, "-hide_banner", "-loglevel", "error", "-y", "-i", tmp_path,
               "-c", "copy", "-movflags", "+faststart", remux_path]
        dbg("remux argv: " + " ".join(cmd))
        with _timed(logger, "remux",
                    "stream copy + faststart, re-packs chunk interleave (FFmpeg)",
                    watch_path=remux_path):
            proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not os.path.isfile(remux_path):
            raise MethodPatchError(f"ffmpeg remux failed: {proc.stderr.strip()[-400:]}")

        with _timed(logger, "container patch",
                    "fake samples + metadata surgery", watch_path=output_path):
            info = surgery(remux_path, output_path, fake_payload,
                           log=logger, verbose=verbose)
    finally:
        for t in (tmp_path, remux_path):
            if os.path.exists(t):
                os.remove(t)

    _stage(logger, "hash output")
    h = sha256()
    with open(output_path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    _stage_done(logger, "hash output", f"{os.path.getsize(output_path) / 1_000_000:.1f} MB")
    info.update({"output": output_path, "sha256": h.hexdigest(),
                 "copyright32": copyright32, "branding": "real" if real_branding else "neutral"})
    return info

