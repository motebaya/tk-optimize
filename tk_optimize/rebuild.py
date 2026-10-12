"""ISO-BMFF rebuild primitives: parse, mutate and re-serialize box trees.

Used by the modern methods (tk_optimize.method_stream / method_patch).
Unlike tk_optimize.boxes (read-only), this module can grow/shrink boxes and
re-emit an identical byte layout for unmodified subtrees.
"""

import struct

CONTAINERS = {"moov", "trak", "mdia", "minf", "stbl", "udta", "edts", "dinf", "meta", "ilst"}


class BoxRec:
    __slots__ = ("typ", "off", "size", "hdr", "children")

    def __init__(self, typ, off, size, hdr):
        self.typ = typ
        self.off = off
        self.size = size
        self.hdr = hdr
        self.children = None

    @property
    def payload_off(self):
        return self.off + self.hdr

    @property
    def end(self):
        return self.off + self.size

    @property
    def payload(self):
        return None


def parse(data, off, end):
    if off + 8 > end:
        return None
    size = struct.unpack(">I", data[off:off + 4])[0]
    typ = data[off + 4:off + 8].decode("latin1")
    hdr = 8
    if size == 1:
        size = struct.unpack(">Q", data[off + 8:off + 16])[0]
        hdr = 16
    elif size == 0:
        size = end - off
    if size < hdr or off + size > end:
        return None
    return BoxRec(typ, off, size, hdr)


def tree(data, start, end):
    """Parse a box sequence into BoxRecs with recursive children."""
    out = []
    off = start
    while off + 8 <= end:
        b = parse(data, off, end)
        if b is None:
            break
        if b.typ in CONTAINERS:
            cstart = b.payload_off
            if b.typ == "meta":  # fullbox container has 4 version/flags bytes
                cstart += 4
            b.children = tree(data, cstart, b.end)
        out.append(b)
        off = b.end
    return out


def find(boxes, typ):
    for b in boxes:
        if b.typ == typ:
            return b
    return None


def raw(data, b):
    return data[b.off:b.end]


def make(typ, payload):
    return struct.pack(">I", 8 + len(payload)) + typ.encode("latin1") + payload


def fullbox_payload(version, flags, body):
    return struct.pack(">B3s", version, flags.to_bytes(3, "big")) + body


# --- sample table helpers (payload excludes the 8-byte box header) --------

def table_read(typ, payload):
    """Return dict describing an st* table box payload (after header)."""
    ver_flags = payload[:4]
    if typ == "stts":
        n = struct.unpack(">I", payload[4:8])[0]
        ents = [struct.unpack(">II", payload[8 + i * 8:16 + i * 8]) for i in range(n)]
        return {"vf": ver_flags, "ents": ents}
    if typ == "stsc":
        n = struct.unpack(">I", payload[4:8])[0]
        ents = [struct.unpack(">III", payload[8 + i * 12:20 + i * 12]) for i in range(n)]
        return {"vf": ver_flags, "ents": ents}
    if typ in ("stco", "co64"):
        n = struct.unpack(">I", payload[4:8])[0]
        w = 4 if typ == "stco" else 8
        fmt = ">I" if w == 4 else ">Q"
        offs = [struct.unpack(fmt, payload[8 + i * w:8 + (i + 1) * w])[0] for i in range(n)]
        return {"vf": ver_flags, "offs": offs}
    if typ == "stsz":
        unit = struct.unpack(">I", payload[4:8])[0]
        n = struct.unpack(">I", payload[8:12])[0]
        sizes = [unit] * n if unit else list(struct.unpack(f">{n}I", payload[12:12 + 4 * n]))
        return {"vf": ver_flags, "unit": unit, "sizes": sizes}
    if typ == "stss":
        n = struct.unpack(">I", payload[4:8])[0]
        return {"vf": ver_flags, "nums": [struct.unpack(">I", payload[8 + i * 4:12 + i * 4])[0] for i in range(n)]}
    raise ValueError(f"unsupported table {typ}")


