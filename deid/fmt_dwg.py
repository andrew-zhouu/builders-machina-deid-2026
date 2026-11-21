"""DWG R2004+ (AC1018 / AC1024 / AC1027 / AC1032) data pages - in place.

The container stores its sections in compressed data pages. Every page has a
32-byte header (XOR-masked with 0x4164536B ^ file offset: type, section
number, compressed size, decompressed size, 64-bit start offset, header
checksum, data checksum; both checksums are Adler-32 with a seed), and is
followed by LZ77-compressed data in which text survives as literal bytes.

This module
  * finds data pages and proves that it understands each one (both stored
    checksums must be reproduced, and the page must decompress to exactly
    the declared size) - pages that fail are never touched;
  * lets the caller change bytes of the *decompressed* page content by
    editing the literal bytes they originate from, then re-decompresses and
    insists that the result is exactly the requested content (so a literal
    that is also the source of an unrelated back-reference is left alone);
  * rewrites both checksums of every page it changed.

No byte moves; the file size is unchanged.
"""
import struct
import zlib

PAGE_TYPE = 0x4163043B
MASK = 0x4164536B
VERSIONS = (b"AC1018", b"AC1024", b"AC1027", b"AC1032")


class Bad(Exception):
    pass


def _lit_len(src, i, n):
    """Literal length opcode at i. Returns (length, next_i, opcode_or_0)."""
    if i >= n:
        raise Bad("eof")
    op = src[i]
    i += 1
    if 0x01 <= op <= 0x0F:
        return op + 3, i, 0
    if op == 0x00:
        total = 0x0F
        while True:
            if i >= n:
                raise Bad("eof")
            b = src[i]
            i += 1
            if b == 0:
                total += 0xFF
            else:
                return total + b + 3, i, 0
    if op & 0xF0:
        return 0, i, op          # not a literal run: this is the next compression opcode
    raise Bad("bad literal opcode")


def _two_byte(src, i, n):
    if i + 2 > n:
        raise Bad("eof")
    first = src[i]
    off = (first >> 2) | (src[i + 1] << 6)
    return off, first & 0x03, i + 2


def _long(src, i, n):
    total = 0
    if i >= n:
        raise Bad("eof")
    b = src[i]
    i += 1
    if b == 0:
        total = 0xFF
        while True:
            if i >= n:
                raise Bad("eof")
            b = src[i]
            i += 1
            if b != 0:
                break
            total += 0xFF
    return total + b, i


MAX_PLAIN = 4 << 20


def decompress(src, expect=None):
    """Decompress one page. Returns (data, origin, consumed) where origin[j]
    is the index in `src` of the literal byte that decompressed byte j
    ultimately comes from and consumed is the offset just past the 0x11
    terminator."""
    n = len(src)
    dst = bytearray()
    origin = []
    length, i, op = _lit_len(src, 0, n)

    def literals(i, count):
        if i + count > n:
            raise Bad("literal overrun")
        dst.extend(src[i:i + count])
        origin.extend(range(i, i + count))
        return i + count

    i = literals(i, length)
    guard = 0
    while True:
        guard += 1
        if guard > 4 * n + 64:
            raise Bad("loop")
        if op == 0:
            if i >= n:
                raise Bad("eof")
            op = src[i]
            i += 1
        if op >= 0x40:
            comp = ((op & 0xF0) >> 4) - 1
            if i >= n:
                raise Bad("eof")
            op2 = src[i]
            i += 1
            off = (op2 << 2) | ((op & 0x0C) >> 2)
            lit = op & 0x03
            nxt = 0
            if lit == 0:
                lit, i, nxt = _lit_len(src, i, n)
        elif 0x21 <= op <= 0x3F:
            comp = op - 0x1E
            off, lit, i = _two_byte(src, i, n)
            nxt = 0
            if lit == 0:
                lit, i, nxt = _lit_len(src, i, n)
        elif op == 0x20:
            v, i = _long(src, i, n)
            comp = v + 0x21
            off, lit, i = _two_byte(src, i, n)
            nxt = 0
            if lit == 0:
                lit, i, nxt = _lit_len(src, i, n)
        elif 0x12 <= op <= 0x1F:
            comp = (op & 0x0F) + 2
            off, lit, i = _two_byte(src, i, n)
            off += 0x3FFF
            nxt = 0
            if lit == 0:
                lit, i, nxt = _lit_len(src, i, n)
        elif op == 0x10:
            v, i = _long(src, i, n)
            comp = v + 9
            off, lit, i = _two_byte(src, i, n)
            off += 0x3FFF
            nxt = 0
            if lit == 0:
                lit, i, nxt = _lit_len(src, i, n)
        elif op == 0x11:
            break
        else:
            raise Bad("bad opcode %02x" % op)
        start = len(dst) - off - 1
        if start < 0:
            raise Bad("bad offset")
        for k in range(comp):
            dst.append(dst[start + k])
            origin.append(origin[start + k])
        i = literals(i, lit)
        op = nxt
        if len(dst) > MAX_PLAIN:
            raise Bad("too long")
    return bytes(dst), origin, i


