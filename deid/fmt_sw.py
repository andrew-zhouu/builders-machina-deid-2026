"""SolidWorks 2015+ container (non-OLE .sldprt/.sldasm/.slddrw).

Layout established empirically and validated on every file before any write:

  local record   : 8 unvalidated bytes, 4-byte per-file signature,
                   14 00 06 00 08 00, u32 stamp, u32 crc32(uncompressed),
                   u32 compressed size, u32 uncompressed size, u32 name length,
                   name (each byte nibble-swapped), raw-deflate data
  central record : 4-byte per-file signature, 00 00, 14 00 06 00 08 00,
                   u32 stamp, u32 crc32, u32 csize, u32 usize, u32 name length,
                   10 bytes, u32 offset of local record, name (nibble-swapped)

A stream is rewritten strictly in place: same uncompressed length, same
compressed length (reached with a padded deflate stream), so no offset in the
file moves. Only the stream bytes and its two CRC fields change. Files that do
not validate completely are never modified.
"""
import collections
import struct
import zlib

MARK = b"\x14\x00\x06\x00\x08\x00"
_SWAP = bytes(((c << 4) & 0xF0) | (c >> 4) for c in range(256))


class Stream:
    __slots__ = ("name", "crc", "csize", "usize", "local", "central", "data_off")

    def __init__(self, name, crc, csize, usize, local, central, data_off):
        self.name = name
        self.crc = crc
        self.csize = csize
        self.usize = usize
        self.local = local        # offset of local record (the 8 preamble bytes)
        self.central = central    # offset of central record
        self.data_off = data_off  # offset of compressed data


def parse(buf):
    """Return list of Stream for a valid container, else None."""
    n = len(buf)
    if n < 64:
        return None
    loc = collections.Counter()
    cen = collections.Counter()
    i = buf.find(MARK)
    hits = 0
    while i >= 0 and hits < 200000:
        hits += 1
        if i >= 6 and buf[i - 2:i] == b"\x00\x00":
            cen[bytes(buf[i - 6:i - 2])] += 1
        if i >= 4:
            loc[bytes(buf[i - 4:i])] += 1
        i = buf.find(MARK, i + 1)
    if not cen or not loc:
        return None
    csig, ccount = cen.most_common(1)[0]
    if ccount < 2:
        return None
    lsig = None
    for cand, cnt in loc.most_common(4):
        if cnt == ccount and cand[2:] != b"\x00\x00":
            lsig = cand
            break
    if lsig is None:
        return None
    cd = buf.find(csig + b"\x00\x00" + MARK)
    streams = []
    i = cd
    while i + 46 <= n and buf[i:i + 4] == csig and buf[i + 6:i + 12] == MARK:
        _c0, crc, cs, us, nl = struct.unpack_from("<IIIII", buf, i + 12)
        if nl > 1024 or i + 46 + nl > n:
            return None
        off = struct.unpack_from("<I", buf, i + 42)[0]
        name = bytes(buf[i + 46:i + 46 + nl]).translate(_SWAP)
        # matching local record
        if off + 38 + nl > n or buf[off + 8:off + 12] != lsig or buf[off + 12:off + 18] != MARK:
            return None
        _l0, lcrc, lcs, lus, lnl = struct.unpack_from("<IIIII", buf, off + 18)
        if (lcrc, lcs, lus, lnl) != (crc, cs, us, nl) or buf[off + 38:off + 38 + nl] != buf[i + 46:i + 46 + nl]:
            return None
        if off + 38 + nl + cs > n:
            return None
        streams.append(Stream(name.decode("latin-1"), crc, cs, us, off, i, off + 38 + nl))
        i += 46 + nl
    if len(streams) != ccount:
        return None
    return streams


def read_stream(buf, s, verify=True):
    d = zlib.decompressobj(-15)
    data = d.decompress(bytes(buf[s.data_off:s.data_off + s.csize])) + d.flush()
    if verify and (len(data) != s.usize or (zlib.crc32(data) & 0xFFFFFFFF) != s.crc):
        raise ValueError("stream %s fails size/crc check" % s.name)
    return data


# ---- exact-size deflate ----------------------------------------------------
_E5 = b"\x00\x00\x00\xff\xff"                 # empty stored block, not final (byte aligned)
_E6 = b"\x02\x00\x00\x00\xff\xff"             # empty fixed block + empty stored block (byte aligned)


