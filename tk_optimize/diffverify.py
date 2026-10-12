"""Structured binary/container diff between a produced file and a reference.

Used whenever whole-file SHA-256 identity is NOT achieved: pinpoints the
exact boxes, tables and sample runs that differ (per the investigation
acceptance policy: never dismiss a hash mismatch without evidence).
"""

import struct
from hashlib import sha256

from .rebuild import find, raw, table_read, tree


def file_sha256(path):
    h = sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _track_view(data, tops):
    moov = find(tops, "moov")
    out = []
    if moov is None:
        return out
    for t in moov.children:
        if t.typ != "trak":
            continue
        info = {"handler": None, "tables": {}, "stsd_sha": None}
        stack = list(t.children)
        while stack:
            b = stack.pop()
            if b.typ == "hdlr":
                info["handler"] = data[b.payload_off + 8:b.payload_off + 12].decode("latin1")
            elif b.typ == "stsd":
                info["stsd_sha"] = sha256(raw(data, b)).hexdigest()[:16]
            elif b.typ in ("stsz", "stts", "stsc", "stco", "co64", "stss"):
                info["tables"][b.typ] = table_read(b.typ, raw(data, b)[8:])
            elif b.children:
                stack.extend(b.children)
        out.append(info)
    return out


def _diff_tables(a, b, label, report):
    for typ in sorted(set(a) | set(b)):
        da, db = a.get(typ), b.get(typ)
        if da is None or db is None:
            report.append(f"trak[{label}].{typ}: present only in {'ours' if db is None else 'ref'}")
            continue
        if typ in ("stsz",):
            sa, sb = da["sizes"], db["sizes"]
            if sa == sb:
                continue
            diffs = [i for i, (x, y) in enumerate(zip(sa, sb)) if x != y]
            report.append(f"trak[{label}].stsz: n {len(sa)}vs{len(sb)} "
                          f"firstdiff@{diffs[0] if diffs else 'len'} count_diff={len(diffs)}")
        elif typ == "stts":
            if da["ents"] != db["ents"]:
                report.append(f"trak[{label}].stts: {da['ents'][:3]}... vs {db['ents'][:3]}... "
                              f"(n {len(da['ents'])} vs {len(db['ents'])})")
        elif typ == "stsc":
            if da["ents"] != db["ents"]:
                diffs = [i for i, (x, y) in enumerate(zip(da["ents"], db["ents"])) if x != y]
                where = diffs[0] if diffs else min(len(da['ents']), len(db['ents']))
                report.append(f"trak[{label}].stsc: first entry diff @ {where}: "
                              f"{da['ents'][where] if where < len(da['ents']) else None} vs "
                              f"{db['ents'][where] if where < len(db['ents']) else None}")
        elif typ in ("stco", "co64"):
            oa, ob = da["offs"], db["offs"]
            if oa == ob:
                continue
            d = {i: (y - x) for i, (x, y) in enumerate(zip(oa, ob)) if x != y}
            uniq = sorted(set(d.values()))
            report.append(f"trak[{label}].{typ}: n {len(oa)} vs {len(ob)}, "
                          f"delta_uniq={uniq[:3]}{'...' if len(uniq) > 3 else ''}")
        elif typ == "stss":
            if da["nums"] != db["nums"]:
                report.append(f"trak[{label}].stss differs")


def _deep_boxes(adata, abox, bdata, bbox, path, report, limit=40):
    """Report every leaf box that differs inside the two parsed subtrees."""
    if len(report) >= limit:
        return
    ba = raw(adata, abox)
    bb = raw(bdata, bbox)
    if ba == bb:
        return
    if abox.children is not None and bbox.children is not None:
        ca, cb = abox.children, bbox.children
        ta = [x.typ for x in ca]
        tb = [x.typ for x in cb]
        if ta != tb:
            report.append(f"{path}: child sequence {ta} vs {tb}")
            return
        for x, y in zip(ca, cb):
            _deep_boxes(adata, x, bdata, y, f"{path}/{x.typ}", report, limit)
        return
    # leaf vs leaf (or shape mismatch): pinpoint
    n = min(len(ba), len(bb))
    first = next((i for i in range(n) if ba[i] != bb[i]), n)
    if len(ba) <= 256:
        report.append(f"{path}[{abox.typ}]: size {len(ba)} vs {len(bb)}, firstdiff@{first}, "
                      f"ours={ba[max(0,first):first+16].hex()} ref={bb[max(0,first):first+16].hex()}")
    else:
        report.append(f"{path}[{abox.typ}]: size {len(ba)} vs {len(bb)}, firstdiff@{first}")


def _diff_payload(a, b, mda, mdb, report, block=1 << 16):
    pa_start, pb_start = mda.payload_off, mdb.payload_off
    n = min(mda.size - mda.hdr, mdb.size - mdb.hdr)
    diffs = 0
    first_at = None
    for off in range(0, n, block):
        seg = min(block, n - off)
        sa = a[pa_start + off:pa_start + off + seg]
        sb = b[pb_start + off:pb_start + off + seg]
        if sa != sb:
            if first_at is None:
                first_at = next((off + i for i in range(seg) if sa[i] != sb[i]), None)
            diffs += seg
    if first_at is not None:
        report.append(f"mdat payload: differs (~{diffs} bytes in first non-matching blocks), "
                      f"first @ payload offset {first_at}")


def diff(ours_path, ref_path, max_report=60):
    """Return list of human-readable difference lines; empty list = identical."""
    a = open(ours_path, "rb").read()
    b = open(ref_path, "rb").read()
    report = []
    if sha256(a).hexdigest() == sha256(b).hexdigest():
        return []
    if len(a) != len(b):
        report.append(f"file size: ours {len(a)} vs ref {len(b)} ({len(a) - len(b):+d})")
    ta = tree(a, 0, len(a))
    tb = tree(b, 0, len(b))
    la = [(x.typ, x.size) for x in ta]
    lb = [(x.typ, x.size) for x in tb]
    if la != lb:
        report.append(f"top-level boxes: ours {la} vs ref {lb}")
    moov_a = find(ta, "moov")
    moov_b = find(tb, "moov")
    if moov_a and moov_b and raw(a, moov_a) != raw(b, moov_b):
        _deep_boxes(a, moov_a, b, moov_b, "moov", report)
        va = _track_view(a, ta)
        vb = _track_view(b, tb)
        for i, (x, y) in enumerate(zip(va, vb)):
            label = f"{i}:{x['handler'] or '?'}"
            if x["stsd_sha"] != y["stsd_sha"]:
                report.append(f"trak[{label}].stsd sha {x['stsd_sha']} vs {y['stsd_sha']}")
            _diff_tables(x["tables"], y["tables"], label, report)
    elif moov_a is None or moov_b is None:
        report.append("moov: present only in " + ("ours" if moov_b is None else "ref"))
    mda = find(ta, "mdat")
    mdb = find(tb, "mdat")
    if mda and mdb:
        _diff_payload(a, b, mda, mdb, report)
    return report[:max_report]