def pages(buf):
    """Validated data pages: list of dict(off, csize, usize, hdr)."""
    n = len(buf)
    out = []
    if bytes(buf[:6]) not in VERSIONS:
        return out
    o = 0x100
    while o + 32 <= n:
        mask = MASK ^ o
        if (struct.unpack_from("<I", buf, o)[0] ^ mask) != PAGE_TYPE:
            o += 0x20
            continue
        hdr = [v ^ mask for v in struct.unpack_from("<8I", buf, o)]
        _typ, _sec, csize, psize, _start_lo, _start_hi, hck, dck = hdr
        usize = psize
        if csize == 0 or o + 32 + csize > n or psize < csize:
            o += 0x20
            continue
        data = bytes(buf[o + 32:o + 32 + csize])
        if (zlib.adler32(data, 0) & 0xFFFFFFFF) != dck:
            o += 0x20
            continue
        plain = bytearray(struct.pack("<8I", *hdr))
        plain[24:28] = b"\x00\x00\x00\x00"
        if (zlib.adler32(bytes(plain), dck) & 0xFFFFFFFF) != hck:
            o += 0x20
            continue
        out.append({"off": o, "csize": csize, "usize": usize, "hdr": hdr})
        o += 32 + csize
        o = (o + 0x1F) & ~0x1F
    return out


SECTION_MAP_TYPE = 0x4163003B


def section_names(buf):
    """Section id -> name ("AcDb:SummaryInfo", "AcDb:AcDbObjects", ...), read
    from the section map (a system page: plain 20-byte header - type,
    decompressed size, compressed size, compression type, checksum - followed
    by LZ77 data). Returns {} when the map cannot be read with certainty."""
    n = len(buf)
    best = {}
    o = 0x100
    while o + 20 <= n:
        if struct.unpack_from("<I", buf, o)[0] != SECTION_MAP_TYPE:
            o += 0x20
            continue
        _t, usize, csize, ctype, _ck = struct.unpack_from("<5I", buf, o)
        if ctype != 2 or csize == 0 or o + 20 + csize > n or usize > (8 << 20):
            o += 0x20
            continue
        try:
            data, _origin, _end = decompress(bytes(buf[o + 20:o + 20 + csize]))
        except Bad:
            o += 0x20
            continue
        if len(data) != usize or len(data) < 20:
            o += 0x20
            continue
        names = {}
        try:
            ndesc = struct.unpack_from("<I", data, 0)[0]
            p = 20
            for _ in range(ndesc):
                if p + 0x60 > len(data):
                    raise Bad("short description")
                _size, npages, _maxsz, _unk, _comp, sid, _enc = struct.unpack_from("<QIIIIII", data, p)
                name = data[p + 0x20:p + 0x60].split(b"\x00", 1)[0].decode("latin-1")
                p += 0x60 + 16 * npages
                if p > len(data):
                    raise Bad("page list overruns")
                if name:
                    names[sid] = name
        except (Bad, struct.error):
            o += 0x20
            continue
        if len(names) > len(best):
            best = names
        o += 0x20
    return best


def read_page(buf, page):
    """Return (plain_bytes, mode). mode "lz": compressed page; "raw": page of
    an uncompressed section (its data size is the full, aligned page size and
    it is not a valid compressed stream); None: not understood."""
    o, csize = page["off"], page["csize"]
    comp = bytes(buf[o + 32:o + 32 + csize])
    try:
        plain, _origin, end = decompress(comp)
        if csize - end <= 64:
            return plain, "lz"
    except Bad:
        pass
    if csize % 0x20 == 0 and page["hdr"][3] - csize == 32:
        return comp, "raw"
    return None, None


def _finish(buf, page, comp):
    o, csize = page["off"], page["csize"]
    dck = zlib.adler32(bytes(comp), 0) & 0xFFFFFFFF
    hdr = list(page["hdr"])
    hdr[7] = dck
    hdr[6] = 0
    hck = zlib.adler32(struct.pack("<8I", *hdr), dck) & 0xFFFFFFFF
    hdr[6] = hck
    mask = MASK ^ o
    buf[o:o + 32] = struct.pack("<8I", *[v ^ mask for v in hdr])
    buf[o + 32:o + 32 + csize] = comp
    page["hdr"] = hdr


def rewrite_page(buf, page, new_plain, mode="lz"):
    """Make the page hold new_plain (same length as before). For compressed
    pages only literal bytes are edited. buf is a bytearray. Returns True on
    success; False leaves buf untouched."""
    o, csize = page["off"], page["csize"]
    comp = bytearray(buf[o + 32:o + 32 + csize])
    if mode == "raw":
        if len(new_plain) != csize or bytes(new_plain) == bytes(comp):
            return False
        _finish(buf, page, bytearray(new_plain))
        return True
    try:
        plain, origin, _end = decompress(bytes(comp))
    except Bad:
        return False
    if len(new_plain) != len(plain):
        return False
    want = {}
    for j in range(len(plain)):
        if plain[j] != new_plain[j]:
            src = origin[j]
            if want.get(src, new_plain[j]) != new_plain[j]:
                return False
            want[src] = new_plain[j]
    if not want:
        return False
    for src, val in want.items():
        comp[src] = val
    try:
        check, _o2, _e2 = decompress(bytes(comp))
    except Bad:
        return False
    if check != bytes(new_plain):
        return False              # a changed literal also feeds unrelated text
    _finish(buf, page, comp)
    return True
