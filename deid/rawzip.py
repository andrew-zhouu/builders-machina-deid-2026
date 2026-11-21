"""Byte-preserving ZIP rewriting.

A ZIP container is rewritten by copying the local record of every untouched
member verbatim (header, compressed bytes, data descriptor), whatever its
compression method, and writing fresh records only for members whose content
or name changed. The central directory is copied entry by entry with only the
changed fields patched. Untouched members are therefore bit-identical to the
input, including their compressed bytes.

Archives that use ZIP64 or spanning are not handled here (the caller falls
back to the generic rewriter or leaves the file alone). Encrypted members can
be copied and renamed (their name is not part of the encryption), but their
content cannot be read or replaced.
"""
import struct
import zlib

_LOC = b"PK\x03\x04"
_CEN = b"PK\x01\x02"
_EOCD = b"PK\x05\x06"


class Unsupported(Exception):
    pass


def _find_eocd(buf):
    i = buf.rfind(_EOCD, max(0, len(buf) - 65557))
    if i < 0:
        raise Unsupported("no end record")
    disk, cddisk, n_here, n_total, cd_size, cd_off, clen = struct.unpack_from("<HHHHIIH", buf, i + 4)
    if disk or cddisk or n_here != n_total:
        raise Unsupported("multi-disk")
    if n_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
        raise Unsupported("zip64")
    if buf[i - 20:i - 16] == b"PK\x06\x07":
        raise Unsupported("zip64")
    return i, n_total, cd_size, cd_off, bytes(buf[i + 22:i + 22 + clen])


def entries(buf):
    """Parse the central directory. Returns (list of dict, eocd_comment)."""
    eocd, n, cd_size, cd_off, comment = _find_eocd(buf)
    # tolerate a preamble: locate the real directory start
    shift = eocd - (cd_off + cd_size)
    if shift < 0:
        raise Unsupported("inconsistent directory")
    p = cd_off + shift
    out = []
    for _ in range(n):
        if buf[p:p + 4] != _CEN:
            raise Unsupported("bad central record")
        (vmade, vneed, flags, method, mtime, mdate, crc, csize, usize, nlen, xlen, clen, disk, iattr,
         eattr, off) = struct.unpack_from("<HHHHHHIIIHHHHHII", buf, p + 4)
        if csize == 0xFFFFFFFF or usize == 0xFFFFFFFF or off == 0xFFFFFFFF:
            raise Unsupported("zip64")
        name = bytes(buf[p + 46:p + 46 + nlen])
        rec = bytes(buf[p:p + 46 + nlen + xlen + clen])
        lo = off + shift
        if buf[lo:lo + 4] != _LOC:
            raise Unsupported("bad local record")
        lnlen, lxlen = struct.unpack_from("<HH", buf, lo + 26)
        data = lo + 30 + lnlen + lxlen
        end = data + csize
        if flags & 0x8:          # data descriptor after the data
            if buf[end:end + 4] == b"PK\x07\x08":
                end += 16
            else:
                end += 12
        if end > len(buf):
            raise Unsupported("truncated member")
        out.append({"name": name, "flags": flags, "method": method, "crc": crc, "csize": csize,
                    "usize": usize, "cen": rec, "local": lo, "data": data, "end": end,
                    "lextra": bytes(buf[lo + 30 + lnlen:lo + 30 + lnlen + lxlen]),
                    "time": (mtime, mdate), "vneed": vneed, "nlen": nlen, "xlen": xlen,
                    "enc": bool(flags & 0x1)})
        p += 46 + nlen + xlen + clen
    return out, comment


def read_member(buf, e):
    if e.get("enc"):
        raise Unsupported("encrypted")
    raw = bytes(buf[e["data"]:e["data"] + e["csize"]])
    if e["method"] == 0:
        data = raw
    elif e["method"] == 8:
        d = zlib.decompressobj(-15)
        data = d.decompress(raw) + d.flush()
    else:
        raise Unsupported("compression method %d" % e["method"])
    if (zlib.crc32(data) & 0xFFFFFFFF) != e["crc"] or len(data) != e["usize"]:
        raise Unsupported("member fails crc")
    return data


def _strip_unicode_path(extra):
    """Drop Info-ZIP Unicode Path fields (0x7075) from an extra block: they
    repeat the member name and would keep the old one after a rename."""
    out = bytearray()
    i = 0
    n = len(extra)
    while i + 4 <= n:
        hid, sz = struct.unpack_from("<HH", extra, i)
        if i + 4 + sz > n:
            return bytes(extra)          # malformed block: leave it alone
        if hid != 0x7075:
            out += extra[i:i + 4 + sz]
        i += 4 + sz
    out += extra[i:]
    return bytes(out)


