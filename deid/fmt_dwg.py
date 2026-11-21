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
        if csize - end <= 64 or not any(comp[end:]):
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
    new_plain = bytes(new_plain)
    if new_plain == plain:
        return False
    # 1. cheapest and most faithful: change the literal bytes the text comes from
    want = {}
    simple = True
    for j in range(len(plain)):
        if plain[j] != new_plain[j]:
            src = origin[j]
            if want.get(src, new_plain[j]) != new_plain[j]:
                simple = False
                break
            want[src] = new_plain[j]
    if simple:
        patched = bytearray(comp)
        for src, val in want.items():
            patched[src] = val
        try:
            check, _o2, _e2 = decompress(bytes(patched))
        except Bad:
            check = None
        if check == new_plain:
            _finish(buf, page, patched)
            return True
    # 2. the text is produced by back-references (or a changed literal also
    #    feeds unrelated bytes): write a fresh stream of the same size
    try:
        fresh = compress(new_plain, csize)
        if fresh is None:
            return False
        check, _o3, _e3 = decompress(fresh)
    except Bad:
        return False
    if check != new_plain or len(fresh) != csize:
        return False
    _finish(buf, page, bytearray(fresh))
    return True


# ---------------------------------------------------------------------------
# object data (AcDb:AcDbObjects)
# ---------------------------------------------------------------------------
# The section starts with the marker 0x0DCA and holds the objects one after
# another. Every object is: a modular short (its size in bytes), from AC1024
# on an unsigned modular char (size of its handle stream in bits), `size`
# bytes of bit-packed data, and a CRC-16 (seed 0xC0C1) over everything from
# the first size byte up to the CRC. Text inside the data is bit-aligned
# arbitrarily, so it has to be searched and patched at bit level, and the
# CRC of every touched object has to be recomputed.
OBJ_MARK = b"\xca\x0d\x00\x00"


def _crc_tables():
    t8 = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        t8.append(c)
    t16 = [0] * 65536
    for x in range(65536):
        lo = t8[x & 0xFF]
        t16[x] = (lo >> 8) ^ t8[((x >> 8) ^ lo) & 0xFF]
    return t8, t16


_T8, _T16 = _crc_tables()


def crc16(data, seed=0xC0C1):
    """CRC-16 of the DWG format (reflected polynomial 0xA001)."""
    c = seed
    n = len(data)
    even = n & ~1
    if even:
        t16 = _T16
        for w in struct.unpack("<%dH" % (even >> 1), bytes(data[:even])):
            c = t16[c ^ w]
    if n & 1:
        c = (c >> 8) ^ _T8[(c ^ data[n - 1]) & 0xFF]
    return c


def _ms(d, i):
    val = 0
    shift = 0
    n = len(d)
    while True:
        if i + 2 > n or shift > 45:
            return None, i
        w = d[i] | (d[i + 1] << 8)
        i += 2
        val |= (w & 0x7FFF) << shift
        shift += 15
        if not w & 0x8000:
            return val, i


def _umc(d, i):
    val = 0
    shift = 0
    n = len(d)
    while True:
        if i >= n or shift > 63:
            return None, i
        b = d[i]
        i += 1
        val |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return val, i


def section_data(buf, name, pagelist=None, names=None):
    """Concatenated plain content of one named section and its page layout:
    (data, [(start offset in data, length, page, mode)]). Returns (None, None)
    when a page cannot be read or the pages do not tile the section."""
    names = names if names is not None else section_names(buf)
    pagelist = pagelist if pagelist is not None else pages(buf)
    mine = [p for p in pagelist if names.get(p["hdr"][1]) == name]
    if not mine:
        return None, None
    mine.sort(key=lambda p: p["hdr"][4] | (p["hdr"][5] << 32))
    out = bytearray()
    layout = []
    for p in mine:
        plain, mode = read_page(buf, p)
        if plain is None:
            return None, None
        start = p["hdr"][4] | (p["hdr"][5] << 32)
        if start != len(out):
            return None, None
        layout.append((start, len(plain), p, mode))
        out += plain
    return bytes(out), layout


