"""Embedded drawing previews.

A drawing file carries bitmap previews of its sheets (title block included).
They are coarsened so that no text in them remains legible, while keeping the
picture size, colour model and - most importantly - the exact byte length of
the stream, so the surrounding container does not change shape.

  PNG       : re-encoded from a block-averaged copy; padded to the original
              length with a private ancillary chunk placed before IEND.
  DIB RLE8  : palette indices are reduced block-wise (most frequent index),
              re-encoded as RLE8 and zero-padded after the end-of-bitmap code.
"""
import io
import struct
import zlib

BLOCKS = (6, 8, 12, 16, 24, 32)


def _png_chunk(typ, data):
    return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)


def coarsen_png(data):
    """Return a PNG of exactly len(data) bytes showing a coarsened picture, or None."""
    from PIL import Image
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return None
    w, h = im.size
    if w < 32 or h < 32 or w * h > 40_000_000:
        return None
    for block in BLOCKS:
        small = im.resize((max(1, w // block), max(1, h // block)), Image.BOX)
        big = small.resize((w, h), Image.NEAREST)
        out = io.BytesIO()
        big.save(out, "PNG", optimize=True)
        new = out.getvalue()
        pad = len(data) - len(new) - 12
        if pad >= 0 and new.endswith(b"IEND\xaeB`\x82"):
            body, iend = new[:-12], new[-12:]
            res = body + _png_chunk(b"prVw", b"\x00" * pad) + iend
            if len(res) == len(data):
                try:
                    chk = Image.open(io.BytesIO(res))
                    chk.load()
                    if chk.size == (w, h):
                        return res
                except Exception:
                    return None
    return None


def _rle8(rows):
    """Encode rows (bottom-up list of bytes) as BMP RLE8."""
    out = bytearray()
    for row in rows:
        i = 0
        n = len(row)
        while i < n:
            v = row[i]
            j = i + 1
            while j < n and row[j] == v and j - i < 255:
                j += 1
            out.append(j - i)
            out.append(v)
            i = j
        out += b"\x00\x00"
    out += b"\x00\x01"
    return bytes(out)


def coarsen_dib(data):
    """SolidWorks 'Preview' stream: 4-byte prefix + BITMAPINFOHEADER + palette
    + RLE8 pixels. Returns same-length bytes or None."""
    from PIL import Image
    import numpy as np
    if len(data) < 60 or data[4:8] != b"\x28\x00\x00\x00":
        return None
    w, h, planes, bpp, comp, size_image, _xp, _yp, clr_used, _imp = struct.unpack_from("<iiHHIIiiII", data, 8)
    if bpp != 8 or comp != 1 or w <= 0 or h <= 0 or w * h > 40_000_000:
        return None
    ncol = clr_used or 256
    pix_off = 4 + 40 + 4 * ncol
    if pix_off >= len(data):
        return None
    bmp = b"BM" + struct.pack("<IHHI", 14 + len(data) - 4, 0, 0, 14 + 40 + 4 * ncol) + data[4:]
    try:
        im = Image.open(io.BytesIO(bmp))
        im.load()
    except Exception:
        return None
    if im.mode != "P" or im.size != (w, h):
        return None
    arr = np.asarray(im, dtype=np.uint8)
    for block in BLOCKS:
        out = arr.copy()
        for y in range(0, h, block):
            for x in range(0, w, block):
                cell = arr[y:y + block, x:x + block]
                vals, counts = np.unique(cell, return_counts=True)
                out[y:y + block, x:x + block] = vals[counts.argmax()]
        rows = [out[y].tobytes() for y in range(h - 1, -1, -1)]
        enc = _rle8(rows)
        room = len(data) - pix_off
        if len(enc) <= room:
            res = data[:pix_off] + enc + b"\x00" * (room - len(enc))
            return res if len(res) == len(data) else None
    return None


def coarsen(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return coarsen_png(data)
    return coarsen_dib(data)
