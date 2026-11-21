"""Image metadata (JPEG EXIF / XMP / comments, PNG text chunks) - in place.

Pixel data is never decoded or re-encoded. Text-valued EXIF tags and XMP
packets are located and returned as byte spans for same-length replacement;
GPS position data is neutralised by emptying the GPS directory (entry count
set to zero and its value bytes zeroed), which keeps every offset valid.
"""
import struct
import zlib

_TEXT_TAGS = {0x013B: ("exif.artist", "id"), 0x8298: ("exif.copyright", "text"),
              0x010E: ("exif.description", "text"), 0x013C: ("exif.hostcomputer", "id"),
              0x9C9B: ("exif.xptitle", "text"), 0x9C9C: ("exif.xpcomment", "text"),
              0x9C9D: ("exif.xpauthor", "id"), 0x9C9E: ("exif.xpkeywords", "text"),
              0x9C9F: ("exif.xpsubject", "text"), 0xA430: ("exif.ownername", "id"),
              0xA431: ("exif.serial", "id")}
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def jpeg_segments(buf):
    """Yield (marker, payload_start, payload_end) for metadata segments."""
    n = len(buf)
    if buf[:2] != b"\xff\xd8":
        return
    i = 2
    while i + 4 <= n:
        if buf[i] != 0xFF:
            return
        m = buf[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m == 0xDA or m == 0xD9:   # start of scan / end: metadata is over
            return
        ln = struct.unpack_from(">H", buf, i + 2)[0]
        if ln < 2 or i + 2 + ln > n:
            return
        yield m, i + 4, i + 2 + ln
        i += 2 + ln


def _ifd(buf, base, off, end, bo, out, gps, depth=0):
    """Walk one TIFF IFD. Appends text spans to out and GPS zero-ranges to gps."""
    if depth > 3 or off < 8 or base + off + 2 > end:
        return
    cnt = struct.unpack_from(bo + "H", buf, base + off)[0]
    if cnt > 400 or base + off + 2 + 12 * cnt > end:
        return
    for k in range(cnt):
        e = base + off + 2 + 12 * k
        tag, typ, num = struct.unpack_from(bo + "HHI", buf, e)
        size = _TYPE_SIZE.get(typ, 0) * num
        if size <= 4:
            voff = e + 8
        else:
            voff = base + struct.unpack_from(bo + "I", buf, e + 8)[0]
        if size <= 0 or voff + size > end:
            continue
        if tag in _TEXT_TAGS and typ in (1, 2, 7) and size > 1:
            label, ctx = _TEXT_TAGS[tag]
            enc = "utf-16-le" if 0x9C9B <= tag <= 0x9C9F else "latin-1"
            out.append((voff, voff + size, label, ctx, enc))
        elif tag == 0x8769 and typ == 4:      # Exif sub-IFD
            _ifd(buf, base, struct.unpack_from(bo + "I", buf, e + 8)[0], end, bo, out, gps, depth + 1)
        elif tag == 0x8825 and typ == 4:      # GPS IFD
            goff = struct.unpack_from(bo + "I", buf, e + 8)[0]
            g = base + goff
            if g + 2 <= end:
                gcnt = struct.unpack_from(bo + "H", buf, g)[0]
                if 0 < gcnt <= 64 and g + 2 + 12 * gcnt <= end:
                    for j in range(gcnt):
                        ge = g + 2 + 12 * j
                        gtag, gtyp, gnum = struct.unpack_from(bo + "HHI", buf, ge)
                        gsize = _TYPE_SIZE.get(gtyp, 0) * gnum
                        if gsize > 4:
                            gv = base + struct.unpack_from(bo + "I", buf, ge + 8)[0]
                            if gv + gsize <= end:
                                gps.append((gv, gv + gsize))
                    gps.append((g + 2, g + 2 + 12 * gcnt))   # the entries themselves
                    gps.append((g, g + 2))                    # entry count -> 0


def jpeg_plan_regions(buf):
    """Return (text_spans, xml_spans, gps_ranges) for a JPEG held in memory."""
    spans = []
    xml = []
    gps = []
    for m, a, b in jpeg_segments(buf):
        if m == 0xE1 and buf[a:a + 6] == b"Exif\x00\x00":
            base = a + 6
            if buf[base:base + 2] == b"II":
                bo = "<"
            elif buf[base:base + 2] == b"MM":
                bo = ">"
            else:
                continue
            try:
                off = struct.unpack_from(bo + "I", buf, base + 4)[0]
                _ifd(buf, base, off, b, bo, spans, gps)
            except struct.error:
                pass
        elif m == 0xE1 and buf[a:a + 29] == b"http://ns.adobe.com/xap/1.0/\x00":
            xml.append((a + 29, b))
        elif m == 0xFE:
            spans.append((a, b, "jpeg.comment", "text", "latin-1"))
    return spans, xml, gps


def png_text_chunks(buf):
    """Yield (data_start, data_end, crc_offset, type) for uncompressed PNG text chunks."""
    if buf[:8] != b"\x89PNG\r\n\x1a\n":
        return
    i = 8
    n = len(buf)
    while i + 12 <= n:
        ln = struct.unpack_from(">I", buf, i)[0]
        typ = bytes(buf[i + 4:i + 8])
        if i + 12 + ln > n:
            return
        if typ == b"tEXt" or (typ == b"iTXt" and b"\x00\x00\x00" in bytes(buf[i + 8:i + 8 + min(ln, 90)])):
            yield i + 8, i + 8 + ln, i + 8 + ln, typ
        if typ == b"IEND":
            return
        i += 12 + ln


def png_fix_crc(buf, data_start, data_end, crc_off):
    crc = zlib.crc32(bytes(buf[data_start - 4:data_end])) & 0xFFFFFFFF
    struct.pack_into(">I", buf, crc_off, crc)
