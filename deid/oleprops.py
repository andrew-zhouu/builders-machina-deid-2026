"""OLE property-set streams ([MS-OLEPS]) - locate string values exactly.

parse(data) returns a list of PropStr records that give, for every string
value in the stream, its byte offset and byte length inside the stream plus
its encoding. Nothing else in the stream is interpreted, and writers only ever
overwrite those bytes with a replacement of identical byte length.
"""
import struct

FMT_SI = "f29f85e04ff91068ab9108002b27b3d9"
FMT_DSI = "d5cdd5022e9c101b939708002b2cf9ae"
FMT_UD = "d5cdd5052e9c101b939708002b2cf9ae"
FMT_INV_DT = "32853f0f344411d19e930060b03c1ca6"
FMT_INV_SI = "3d38de3905884c14bb3718f4d5dd31c7"    # Inventor Summary Information
FMT_INV_DSI = "8cf58000da664ae68ff07b58406fb049"   # Inventor Document Summary Information
FMT_INV_DTC = "d861fb30313611d19e920060b03c1ca6"   # Design Tracking Control
FMT_INV_UD = "9929adb86407413eb3dccb9ad2f564b7"    # Inventor User Defined Properties
FMT_SE_EXT = "cc024fa26eb511ce8aa208003601e988"    # Solid Edge extended summary

SI_NAMES = {2: "title", 3: "subject", 4: "author", 5: "keywords", 6: "comments", 7: "template",
            8: "lastauthor", 9: "revnumber", 18: "appname"}
DSI_NAMES = {2: "category", 14: "manager", 15: "company", 26: "contenttype", 27: "contentstatus",
             28: "language", 29: "docversion"}
INV_DT_NAMES = {5: "partnumber", 7: "project", 9: "costcenter", 10: "checkedby", 12: "engapprovedby",
                17: "userstatus", 20: "material", 23: "weblink", 29: "description", 30: "vendor",
                33: "subtypename", 35: "mfgapprovedby", 40: "designstatus", 41: "designer",
                42: "engineer", 43: "authority", 44: "template", 48: "manufacturer",
                49: "standardsorg", 55: "categories", 56: "stocknumber"}

CODEPAGES = {1200: "utf-16-le", 1201: "utf-16-be", 65001: "utf-8", 10000: "mac_roman", 0: "cp1252",
             20127: "ascii", 28591: "latin-1"}


def codec_for(cp):
    import codecs
    if cp in CODEPAGES:
        return CODEPAGES[cp]
    name = "cp%d" % cp
    try:
        codecs.lookup(name)
        return name
    except LookupError:
        return "cp1252"


class PropStr:
    __slots__ = ("fmtid", "pid", "name", "off", "nbytes", "enc", "kind")

    def __init__(self, fmtid, pid, name, off, nbytes, enc, kind):
        self.fmtid = fmtid
        self.pid = pid
        self.name = name
        self.off = off          # offset of first character byte in the stream
        self.nbytes = nbytes    # bytes of character data excluding terminator
        self.enc = enc
        self.kind = kind        # 'lpstr' | 'lpwstr'

    def label(self):
        if self.name == "Property Set Name":
            return "ole.setname"
        if self.fmtid in (FMT_SI, FMT_INV_SI):
            return "ole.si." + SI_NAMES.get(self.pid, str(self.pid))
        if self.fmtid in (FMT_DSI, FMT_INV_DSI):
            return "ole.dsi." + DSI_NAMES.get(self.pid, str(self.pid))
        if self.fmtid == FMT_INV_DTC:
            return "ole.invdtc." + ("lastsavedby" if self.pid == 16 else str(self.pid))
        if self.fmtid == FMT_SE_EXT and not self.name:
            return "ole.seext." + ("fullname" if self.pid == 20 else str(self.pid))
        if self.fmtid == FMT_INV_DT:
            return "ole.inv." + INV_DT_NAMES.get(self.pid, str(self.pid))
        if self.name:
            return "ole.ud." + self.name
        return "ole.%s.%d" % (self.fmtid[:8], self.pid)


def _u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def _strip_term(d, off, nbytes, wide):
    """Trim trailing NUL terminator(s) from a character run."""
    if wide:
        while nbytes >= 2 and d[off + nbytes - 2:off + nbytes] == b"\x00\x00":
            nbytes -= 2
    else:
        while nbytes >= 1 and d[off + nbytes - 1] == 0:
            nbytes -= 1
    return nbytes