def object_spans(d, version):
    """Objects at the start of the object section whose CRC-16 validates:
    list of (start, data start, CRC position). The walk stops at the first
    object that does not validate (padding, free space)."""
    out = []
    if bytes(d[:4]) != OBJ_MARK:
        return out
    new = bytes(version) >= b"AC1024"
    n = len(d)
    i = 4
    while i + 4 <= n:
        size, k = _ms(d, i)
        if not size:
            break
        if new:
            hs, k = _umc(d, k)
            if hs is None:
                break
        crcpos = k + size
        if crcpos + 2 > n:
            break
        if crc16(d[i:crcpos]) != (d[crcpos] | (d[crcpos + 1] << 8)):
            break
        out.append((i, k, crcpos))
        i = crcpos + 2
    return out


def shift_view(data, k):
    """The byte string seen when reading `data` from bit offset k (MSB first)."""
    if not k:
        return bytes(data)
    n = len(data)
    v = (int.from_bytes(data, "big") << k) & ((1 << (8 * n)) - 1)
    return v.to_bytes(n, "big")


def put_bits(buf, bitpos, payload):
    """Overwrite len(payload) bytes of bytearray `buf` starting at bit offset
    bitpos (MSB first); neighbouring bits are preserved."""
    j, k = divmod(bitpos, 8)
    if not k:
        buf[j:j + len(payload)] = payload
        return
    keep_hi = (0xFF << (8 - k)) & 0xFF
    keep_lo = 0xFF >> k
    for i, r in enumerate(payload):
        buf[j + i] = (buf[j + i] & keep_hi) | (r >> k)
        buf[j + i + 1] = (buf[j + i + 1] & keep_lo) | ((r << (8 - k)) & 0xFF)


# ---------------------------------------------------------------------------
# compressor (for pages whose new content cannot be reached by editing
# literal bytes of the original stream)
# ---------------------------------------------------------------------------
def _emit_long(out, v):
    k, b = divmod(v - 1, 0xFF)
    out += b"\x00" * k
    out.append(b + 1)


def _emit_literals(out, run):
    """Literal run of 4 or more bytes: length opcode, then the bytes."""
    ln = len(run)
    if ln <= 18:
        out.append(ln - 3)
    else:
        out.append(0)
        _emit_long(out, ln - 18)
    out += run


def _emit_match(out, length, off, lit):
    if 3 <= length <= 14 and off <= 0x3FF:
        out.append(((length + 1) << 4) | ((off & 3) << 2) | lit)
        out.append(off >> 2)
    elif off <= 0x3FFF:
        if length <= 33:
            out.append(0x1E + length)
        else:
            out.append(0x20)
            _emit_long(out, length - 0x21)
        out.append(((off & 0x3F) << 2) | lit)
        out.append(off >> 6)
    else:
        o = off - 0x3FFF
        if length <= 17:
            out.append(0x10 | (length - 2))
        else:
            out.append(0x10)
            _emit_long(out, length - 9)
        out.append(((o & 0x3F) << 2) | lit)
        out.append(o >> 6)


def _best(data, table, i, n, depth):
    """Longest earlier match for position i: (length, offset) or (0, 0)."""
    cands = table.get(data[i:i + 3])
    if not cands:
        return 0, 0
    best_len = 0
    best_off = 0
    limit = n - i
    for c in cands[-depth:][::-1]:
        dist = i - c
        if dist > 0x7FFF:
            break
        ln = 3
        while ln < limit and data[c + ln] == data[i + ln]:
            ln += 1
        # a match must be shorter to write than its bytes: 2 bytes for a
        # near one of up to 14, 3 bytes otherwise (4 with a far offset)
        if ln > best_len and (ln >= 5 or (ln == 4 and dist <= 0x4000) or dist <= 0x400):
            best_len = ln
            best_off = dist - 1
            if ln == limit:
                break
    return best_len, best_off