def rewrite(buf, out, changes, names=None):
    """Write a new archive to file object `out`.

    changes: {member index: new uncompressed bytes}
    names  : {member index: new name bytes} (optional)
    Returns the number of members written fresh.
    """
    names = names or {}
    ents, comment = entries(buf)
    pos = 0
    cen = []
    fresh = 0
    for idx, e in enumerate(ents):
        new_name = names.get(idx)
        if idx not in changes and new_name is None:
            out.write(buf[e["local"]:e["end"]])
            rec = bytearray(e["cen"])
            struct.pack_into("<I", rec, 42, pos)
            cen.append(bytes(rec))
            pos += e["end"] - e["local"]
            continue
        fresh += 1
        old = e["cen"]
        cextra = old[46 + e["nlen"]:46 + e["nlen"] + e["xlen"]]
        ccomment = old[46 + e["nlen"] + e["xlen"]:]
        lextra = e["lextra"]
        name = new_name if new_name is not None else e["name"]
        if new_name is not None:
            lextra = _strip_unicode_path(lextra)
            cextra = _strip_unicode_path(cextra)
        if idx not in changes:
            # rename only: the record is copied as it is (flags, sizes, the
            # compressed or encrypted bytes, data descriptor); only the name
            # and the name length change
            hdr = bytearray(buf[e["local"]:e["local"] + 30])
            struct.pack_into("<HH", hdr, 26, len(name), len(lextra))
            out.write(bytes(hdr) + name + lextra)
            out.write(buf[e["data"]:e["end"]])
            rec = bytearray(old[:46])
            struct.pack_into("<HH", rec, 28, len(name), len(cextra))
            struct.pack_into("<I", rec, 42, pos)
            cen.append(bytes(rec) + name + cextra + ccomment)
            pos += 30 + len(name) + len(lextra) + (e["end"] - e["data"])
            continue
        if e["enc"]:
            raise Unsupported("encrypted member content")
        data = changes[idx]
        if e["method"] == 0:
            method, comp = 0, data
        else:
            co = zlib.compressobj(6, zlib.DEFLATED, -15)
            comp = co.compress(data) + co.flush()
            method = 8
        crc = zlib.crc32(data) & 0xFFFFFFFF
        usize = len(data)
        flags = e["flags"] & ~0x8
        if len(comp) >= 0xFFFFFFFF or usize >= 0xFFFFFFFF:
            raise Unsupported("member too large")
        vneed = 20 if method == 8 else min(e["vneed"], 20)
        out.write(_LOC + struct.pack("<HHHHHIIIHH", vneed, flags, method, e["time"][0], e["time"][1], crc,
                                     len(comp), usize, len(name), len(lextra)) + name + lextra)
        out.write(comp)
        rec = bytearray(old[:46])
        struct.pack_into("<H", rec, 6, vneed)
        struct.pack_into("<HH", rec, 8, flags, method)
        struct.pack_into("<III", rec, 16, crc, len(comp), usize)
        struct.pack_into("<HH", rec, 28, len(name), len(cextra))
        struct.pack_into("<I", rec, 42, pos)
        cen.append(bytes(rec) + name + cextra + ccomment)
        pos += 30 + len(name) + len(lextra) + len(comp)
    cd_start = pos
    for rec in cen:
        out.write(rec)
        pos += len(rec)
    if cd_start >= 0xFFFFFFFF or len(cen) >= 0xFFFF:
        raise Unsupported("needs zip64")
    out.write(_EOCD + struct.pack("<HHHHIIH", 0, 0, len(cen), len(cen), pos - cd_start, cd_start, len(comment))
              + comment)
    return fresh


def decode_name(e):
    return e["name"].decode("utf-8" if e["flags"] & 0x800 else "cp437", "replace")


def encode_name(e, new_name):
    """Encode a new member name the way the original one was encoded."""
    if e["flags"] & 0x800:
        return new_name.encode("utf-8")
    try:
        return new_name.encode("cp437")
    except UnicodeEncodeError:
        return None


def verify(old, new, changes, names=None):
    """Check a rewritten archive against the original. Raises ValueError."""
    names = names or {}
    a, ca = entries(old)
    b, cb = entries(new)
    if len(a) != len(b) or ca != cb:
        raise ValueError("member count or comment changed")
    for idx, (x, y) in enumerate(zip(a, b)):
        if y["name"] != names.get(idx, x["name"]):
            raise ValueError("member name mismatch")
        if idx in changes:
            if read_member(new, y) != changes[idx]:
                raise ValueError("changed member does not read back")
        else:
            if (x["crc"], x["csize"], x["usize"], x["method"]) != (y["crc"], y["csize"], y["usize"], y["method"]):
                raise ValueError("untouched member header changed")
            if bytes(old[x["data"]:x["data"] + x["csize"]]) != bytes(new[y["data"]:y["data"] + y["csize"]]):
                raise ValueError("untouched member bytes changed")