def _value(d, p, limit, cp, enc, fmtid, pid, name, out, depth=0):
    """Parse a TypedPropertyValue at p; append PropStr; return offset after it
    (or -1 when the size cannot be determined)."""
    if p + 8 > limit:
        return -1
    vt = _u32(d, p) & 0xFFFF
    q = p + 4
    if vt == 0x1E:  # VT_LPSTR
        ln = _u32(d, q)
        if q + 4 + ln > limit:
            return -1
        wide = cp in (1200, 1201)
        nb = _strip_term(d, q + 4, ln, wide)
        if nb > 0:
            out.append(PropStr(fmtid, pid, name, q + 4, nb, enc, "lpstr"))
        return q + 4 + ((ln + 3) & ~3)
    if vt == 0x1F:  # VT_LPWSTR
        ln = _u32(d, q) * 2
        if q + 4 + ln > limit:
            return -1
        nb = _strip_term(d, q + 4, ln, True)
        if nb > 0:
            out.append(PropStr(fmtid, pid, name, q + 4, nb, "utf-16-le", "lpwstr"))
        return q + 4 + ((ln + 3) & ~3)
    if vt in (0x101E, 0x101F):  # vector of strings
        cnt = _u32(d, q)
        q += 4
        if cnt > 10000:
            return -1
        for _ in range(cnt):
            if q + 4 > limit:
                return -1
            ln = _u32(d, q)
            if vt == 0x101F:
                ln *= 2
            if q + 4 + ln > limit:
                return -1
            nb = _strip_term(d, q + 4, ln, vt == 0x101F or cp in (1200, 1201))
            if nb > 0:
                out.append(PropStr(fmtid, pid, name, q + 4, nb,
                                   "utf-16-le" if vt == 0x101F else enc, "lpstr" if vt == 0x101E else "lpwstr"))
            q += 4 + ln
            if vt == 0x101F:
                q = (q + 3) & ~3
        return (q + 3) & ~3
    if vt == 0x100C and depth == 0:  # vector of variants
        cnt = _u32(d, q)
        q += 4
        if cnt > 10000:
            return -1
        for _ in range(cnt):
            q = _value(d, q, limit, cp, enc, fmtid, pid, name, out, depth + 1)
            if q < 0:
                return -1
        return q
    sizes = {0: 0, 1: 0, 2: 4, 3: 4, 4: 4, 5: 8, 6: 8, 7: 8, 10: 4, 11: 4, 16: 4, 17: 4, 18: 4, 19: 4,
             20: 8, 21: 8, 22: 4, 23: 4, 64: 8, 72: 16}
    if vt in sizes:
        return q + sizes[vt]
    if vt in (0x41, 0x46, 0x47, 0x08):  # blob / clipboard data / BSTR: length prefixed
        if q + 4 > limit:
            return -1
        return q + 4 + ((_u32(d, q) + 3) & ~3)
    return -1


def parse(data):
    """Return list of PropStr for a property-set stream (empty when not one)."""
    out = []
    try:
        if len(data) < 48 or data[0:2] != b"\xfe\xff":
            return out
        nsets = _u32(data, 24)
        if nsets < 1 or nsets > 2:
            return out
        for s in range(nsets):
            fmtid = data[28 + 20 * s:44 + 20 * s]
            # canonical GUID text order
            f = struct.unpack("<IHH8s", fmtid)
            fmt = "%08x%04x%04x%s" % (f[0], f[1], f[2], f[3].hex())
            off = _u32(data, 44 + 20 * s)
            if off + 8 > len(data):
                continue
            size = _u32(data, off)
            nprops = _u32(data, off + 4)
            limit = min(len(data), off + size) if size else len(data)
            if nprops > 5000 or off + 8 + 8 * nprops > len(data):
                continue
            entries = [(_u32(data, off + 8 + 8 * i), _u32(data, off + 12 + 8 * i)) for i in range(nprops)]
            cp = 1252
            for pid, po in entries:
                if pid == 1 and off + po + 6 <= len(data) and (_u32(data, off + po) & 0xFFFF) == 2:
                    cp = struct.unpack_from("<H", data, off + po + 4)[0]
            enc = codec_for(cp)
            names = {}
            for pid, po in entries:
                if pid == 0:
                    p = off + po
                    if p + 4 > limit:
                        break
                    cnt = _u32(data, p)
                    p += 4
                    if cnt > 5000:
                        break
                    for _ in range(cnt):
                        if p + 8 > limit:
                            break
                        ident = _u32(data, p)
                        ln = _u32(data, p + 4)
                        if cp == 1200:
                            raw = data[p + 8:p + 8 + 2 * ln]
                            p += 8 + ((2 * ln + 3) & ~3)
                            nm = raw.decode("utf-16-le", "replace")
                        else:
                            raw = data[p + 8:p + 8 + ln]
                            p += 8 + ln
                            nm = raw.decode(enc, "replace")
                        names[ident] = nm.rstrip("\x00")
            for pid, po in entries:
                if pid in (0, 1):
                    continue
                _value(data, off + po, limit, cp, enc, fmt, pid, names.get(pid), out)
    except (struct.error, ValueError, IndexError):
        pass
    return out