def _shift(z, k):
    """Prepend k empty, non-final fixed-Huffman blocks (10 bits each) to a raw
    deflate stream by shifting its bits. Deflate packs bits LSB first, so the
    stream is a little-endian integer."""
    v = int.from_bytes(z, "little") << (10 * k)
    for i in range(k):
        v |= 2 << (10 * i)                    # BFINAL=0, BTYPE=01, end-of-block
    return v.to_bytes(len(z) + (10 * k + 7) // 8, "little")


def _inflates_to(z, data):
    d = zlib.decompressobj(-15)
    try:
        return d.decompress(z) + d.flush() == data and d.eof and not d.unused_data
    except zlib.error:
        return False


def _fit(z, size, data):
    """Pad raw deflate stream z (which inflates to data) to exactly `size`
    bytes with empty blocks placed before it, or return None."""
    p = size - len(z)
    if p < 0:
        return None
    if p == 0:
        return z
    for k in range(0, 9):
        base = z if k == 0 else _shift(z, k)
        cands = [base]
        if k and base[-1:] == b"\x00":
            cands.append(base[:-1])           # the shift fitted into existing padding bits
        for c in cands:
            rest = size - len(c)
            if rest < 0:
                continue
            # rest = 5a + 6b with the fewest 6-byte units
            for b6 in range(0, 5):
                r5 = rest - 6 * b6
                if r5 >= 0 and r5 % 5 == 0:
                    out = _E5 * (r5 // 5) + _E6 * b6 + c
                    if _inflates_to(out, data):
                        return out
                    break
    return None


_LEVELS = (1, 6, 9, 2, 3, 4, 5, 7, 8)
_MEMS = (8, 9, 7, 6, 5, 4, 3, 2, 1)


def _deflate(data, level, mem=8, strategy=zlib.Z_DEFAULT_STRATEGY):
    co = zlib.compressobj(level, zlib.DEFLATED, -15, mem, strategy)
    return co.compress(data) + co.flush()


def guess_params(data, original):
    """(level, memLevel) that reproduce `original` from `data`, or None."""
    for level in _LEVELS:
        if _deflate(data, level, 8) == original:
            return level, 8
    return None


def deflate_exact(data, size, prefer=None):
    """Raw-deflate `data` into exactly `size` bytes, or return None.

    The writer's own zlib parameters are tried first, so the result normally
    differs from a native stream only by a few bytes of padding (empty,
    non-final deflate blocks placed before the data). Other settings are
    tried only when that does not fit."""
    order = []
    if prefer:
        order.append(prefer)
    order += [(lv, 8) for lv in _LEVELS if (lv, 8) != prefer]
    out = None
    seen = set()
    for level, mem in order:
        z = _deflate(data, level, mem)
        if len(z) in seen:
            continue
        seen.add(len(z))
        out = _fit(z, size, data)
        if out is not None:
            break
    if out is None:
        for level, mem, strat in ((1, 9, 0), (1, 7, 0), (1, 5, 0), (9, 9, 0), (9, 7, 0), (6, 5, 0),
                                  (9, 8, zlib.Z_FILTERED), (6, 8, zlib.Z_FILTERED), (1, 8, zlib.Z_FILTERED)):
            z = _deflate(data, level, mem, strat)
            if len(z) in seen:
                continue
            seen.add(len(z))
            out = _fit(z, size, data)
            if out is not None:
                break
    if out is None:
        try:
            from zopfli import zlib as zz
            for it in (15, 5, 40):
                z = zz.compress(data, numiterations=it)[2:-4]
                out = _fit(z, size, data)
                if out is not None:
                    break
        except Exception:
            pass
    if out is None and len(data) > 1:
        # last resort: split the data into two blocks at various points; every
        # split point gives a slightly different length and bit alignment
        n = len(data)
        level = prefer[0] if prefer else 1
        for j in sorted(set([n // 2, n // 3, n // 4, 2 * n // 3, 3 * n // 4, 1, n - 1, n // 5, n // 7, n // 9])):
            if j <= 0 or j >= n:
                continue
            for lv in (level, 9):
                co = zlib.compressobj(lv, zlib.DEFLATED, -15)
                z = co.compress(data[:j]) + co.flush(zlib.Z_SYNC_FLUSH) + co.compress(data[j:]) + co.flush()
                out = _fit(z, size, data)
                if out is not None:
                    break
            if out is not None:
                break
    if out is None or len(out) != size or not _inflates_to(out, data):
        return None
    return out


def write_stream(buf, s, new_data, old_data=None):
    """Replace stream contents inside bytearray `buf`. Returns True on success;
    False (buffer untouched) if the new data cannot be stored in place.
    The compressed size never changes; the uncompressed size may."""
    prefer = None
    if old_data is not None:
        prefer = guess_params(old_data, bytes(buf[s.data_off:s.data_off + s.csize]))
    z = deflate_exact(new_data, s.csize, prefer)
    if z is None:
        return False
    crc = zlib.crc32(new_data) & 0xFFFFFFFF
    buf[s.data_off:s.data_off + s.csize] = z
    struct.pack_into("<I", buf, s.local + 22, crc)
    struct.pack_into("<I", buf, s.central + 16, crc)
    if len(new_data) != s.usize:
        struct.pack_into("<I", buf, s.local + 30, len(new_data))
        struct.pack_into("<I", buf, s.central + 24, len(new_data))
        s.usize = len(new_data)
    s.crc = crc
    return True


def validate(buf):
    """Full check: parse and CRC-verify every stream. Returns stream count or
    raises ValueError."""
    streams = parse(buf)
    if streams is None:
        raise ValueError("not a valid SolidWorks container")
    for s in streams:
        read_stream(buf, s, verify=True)
    return len(streams)