def _tokens(data, depth=48, lazy=False):
    """LZ77 parse: list of (literal bytes, match length, match offset); the
    last token has match length 0. With lazy=True a match is deferred by one
    byte when the next position offers a longer one."""
    n = len(data)
    table = {}
    toks = []
    lit_start = 0
    i = 0
    while i < n:
        best_len = 0
        best_off = 0
        if 4 <= i <= n - 3:
            best_len, best_off = _best(data, table, i, n, depth)
            if lazy and 3 <= best_len < 64 and i + 1 <= n - 3:
                table.setdefault(data[i:i + 3], []).append(i)
                nl, _no = _best(data, table, i + 1, n, depth)
                if nl > best_len + 1:
                    i += 1                      # keep this byte as a literal
                    continue
                table[data[i:i + 3]].pop()
        if best_len >= 3:
            toks.append((data[lit_start:i], best_len, best_off))
            end = i + best_len
            stop = min(end, n - 2)
            while i < stop:
                table.setdefault(data[i:i + 3], []).append(i)
                i += 1
            i = end
            lit_start = i
        else:
            if i <= n - 3:
                table.setdefault(data[i:i + 3], []).append(i)
            i += 1
    toks.append((data[lit_start:n], 0, 0))
    return toks


def _serialize(toks):
    """Tokens -> stream. A literal run of 1-3 bytes rides on the match before
    it; a longer one gets its own length opcode."""
    out = bytearray()
    first = toks[0][0]
    if len(first) < 4:
        raise Bad("first literal run too short")
    _emit_literals(out, first)
    for idx, (_lit, mlen, moff) in enumerate(toks):
        if not mlen:
            break
        nxt = toks[idx + 1][0]
        if len(nxt) <= 3:
            _emit_match(out, mlen, moff, len(nxt))
            out += nxt
        else:
            _emit_match(out, mlen, moff, 0)
            _emit_literals(out, nxt)
    out += b"\x11\x00\x00"
    return out


def compress(data, size=None):
    """Compress one page. With `size`, the stream is made to fit: it is
    padded with zero bytes after the terminator, and when it would be much
    shorter than `size` some matches are written as literals instead so that
    the padding stays small. Returns None when it cannot fit."""
    data = bytes(data)
    if len(data) < 8:
        return None
    toks = _tokens(data)
    out = _serialize(toks)
    if size is None:
        return bytes(out)
    if len(out) > size:
        # tight page: search harder before giving up
        for depth, lazy in ((256, True), (4096, True)):
            toks = _tokens(data, depth, lazy)
            out = _serialize(toks)
            if len(out) <= size:
                break
        else:
            return None
    if size - len(out) > 24:
        # give space back: expand the shortest matches to literals, front to back
        pos = 0
        spans = []
        for lit, mlen, moff in toks:
            pos += len(lit)
            spans.append(pos)
            pos += mlen
        order = sorted((mlen, i) for i, (_l, mlen, _o) in enumerate(toks) if mlen)
        drop = set()
        slack = size - len(out)
        for mlen, i in order:
            if slack <= 24:
                break
            gain = mlen - 2                      # a match costs 2-4 bytes, its literals mlen
            if gain <= 0 or gain > slack - 4:
                continue
            drop.add(i)
            slack -= gain + 2                    # allow for a new length opcode
        if drop:
            merged = []
            cur = bytearray()
            for i, (lit, mlen, moff) in enumerate(toks):
                cur += lit
                if mlen and i in drop:
                    cur += data[spans[i]:spans[i] + mlen]
                else:
                    merged.append((bytes(cur), mlen, moff))
                    cur = bytearray()
            if merged and len(merged[0][0]) >= 4:
                out2 = _serialize(merged)
                if len(out2) <= size:
                    out = out2
    return bytes(out) + b"\x00" * (size - len(out))