def table_write(typ, d):
    if typ == "stts":
        body = struct.pack(">I", len(d["ents"])) + b"".join(struct.pack(">II", *e) for e in d["ents"])
    elif typ == "stsc":
        body = struct.pack(">I", len(d["ents"])) + b"".join(struct.pack(">III", *e) for e in d["ents"])
    elif typ in ("stco", "co64"):
        fmt = ">I" if typ == "stco" else ">Q"
        body = struct.pack(">I", len(d["offs"])) + b"".join(struct.pack(fmt, o) for o in d["offs"])
    elif typ == "stsz":
        if d["unit"]:
            body = struct.pack(">II", d["unit"], len(d["sizes"]))
        else:
            body = struct.pack(">II", 0, len(d["sizes"])) + struct.pack(f">{len(d['sizes'])}I", *d["sizes"])
    elif typ == "stss":
        body = struct.pack(">I", len(d["nums"])) + b"".join(struct.pack(">I", n) for n in d["nums"])
    else:
        raise ValueError(typ)
    return make(typ, d["vf"] + body)


def rebuild(data, boxes, handlers):
    """Re-serialize a parsed box tree.

    handlers: dict box_type -> callable(data, boxrec) -> bytes (full box incl
    header) or None to fall through.  Handled types must not rely on children
    being recursed.  Unhandled containers recurse; unhandled leaf boxes are
    copied byte-identically.
    """
    out = []
    for b in boxes:
        fn = handlers.get(b.typ)
        if fn is not None:
            res = fn(data, b)
            if res is not None:
                out.append(res)
                continue
        if b.children is not None:
            kids = rebuild(data, b.children, handlers)
            if b.typ == "meta":
                payload = data[b.payload_off:b.payload_off + 4] + kids
            else:
                payload = kids
            if b.hdr == 16:
                out.append(struct.pack(">IQ", 1, 8 + len(payload)) + b.typ.encode("latin1") + payload)
            else:
                out.append(make(b.typ, payload))
        else:
            out.append(raw(data, b))
    return b"".join(out)


# --- metadata (udta/mdta) helpers ----------------------------------------
# Wire format observed in reference outputs:
#   udta > meta(v0) > hdlr('mdta', name="") + keys + ilst
#   hdlr  payload: verflags(4) predef(4) 'mdta' reserved(12) "\0"
#   keys  payload: verflags(4) count(4) then one box per key: size(4) 'mdta' name
#   ilst  payload: per entry: size(4) key_id(4) data
#   data  payload: verflags(4, text=0x00000001) reserved(4) value

def mdta_key_box(name):
    """'keys' entry: size covers the 4-byte size field plus 'mdta' + name."""
    body = b"mdta" + name.encode("latin1")
    return struct.pack(">I", 4 + len(body)) + body


def mdta_data_box(key_id, text):
    payload = text.encode("utf-8") if isinstance(text, str) else text
    inner = struct.pack(">I", 8 + 8 + len(payload)) + b"data" + b"\x00\x00\x00\x01" \
        + b"\x00\x00\x00\x00" + payload
    return struct.pack(">I", 8 + len(inner)) + struct.pack(">I", key_id) + inner


def udta_mdta(entries):
    """entries: ordered list[(key_name, value_str)] -> full udta box bytes."""
    hdlr_body = b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + b"mdta" + b"\x00" * 12 + b"\x00"
    hdlr = make("hdlr", hdlr_body)
    keys = make("keys", b"\x00\x00\x00\x00" + struct.pack(">I", len(entries))
                + b"".join(mdta_key_box(n) for n, _v in entries))
    ilst = make("ilst", b"".join(mdta_data_box(i, v) for i, (_n, v) in enumerate(entries, start=1)))
    body = b"\x00\x00\x00\x00" + hdlr + keys + ilst
    meta = make("meta", body)
    return make("udta", meta)
