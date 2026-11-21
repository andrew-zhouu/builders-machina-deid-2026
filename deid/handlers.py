"""Format handlers: given an input file, write a de-identified copy.

Every handler returns (changed, info). When changed is False the caller links
the original bytes into the output unchanged. Handlers never modify bytes they
do not understand: they locate text-bearing fields, ask the engine for
same-length replacements inside them, apply those, and then validate the
result with an independent re-parse before it is accepted.
"""
import bisect
import mmap
import os
import re
import shutil
import zipfile
import zlib

from . import regions as R
from . import fmt_step, fmt_iges, fmt_sw, oleprops, engine as E
from .build_dict import label_class, name_class

OPTS = {"rename": False, "pdftext": False, "creo": False, "zlb": False, "strings": False, "dwg": False,
        "pdfextra": False, "pdfthumbs": True, "previews": False, "logos": None}
BLOB_RUN = 160
_B64 = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")


class Unsafe(Exception):
    """Raised when a post-write validation fails; the original is kept."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _contains(spans, starts, pos):
    k = bisect.bisect_right(starts, pos) - 1
    return k >= 0 and spans[k][0] <= pos < spans[k][1]


def not_in_blob(buf, n):
    """Predicate: position is not inside a long base64/hex run (embedded
    binary payloads in text files must never be edited)."""
    def ok(pos):
        a = pos
        lim = max(0, pos - BLOB_RUN)
        while a > lim and buf[a - 1] in _B64:
            a -= 1
        b = pos
        lim = min(n, pos + BLOB_RUN)
        while b < lim and buf[b] in _B64:
            b += 1
        return (b - a) < BLOB_RUN
    return ok


_DXF_CODES = frozenset([1, 2, 3, 8, 410, 1000] + list(range(300, 310)))


def dxf_ok(buf, n):
    def ok(pos):
        ls = buf.rfind(b"\n", 0, pos)
        if ls <= 0:
            return False
        ps = buf.rfind(b"\n", max(0, ls - 32), ls)
        try:
            code = int(bytes(buf[ps + 1:ls]).strip())
        except ValueError:
            return False
        if code not in _DXF_CODES:
            return False
        if code in (1, 3):
            # never touch (encrypted) ACIS text carried by modeler-geometry entities
            lo = max(0, pos - (8 << 20))
            k = buf.rfind(b"AcDb", lo, pos)
            if k < 0:
                return lo == 0
            if buf[k:k + 19] == b"AcDbModelerGeometry":
                return False
        return True
    return ok


def xml_ok(buf, n):
    """Text nodes, comments and quoted attribute values only."""
    blob = not_in_blob(buf, n)

    def ok(pos):
        if not blob(pos):
            return False
        lo = max(0, pos - 65536)
        lt = buf.rfind(b"<", lo, pos)
        gt = buf.rfind(b">", lo, pos)
        if gt >= lt:
            return True
        seg = bytes(buf[lt:pos])
        if seg.startswith(b"<!--") or seg.startswith(b"<![CDATA["):
            return True
        dq = seg.count(b'"')
        if dq:
            return dq % 2 == 1
        return seg.count(b"'") % 2 == 1
    return ok


def quoted_ok(buf, n):
    """Inside a double-quoted string on the same line (JSON, s-expressions)."""
    blob = not_in_blob(buf, n)

    def ok(pos):
        if not blob(pos):
            return False
        ls = buf.rfind(b"\n", max(0, pos - 65536), pos) + 1
        seg = bytes(buf[ls:pos]).replace(b'\\"', b"")
        return seg.count(b'"') % 2 == 1
    return ok


def comment_ok(buf, n, prefixes=(), inline=(), parens=False, quotes=False):
    """Lines starting with one of `prefixes`, text after an `inline` comment
    marker, text inside parentheses (parens=True) or double quotes."""
    def ok(pos):
        ls = buf.rfind(b"\n", max(0, pos - 65536), pos) + 1
        seg = bytes(buf[ls:pos])
        head = seg.lstrip()[:16] if seg.strip() else bytes(buf[ls:ls + 32]).lstrip()[:16]
        for p in prefixes:
            if head.startswith(p):
                return True
        for c in inline:
            if c in seg:
                return True
        if parens and seg.count(b"(") > seg.count(b")"):
            return True
        if quotes and seg.count(b'"') % 2 == 1:
            return True
        return False
    return ok


def sat_ok(buf, n):
    """ACIS text: only inside counted strings '@<len> <chars>'."""
    def ok(pos):
        a = buf.rfind(b"@", max(0, pos - 4096), pos)
        if a < 0:
            return False
        j = a + 1
        while j < pos and 0x30 <= buf[j] <= 0x39:
            j += 1
        if j == a + 1 or j >= pos or buf[j] != 0x20:
            return False
        ln = int(bytes(buf[a + 1:j]))
        return j + 1 <= pos < j + 1 + ln
    return ok


_PROSE_EXT = frozenset("""txt md markdown rst csv tsv tex log ini cfg conf yml yaml toml bat cmd sh ps1 py pyw c h cpp hpp cc
hh cxx java js mjs ts tsx jsx css scad ino pde m v vhd s asm inc mac lst agc go rs rb pl php lua tcl r jl
properties gitignore gitattributes mk cmake make dockerfile license readme authors contributors notice
changelog todo nfo desc""".split())
_XML_EXT = frozenset("xml svg html htm xhtml x3d dae rels xsd dtd vml xlf plist kml gpx config vcxproj csproj "
                     "prjpcb model".split())
_JSON_EXT = frozenset("json geojson ipynb kicad_pro kicad_prl gbrjob map webmanifest".split())
_QUOTED_EXT = frozenset("kicad_pcb kicad_sch kicad_mod kicad_sym kicad_wks kicad_dru sch lib dcm".split())


def text_rule(kind, name, buf, n):
    """Free-text eligibility predicate for a text-like file (or False)."""
    from . import util
    ext = util.split_ext(name)
    base = name.rsplit("/", 1)[-1].lower()
    if kind == "iges":
        return True
    if kind == "dxf":
        return dxf_ok(buf, n)
    head = bytes(buf[:256]).lstrip(b"\xef\xbb\xbf \t\r\n")
    if kind in ("svg", "xml") or ext in _XML_EXT or head.startswith((b"<?xml", b"<svg", b"<!DOCTYPE", b"<html")):
        return xml_ok(buf, n)
    if ext in _JSON_EXT or head[:1] in (b"{", b"["):
        return quoted_ok(buf, n)
    if ext in _QUOTED_EXT or head.startswith(b"(kicad"):
        return quoted_ok(buf, n)
    if ext == "sat" or ext == "sab":
        return sat_ok(buf, n)
    if ext in ("x_t", "xmt_txt"):
        end = buf.find(b"**END_OF_HEADER", 0, min(n, 65536))
        if end < 0:
            return False
        return lambda pos: pos < end
    if ext == "wrl":
        return comment_ok(buf, n, prefixes=(b"#",), inline=(b"#",), quotes=True)
    if ext in ("obj", "mtl"):
        return comment_ok(buf, n, prefixes=(b"#", b"o ", b"g ", b"usemtl", b"mtllib", b"newmtl"))
    if ext in ("ps", "eps"):
        return comment_ok(buf, n, prefixes=(b"%",), parens=True)
    if ext in ("gcode", "nc", "ngc", "tap", "gco"):
        return comment_ok(buf, n, prefixes=(b";", b"("), inline=(b";",), parens=True)
    if ext in ("gbr", "gbl", "gtl", "gbs", "gts", "gbo", "gto", "gbp", "gtp", "gko", "gm1", "gm2", "gm3", "gm",
               "drl", "pho", "g1", "g2", "gdd", "gdl", "gd1"):
        return comment_ok(buf, n, prefixes=(b"G04", b";", b"%TF", b"%TA", b"%TO"))
    if ext in _PROSE_EXT or base in _PROSE_EXT or (not ext and b"\x00" not in head):
        return not_in_blob(buf, n)
    return False


_XML_ID = re.compile(
    rb"<((?:dc:creator|dc:publisher|dc:rights|dc:contributor|cc:Agent|pdf:Author|xmp:Author|"
    rb"cp:lastModifiedBy|dc:lastModifiedBy|Company|Manager|pdfx:Company|pdfx:Manager|xmpRights:Owner|"
    rb"photoshop:Credit))\b[^>]*>(.{0,4000}?)</\1>", re.S)
_FC_ID = re.compile(rb"<Property name=\"(?:CreatedBy|LastModifiedBy|Company)\"[^>]*>\s*<\w+ value=\"([^\"]{1,400})\"")
_3MF_ID = re.compile(rb"<metadata\s+name=\"(?:Designer|Author|Copyright|Creator|Company)\"[^>]*>([^<]{1,400})</metadata>", re.I)
_SWPROP = re.compile(rb"<property\b[^>]*\bname=\"([^\"]{1,80})\"[^>]*>\s*<vt:(?:lpstr|lpwstr)>([^<]{1,400})</vt:")
_XT_ID = re.compile(rb"\b(?:USER|SITE)=([^;\r\n]{1,200});")
_PS_ID = re.compile(rb"^%%(?:For|Author|Routing|Copyright)\s*:\s*(.{1,200}?)\r?$", re.M)
_DXF_ID = re.compile(rb"\$LASTSAVEDBY\s*\r?\n\s*1\s*\r?\n([^\r\n]{1,200})")


def _text_nodes(buf, a, b):
    """Spans of character data (outside tags) in buf[a:b]."""
    out = []
    i = a
    while i < b:
        if buf[i] == 0x3C:
            k = buf.find(b">", i, b)
            if k < 0:
                break
            i = k + 1
        else:
            k = buf.find(b"<", i, b)
            if k < 0:
                k = b
            out.append((i, k))
            i = k
    return out


def field_regions(buf, n, kind, name=""):
    """Identity-field spans for text formats, as (start, end) tuples."""
    ids = []
    head = bytes(buf[:min(n, 8 << 20)])
    if kind in ("svg", "xml", "text"):
        if b"<" in head[:4096] or kind != "text":
            for m in _XML_ID.finditer(head):
                ids.extend(_text_nodes(head, m.start(2), m.end(2)))
            for m in _FC_ID.finditer(head[:1 << 18]):
                ids.append(m.span(1))
            for m in _3MF_ID.finditer(head[:1 << 18]):
                ids.append(m.span(1))
            for m in _SWPROP.finditer(head):
                nm = m.group(1).decode("latin-1")
                if name_class(nm) or nm.lower() == "author":
                    ids.append(m.span(2))
        for m in _XT_ID.finditer(head[:8192]):
            ids.append(m.span(1))
        for m in _PS_ID.finditer(head[:8192]):
            ids.append(m.span(1))
    elif kind == "dxf":
        for m in _DXF_ID.finditer(head[:1 << 20]):
            ids.append(m.span(1))
    return ids


def plan_text(eng, buf, n, kind, name="", anchors=True, fixed=False):
    """Replacement plan for a text-like buffer. Returns list of (s, e, bytes)."""
    regs = []
    text_ok = True
    lo, hi = 0, n
    if kind == "step":
        regs, _hdr = fmt_step.step_regions(buf, n)
    elif kind == "iges":
        regs = fmt_iges.iges_regions(buf, n)
    if anchors:
        regs = regs + R.scan_userpaths(buf, n)
        if kind != "iges":
            regs += [r for r in R.scan_emails(buf, n, limit=20000) if r.label == "email.local"]
            regs += [r for r in R.scan_urls(buf, n, limit=20000) if r.label == "url.forge.owner"]
    ids = [(r.start, r.end) for r in regs if r.ctx == "id" and r.omap is None]
    ids += field_regions(buf, n, kind, name)
    ids = sorted(set(ids))
    # drop spans nested in / overlapping an earlier one
    flat = []
    for a, b in ids:
        if flat and a < flat[-1][1]:
            if b > flat[-1][1]:
                flat[-1] = (flat[-1][0], b)
            continue
        flat.append((a, b))
    ids = flat
    keeps = sorted((r.start, r.end) for r in regs if r.ctx == "keep")
    kstarts = [a for a, _b in keeps]
    if kind == "step":
        def text_ok(pos):
            if fmt_step.in_comment(buf, pos):
                return True
            return fmt_step.in_string(buf, pos) and not _contains(keeps, kstarts, pos)
    else:
        text_ok = text_rule(kind, name, buf, n)
    from . import util as _u
    strict = (fixed or kind == "iges" or text_ok is False
              or _u.split_ext(name) in ("sat", "sab", "x_t", "xmt_txt", "unv", "neu", "brep", "brp"))
    plan = eng.plan_bytes(buf, ids, text_ok, lo, hi, fixed=strict, wide=False)
    taken = set()
    for s, e, _nb in plan:
        taken.update(range(s, e))
    for r in regs:
        if r.omap is None or r.ctx not in ("id", "text"):
            continue
        logical = r.read(buf)
        sub = eng.plan_bytes(logical, [(0, len(logical))] if r.ctx == "id" else [], True, fixed=True)
        for s, e, nb in sub:
            for i in range(s, e):
                o = r.omap[i]
                if o not in taken:
                    taken.add(o)
                    plan.append((o, o + 1, nb[i - s:i - s + 1]))
    return plan


def patch_file(src, dst, plan):
    """Copy src to dst applying the plan. Same-length plans are patched in
    place; otherwise the file is re-assembled from the untouched segments."""
    if all(len(nb) == e - s for s, e, nb in plan):
        shutil.copyfile(src, dst)
        with open(dst, "r+b") as fh:
            for s, e, nb in plan:
                fh.seek(s)
                fh.write(nb)
        return
    plan = sorted(plan)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        pos = 0
        for s, e, nb in plan:
            if s < pos:
                raise Unsafe("overlapping patches")
            fin.seek(pos)
            left = s - pos
            while left > 0:
                chunk = fin.read(min(left, 1 << 22))
                if not chunk:
                    raise Unsafe("short read")
                fout.write(chunk)
                left -= len(chunk)
            fout.write(nb)
            pos = e
        fin.seek(pos)
        shutil.copyfileobj(fin, fout, 1 << 22)


# ---------------------------------------------------------------------------
# text-like files (STEP, IGES, DXF, SVG, generic text)
# ---------------------------------------------------------------------------
def h_textlike(eng, src, dst, kind, name):
    size = os.path.getsize(src)
    if size == 0:
        return False, {}
    with open(src, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as buf:
        if buf[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return False, {"note": "utf16-text-skipped"}
        plan = plan_text(eng, buf, size, kind, name)
    if not plan:
        return False, {}
    patch_file(src, dst, plan)
    delta = sum(len(nb) - (e - s) for s, e, nb in plan)
    if os.path.getsize(dst) != size + delta:
        raise Unsafe("unexpected output size")
    return True, {"n": len(plan)}


def h_stl(eng, src, dst, name):
    size = os.path.getsize(src)
    with open(src, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as buf:
        head = bytes(buf[:80])
        plan = []
        if head[:5].lower() == b"solid" and b"\x00" not in bytes(buf[:256]):
            first = buf.find(b"\n", 0, min(size, 1024))
            if first > 0:
                ids = [(r.start, r.end) for r in R.scan_userpaths(buf, first)]
                plan = eng.plan_bytes(buf, ids, True, 0, first, fixed=True)
                tail = max(first, size - 512)
                k = buf.rfind(b"endsolid", tail, size)
                if k >= 0:
                    plan += eng.plan_bytes(buf, [], True, k, size, fixed=True)
        else:
            txt = head.rstrip(b"\x00 ")
            if txt and sum(1 for c in txt if 32 <= c < 127) >= len(txt) * 0.9:
                ids = [(r.start, r.end) for r in R.scan_userpaths(buf, 80)]
                plan = eng.plan_bytes(buf, ids, True, 0, 80, fixed=True)
    if not plan:
        return False, {}
    patch_file(src, dst, plan)
    return True, {"n": len(plan)}


# ---------------------------------------------------------------------------
# PDF metadata (Info dictionary and XMP packets); page content is untouched
# ---------------------------------------------------------------------------
_USERPATH_T = re.compile(
    r"(?i)(?<=[\\/])(?:Users|Documents and Settings|DOCUME~1|home)[\\/]+([^\\/:*?\"<>|\r\n]{1,64})(?=[\\/])")
_EMAIL_T = re.compile(r"([A-Za-z0-9._%+-]{1,64})@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def replace_str(eng, s, ctx="text"):
    """Replace identities in a real string (free length)."""
    if not s:
        return s
    if ctx == "id":
        return eng.replace_text(s, "id")[0]
    spans = [m.span(1) for m in _USERPATH_T.finditer(s)] + [m.span(1) for m in _EMAIL_T.finditer(s)]
    if not spans:
        return eng.replace_text(s, "text")[0]
    spans.sort()
    out = []
    pos = 0
    for a, b in spans:
        if a < pos:
            continue
        out.append(eng.replace_text(s[pos:a], "text")[0])
        out.append(eng.replace_text(s[a:b], "id")[0])
        pos = b
    out.append(eng.replace_text(s[pos:], "text")[0])
    return "".join(out)


_PDF_SKIP = {"/CreationDate", "/ModDate", "/Trapped", "/Producer", "/Creator"}


_LOGOS = {}


def _logo_set(group):
    """Digests of repeated small images (logo candidates) of a source group,
    from the file written by pdfharvest.py (OPTS["logos"])."""
    path = OPTS.get("logos")
    if not path:
        return ()
    if path not in _LOGOS:
        import json
        try:
            with open(path, encoding="utf-8") as fh:
                _LOGOS[path] = {g: frozenset(v) for g, v in json.load(fh).items()}
        except (OSError, ValueError):
            _LOGOS[path] = {}
    return _LOGOS[path].get(group, ())


def _replace_logos(doc, logos):
    """Replace logo images by a neutral grey picture of the same pixel size."""
    import hashlib
    import pymupdf
    done = set()
    count = 0
    for page in doc:
        try:
            images = page.get_images(full=True)
        except Exception:
            continue
        for img in images:
            xref = img[0]
            if xref in done:
                continue
            try:
                raw = doc.xref_stream_raw(xref)
            except Exception:
                continue
            if not raw or hashlib.md5(raw).hexdigest() not in logos:
                continue
            done.add(xref)
            w, h = max(1, img[2]), max(1, img[3])
            pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, w, h), False)
            pix.set_rect(pix.irect, (225,))
            try:
                page.replace_image(xref, pixmap=pix)
                count += 1
            except Exception:
                pass
    return count


def _pdf_visible_text(eng, src, tmp):
    """Replace identities in visible page text with PyMuPDF. Writes `tmp` and
    returns the number of replacements (0: nothing written)."""
    import pymupdf
    from . import pdftext
    total = 0
    doc = pymupdf.open(src)
    try:
        if doc.needs_pass or doc.is_encrypted or doc.page_count > 3000:
            return 0
        images_before = []
        touched = []
        for page in doc:
            plan = pdftext.page_plan(eng, page)
            if not plan:
                continue
            images_before.append((page.number, len(page.get_images())))
            total += pdftext.apply_page(page, plan)
            touched.append(page.number)
        logos = _logo_set(eng.group)
        if logos:
            total += _replace_logos(doc, logos)
        if not total:
            return 0
        for num, cnt in images_before:
            if len(doc[num].get_images()) != cnt:
                raise Unsafe("pdf page lost an image")
        doc.save(tmp, garbage=3, deflate=True)
    finally:
        doc.close()
    return total


def h_pdf(eng, src, dst, name):
    import pikepdf
    n = 0
    ntext = 0
    orig_src = src
    if OPTS.get("pdftext"):
        tmp1 = dst + ".txt~"
        try:
            ntext = _pdf_visible_text(eng, src, tmp1)
        except Exception:
            ntext = 0
            try:
                os.unlink(tmp1)
            except OSError:
                pass
        if ntext:
            src = tmp1
    try:
        return _pdf_meta(eng, src, dst, name, ntext, orig_src)
    finally:
        if src != orig_src:
            try:
                os.unlink(src)
            except OSError:
                pass


def _pdf_extras(eng, pdf, text_changed):
    """Bookmarks, annotation authors / comments, page thumbnails and form
    field values. Returns the number of changes."""
    import pikepdf
    n = 0

    def fix(obj, key, ctx):
        nonlocal n
        try:
            v = obj.get(key)
            if isinstance(v, pikepdf.String):
                s = str(v)
                new = replace_str(eng, s, ctx)
                if new != s:
                    obj[key] = pikepdf.String(new)
                    n += 1
        except Exception:
            pass

    # outlines (bookmarks)
    try:
        seen = set()
        stack = []
        root = pdf.Root.get("/Outlines")
        if root is not None and root.get("/First") is not None:
            stack.append(root.get("/First"))
        guard = 0
        while stack and guard < 20000:
            guard += 1
            item = stack.pop()
            try:
                og = item.objgen
            except Exception:
                og = None
            if og in seen:
                continue
            seen.add(og)
            fix(item, "/Title", "text")
            for k in ("/First", "/Next"):
                nx = item.get(k)
                if nx is not None:
                    stack.append(nx)
    except Exception:
        pass
    # annotations and thumbnails
    try:
        for page in pdf.pages:
            try:
                annots = page.get("/Annots")
                if annots is not None:
                    for a in annots:
                        st = str(a.get("/Subtype"))
                        if st in ("/Link",):
                            continue
                        fix(a, "/T", "id" if st != "/Widget" else "text")
                        fix(a, "/Contents", "text")
                        fix(a, "/Subj", "text")
                        if st == "/Widget":
                            fix(a, "/V", "text")
                            fix(a, "/TU", "text")
                # a page thumbnail is an embedded preview of the old page image
                if "/Thumb" in page.obj and (text_changed or OPTS.get("pdfthumbs")):
                    del page.obj["/Thumb"]
                    n += 1
            except Exception:
                continue
    except Exception:
        pass
    return n


def _pdf_meta(eng, src, dst, name, ntext, orig_src):
    import pikepdf
    n = 0
    with pikepdf.open(src) as pdf:
        npages = len(pdf.pages)
        info = pdf.trailer.get("/Info")
        if isinstance(info, pikepdf.Dictionary):
            for k in list(info.keys()):
                try:
                    v = info[k]
                except Exception:
                    continue
                if not isinstance(v, pikepdf.String) or k in _PDF_SKIP:
                    continue
                s = str(v)
                key = str(k).lstrip("/")
                ctx = "id" if label_class("pdf.info." + key.lower()) else "text"
                new = replace_str(eng, s, ctx)
                if new != s:
                    info[k] = pikepdf.String(new)
                    n += 1
        seen = set()
        cands = []
        try:
            md = pdf.Root.get("/Metadata")
            if md is not None:
                cands.append(md)
        except Exception:
            pass
        nx = 0
        for obj in cands:
            try:
                if not isinstance(obj, pikepdf.Stream) or obj.objgen in seen:
                    continue
                seen.add(obj.objgen)
                raw = obj.read_bytes()
            except Exception:
                continue
            plan = plan_text(eng, raw, len(raw), "xml")
            if plan:
                buf = bytearray(raw)
                E.apply_plan(buf, plan)
                obj.write(bytes(buf))
                nx += 1
        nextra = 0
        if OPTS.get("pdfextra"):
            nextra = _pdf_extras(eng, pdf, ntext > 0)
        n += nextra
        if not (n or nx or ntext):
            return False, {}
        enc = pdf.is_encrypted
        pdf.save(dst, encryption=True if enc else False, fix_metadata_version=False)
    # validation: same page count as the original input
    with pikepdf.open(orig_src) as o, pikepdf.open(dst) as b:
        if len(b.pages) != len(o.pages):
            raise Unsafe("pdf page count changed")
    # metadata step must leave decoded page content identical to its input
    with pikepdf.open(src) as a, pikepdf.open(dst) as b:
        if len(b.pages) != npages:
            raise Unsafe("pdf page count changed")
        for pa, pb in zip(a.pages, b.pages):
            ca, cb = pa.get("/Contents"), pb.get("/Contents")
            if (ca is None) != (cb is None):
                raise Unsafe("pdf page contents changed")
            if ca is None:
                continue
            try:
                da = b"".join(x.read_bytes() for x in (ca if isinstance(ca, pikepdf.Array) else [ca]))
                db = b"".join(x.read_bytes() for x in (cb if isinstance(cb, pikepdf.Array) else [cb]))
            except Exception:
                continue
            if da != db:
                raise Unsafe("pdf page contents changed")
    info = {"n": n + nx + ntext}
    if ntext:
        info["pdftext"] = ntext
    return True, info


# ---------------------------------------------------------------------------
# MFC-serialised UTF-16 strings (SolidWorks binary streams)
# ---------------------------------------------------------------------------
def mfc_strings(data, limit=200000):
    """Spans (start, end) of CString payloads serialised by MFC CArchive:
    FF FE FF <len8> | FF FE FF FF <len16>, followed by len UTF-16LE chars.
    A span is accepted only when all its characters are plausible text."""
    out = []
    n = len(data)
    i = data.find(b"\xff\xfe\xff")
    while i >= 0 and len(out) < limit:
        p = i + 3
        ln = -1
        if p < n:
            if data[p] != 0xFF:
                ln = data[p]
                p += 1
            elif p + 3 <= n:
                ln = data[p + 1] | (data[p + 2] << 8)
                p += 3
                if ln == 0xFFFF:
                    ln = -1
        if ln > 0 and p + 2 * ln <= n:
            seg = data[p:p + 2 * ln]
            ok = True
            for k in range(0, 2 * ln, 2):
                lo_b, hi_b = seg[k], seg[k + 1]
                if hi_b == 0:
                    if lo_b < 0x20 and lo_b not in (9, 10, 13):
                        ok = False
                        break
                elif 0xD8 <= hi_b <= 0xDF or hi_b == 0xFF:
                    ok = False
                    break
            if ok:
                out.append((p, p + 2 * ln))
                i = data.find(b"\xff\xfe\xff", p + 2 * ln)
                continue
        i = data.find(b"\xff\xfe\xff", i + 1)
    return out


def plan_mfc(eng, data):
    """Plan for a binary stream: only inside validated MFC strings."""
    plan = []
    for a, b in mfc_strings(data):
        seg = data[a:b]
        ids = [(r.start, r.end) for r in R.scan_userpaths_utf16(seg, len(seg))]
        for s, e, nb in eng.plan_bytes(seg, ids, True, fixed=True):
            plan.append((a + s, a + e, nb))
    return plan


_SW_SKIP = ("Preview", "PreviewPNG")


def _sw_stream_plan(eng, name, data):
    low = name.lower()
    if name in _SW_SKIP or low.startswith("images/") or low.endswith("__zlb"):
        return []
    if not data:
        return []
    head = data[:64].lstrip(b"\xef\xbb\xbf \r\n\t")
    if head.startswith(b"<"):
        return plan_text(eng, data, len(data), "xml", name)
    if data[:4] == b"\xfe\xff\x00\x00":
        return plan_propset(eng, data)
    if OPTS.get("strings"):
        return plan_strings(eng, data)
    return plan_mfc(eng, data)


def h_sw(eng, src, dst, name):
    with open(src, "rb") as fh:
        raw = fh.read()
    streams = fmt_sw.parse(raw)
    if streams is None:
        return False, {"note": "sw-unparsed"}
    buf = bytearray(raw)
    n = 0
    nofit = 0
    is_drawing = util_ext(name) in ("slddrw", "drwdot")
    nprev = 0
    for s in streams:
        if is_drawing and OPTS.get("previews") and (s.name in _SW_SKIP or s.name.startswith("Images/")):
            # sheet previews show the title block: coarsen them in place
            try:
                pdata = fmt_sw.read_stream(raw, s)
            except Exception:
                continue
            from . import fmt_preview
            new = fmt_preview.coarsen(pdata)
            if new is not None and new != pdata and fmt_sw.write_stream(buf, s, new, pdata):
                nprev += 1
            continue
        if s.usize > (96 << 20) or s.name in _SW_SKIP:
            continue
        try:
            data = fmt_sw.read_stream(raw, s)
        except Exception:
            return False, {"note": "sw-stream-crc"}
        plan = _sw_stream_plan(eng, s.name, data)
        if not plan:
            continue
        nd = bytearray(data)
        E.apply_plan(nd, plan)
        if fmt_sw.write_stream(buf, s, bytes(nd), data):
            n += len(plan)
        else:
            nofit += 1
    n += nprev
    if not n:
        return False, ({"sw_nofit": nofit} if nofit else {})
    try:
        cnt = fmt_sw.validate(buf)
    except Exception as ex:
        raise Unsafe("sw validation: %s" % ex)
    if cnt != len(streams) or len(buf) != len(raw):
        raise Unsafe("sw structure changed")
    with open(dst, "wb") as fh:
        fh.write(buf)
    info = {"n": n}
    if nofit:
        info["sw_nofit"] = nofit
    if nprev:
        info["previews"] = nprev
    return True, info


# ---------------------------------------------------------------------------
# OLE compound files (Inventor, Solid Edge, pre-2015 SolidWorks, ...)
# ---------------------------------------------------------------------------
def plan_propset(eng, data):
    plan = []
    for ps in oleprops.parse(data):
        lab = ps.label()
        if lab == "ole.setname":
            continue
        seg = data[ps.off:ps.off + ps.nbytes]
        cls = label_class(lab)
        if cls in ("PERSON", "ORG", "ACCOUNT"):
            ids = [(0, len(seg))]
        elif ps.enc == "utf-16-le":
            ids = [(r.start, r.end) for r in R.scan_userpaths_utf16(seg, len(seg))]
        else:
            ids = [(r.start, r.end) for r in R.scan_userpaths(seg, len(seg))]
        if ps.enc not in ("utf-16-le", "utf-8", "cp1252", "latin-1", "ascii", "gbk", "cp936"):
            # other code pages: only pure-ASCII values are safe to rewrite bytewise
            if not seg.isascii():
                continue
        sub = eng.plan_bytes(seg, ids, True, fixed=False)
        if not sub:
            continue
        if all(len(nb) == e - s for s, e, nb in sub):
            for s, e, nb in sub:
                plan.append((ps.off + s, ps.off + e, nb))
        else:
            # accented multi-byte text became shorter: keep the slot size by
            # padding the string with NUL terminators
            nseg = bytearray(seg)
            E.apply_plan(nseg, sub)
            if len(nseg) > len(seg):
                continue
            nseg += b"\x00" * (len(seg) - len(nseg))
            plan.append((ps.off, ps.off + ps.nbytes, bytes(nseg)))
    return plan


def string_run_ok(data, n):
    """Predicate for unparsed binary streams: is the match [s, e) text?

    A dictionary match is accepted only when it is unmistakably part of a
    string, so that a chance byte pattern inside geometry or compressed data
    can never be edited:
      * it lies in a run of at least 12 printable characters (a path, a
        sentence, markup), or
      * it *is* a complete string: the printable run is exactly the match and
        is NUL-terminated or preceded by its own length byte.
    The same rules apply to UTF-16LE text (code units instead of bytes)."""
    def ok(s, e):
        if s + 1 < n and data[s + 1] == 0 and 0x20 <= data[s] < 0x7F:
            a = s
            while a >= 2 and data[a - 1] == 0 and 0x20 <= data[a - 2] < 0x7F and s - a < 1600:
                a -= 2
            b = s
            while b + 1 < n and data[b + 1] == 0 and 0x20 <= data[b] < 0x7F and b - s < 1600:
                b += 2
            units = (b - a) // 2
            if units >= 12:
                return True
            if a == s and b == e:
                # whole string: MFC length prefix, or NUL code unit on both sides
                if a >= 1 and data[a - 1] == units and (a < 4 or data[a - 4:a - 1] == b"\xff\xfe\xff"):
                    return True
                if b + 1 < n and data[b] == 0 and data[b + 1] == 0 and a >= 2 and data[a - 1] == 0 and data[a - 2] == 0:
                    return True
            return False
        a = s
        while a > 0 and 0x20 <= data[a - 1] < 0x7F and s - a < 800:
            a -= 1
        b = s
        while b < n and 0x20 <= data[b] < 0x7F and b - s < 800:
            b += 1
        run = b - a
        if run >= 12:
            return True
        if a == s and b == e:
            terminated = b < n and data[b] == 0
            prefixed = a >= 1 and data[a - 1] in (run, run + 1)
            if (terminated and (a == 0 or data[a - 1] < 0x20 or prefixed)) or (prefixed and run >= 5):
                return True
        return False
    ok.span = True
    return ok


def plan_strings(eng, data):
    """Plan for an unparsed binary stream: user-name path components, the
    USER / SITE fields of embedded Parasolid headers, and dictionary matches
    inside text runs. Always same length."""
    n = len(data)
    ids = [(r.start, r.end) for r in R.scan_userpaths(data, n)]
    ids += [(r.start, r.end) for r in R.scan_userpaths_utf16(data, n)]
    i = data.find(b"**ABCDEFG")
    while i >= 0:
        end = data.find(b"**END_OF_HEADER", i, min(n, i + 8192))
        if end > 0:
            for m in _XT_ID.finditer(data[i:end]):
                ids.append((i + m.start(1), i + m.end(1)))
            for m in re.finditer(rb"\bUSER=([^;\r\n]{1,80})\r?\n", data[i:end]):
                ids.append((i + m.start(1), i + m.end(1)))
        i = data.find(b"**ABCDEFG", i + 9)
    ids = sorted(set(ids))
    flat = []
    for a, b in ids:
        if flat and a < flat[-1][1]:
            continue
        flat.append((a, b))
    return eng.plan_bytes(data, flat, string_run_ok(data, n), fixed=True, wide=True)


_ZLB_GUID = bytes.fromhex("231dd571da8148a2a85898b21b89ef99")


def rewrite_zlb(eng, data, name):
    """Pre-2015 SolidWorks "__ZLB" stream: GUID, u32 uncompressed size, u32
    compressed size, zlib data. The payload is edited like any other stream
    and re-deflated to exactly the original compressed size, so the stream
    (and the OLE file around it) keeps its size. Returns (new_bytes, count)
    or None."""
    import struct
    if len(data) < 30 or data[:16] != _ZLB_GUID:
        return None
    usize, csize = struct.unpack_from("<II", data, 16)
    if csize < 8 or 24 + csize > len(data):
        return None
    comp = data[24:24 + csize]
    try:
        inner = zlib.decompress(comp)
    except zlib.error:
        return None
    if len(inner) != usize:
        return None
    head = inner[:64].lstrip(b"\xef\xbb\xbf \r\n\t")
    if head.startswith(b"<"):
        plan = plan_text(eng, inner, len(inner), "xml", name, fixed=True)
    else:
        plan = plan_mfc(eng, inner)
    if not plan:
        return None
    nd = bytearray(inner)
    E.apply_plan(nd, plan)
    if len(nd) != len(inner):
        return None
    nd = bytes(nd)
    raw_old = comp[2:-4]
    prefer = fmt_sw.guess_params(inner, raw_old)
    z = fmt_sw.deflate_exact(nd, csize - 6, prefer)
    if z is None:
        return None
    new_comp = comp[:2] + z + struct.pack(">I", zlib.adler32(nd) & 0xFFFFFFFF)
    if zlib.decompress(new_comp) != nd:
        return None
    return data[:24] + new_comp + data[24 + csize:], len(plan)


_OLE_MFC_STREAMS = ("header2", "biography", "history", "definition", "cmgr", "cmgrhdr2", "swdoccontentmgrinfo",
                    "visualstates", "modelheader", "docvariabletable", "displaylists", "sheetnames")


def util_ext(name):
    from . import util
    return util.split_ext(name)


def h_ole(eng, src, dst, name):
    import olefile
    plans = []
    is_drawing = util_ext(name) in ("slddrw", "drwdot")
    ole = olefile.OleFileIO(src)
    try:
        for entry in ole.listdir(streams=True, storages=False):
            try:
                st = ole.openstream(entry)
                size = ole.get_size(entry)
            except Exception:
                continue
            leaf = entry[-1]
            lowleaf = leaf.lower()
            if size == 0 or size > (256 << 20):
                continue
            head = st.read(4)
            is_prop = leaf.startswith("\x05") or head == b"\xfe\xff\x00\x00"
            is_zlb = lowleaf.endswith("__zlb") and head == _ZLB_GUID[:4]
            is_mfc = (not is_prop) and any(lowleaf == k or lowleaf.endswith(k) for k in _OLE_MFC_STREAMS)
            is_xml = (not is_prop) and head[:1] == b"<"
            is_other = False
            if is_drawing and OPTS.get("previews") and (lowleaf in ("preview", "previewpng") or (
                    len(entry) > 1 and entry[0] == "Images")):
                st.seek(0)
                pdata = st.read()
                from . import fmt_preview
                new = fmt_preview.coarsen(pdata) if len(pdata) == size else None
                if new is not None and new != pdata:
                    plans.append((entry, new, 1))
                continue
            if not (is_prop or is_mfc or is_xml or is_zlb):
                # any other stream: only with --strings, never previews / images
                if not OPTS.get("strings") or size > (64 << 20):
                    continue
                if lowleaf.startswith(("preview", "thumbnail")) or head[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0"):
                    continue
                is_other = True
            st.seek(0)
            data = st.read()
            if len(data) != size:
                continue
            if is_zlb:
                res = rewrite_zlb(eng, data, leaf) if OPTS.get("zlb") else None
                if res and len(res[0]) == len(data):
                    plans.append((entry, res[0], res[1]))
                continue
            if is_prop:
                plan = plan_propset(eng, data)
            elif is_xml:
                plan = plan_text(eng, data, len(data), "xml", leaf, fixed=True)
            elif is_other or OPTS.get("strings"):
                plan = plan_strings(eng, data)
            else:
                plan = plan_mfc(eng, data)
            if plan:
                nd = bytearray(data)
                E.apply_plan(nd, plan)
                if len(nd) != len(data):
                    raise Unsafe("ole stream length changed")
                plans.append((entry, bytes(nd), len(plan)))
    finally:
        ole.close()
    if not plans:
        return False, {}
    shutil.copyfile(src, dst)
    ole = olefile.OleFileIO(dst, write_mode=True)
    try:
        for entry, nd, _c in plans:
            ole.write_stream(entry, nd)
    finally:
        ole.close()
    # validation: every stream identical to the original except the planned ones
    want = {tuple(e): d for e, d, _c in plans}
    a = olefile.OleFileIO(src)
    b = olefile.OleFileIO(dst)
    try:
        la, lb = a.listdir(streams=True, storages=True), b.listdir(streams=True, storages=True)
        if la != lb:
            raise Unsafe("ole directory changed")
        for entry in a.listdir(streams=True, storages=False):
            try:
                da = a.openstream(entry).read()
            except Exception:
                continue
            db = b.openstream(entry).read()
            exp = want.get(tuple(entry), da)
            if db != exp:
                raise Unsafe("ole stream mismatch: %r" % (entry,))
    finally:
        a.close()
        b.close()
    if os.path.getsize(dst) != os.path.getsize(src):
        raise Unsafe("ole size changed")
    return True, {"n": sum(c for _e, _d, c in plans)}


# ---------------------------------------------------------------------------
# ZIP rewriting (zip-based native formats and plain archives)
# ---------------------------------------------------------------------------
class _RawZipInfo(zipfile.ZipInfo):
    """ZipInfo that writes its file name with the exact original bytes."""
    __slots__ = ("_raw_name", "_raw_flags")

    def _encodeFilenameFlags(self):
        raw = getattr(self, "_raw_name", None)
        if raw is None:
            return super()._encodeFilenameFlags()
        return raw, (self.flag_bits & ~0x800) | self._raw_flags


def clone_zipinfo(zi, new_name=None):
    ni = _RawZipInfo(new_name or zi.filename, zi.date_time)
    ni.compress_type = zi.compress_type
    ni.comment = zi.comment
    ni.create_system = zi.create_system
    ni.create_version = zi.create_version
    ni.extract_version = zi.extract_version
    ni.external_attr = zi.external_attr
    ni.internal_attr = zi.internal_attr
    if zi.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
        ni.compress_type = zipfile.ZIP_DEFLATED
    try:
        if new_name is not None:
            # renamed member: same name encoding as the original where possible
            if zi.flag_bits & 0x800:
                ni._raw_name, ni._raw_flags = new_name.encode("utf-8"), 0x800
            else:
                try:
                    ni._raw_name, ni._raw_flags = new_name.encode("cp437"), 0
                except UnicodeEncodeError:
                    ni._raw_name, ni._raw_flags = new_name.encode("utf-8"), 0x800
        elif zi.flag_bits & 0x800:
            ni._raw_name, ni._raw_flags = zi.filename.encode("utf-8"), 0x800
        else:
            ni._raw_name, ni._raw_flags = zi.orig_filename.encode("cp437"), 0
    except UnicodeEncodeError:
        ni._raw_name = None
        ni._raw_flags = 0
    # keep extra fields other than zip64 (0x0001), which zipfile regenerates
    extra = zi.extra or b""
    out = b""
    i = 0
    while i + 4 <= len(extra):
        tag = extra[i] | (extra[i + 1] << 8)
        ln = extra[i + 2] | (extra[i + 3] << 8)
        if tag != 1:
            out += extra[i:i + 4 + ln]
        i += 4 + ln
    ni.extra = out
    return ni


def rewrite_zip(src, dst, member_fn):
    """Copy a zip, passing each member's bytes through member_fn(name, data)
    -> new_data or None. Returns number of changed members (0: dst not kept)."""
    changed = 0
    with zipfile.ZipFile(src) as zin:
        infos = zin.infolist()
        for zi in infos:
            if zi.flag_bits & 0x1:
                return 0  # encrypted archive: leave untouched
        with zipfile.ZipFile(dst, "w", allowZip64=True) as zout:
            zout.comment = zin.comment
            for zi in infos:
                ni = clone_zipinfo(zi)
                if zi.is_dir():
                    zout.writestr(ni, b"")
                    continue
                data = zin.read(zi)
                new = member_fn(zi.filename, data)
                if new is not None and new != data:
                    data = new
                    changed += 1
                zout.writestr(ni, data)
    if not changed:
        os.unlink(dst)
        return 0
    # validation: same member list, CRC-clean
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst) as b:
        if [i.filename for i in a.infolist()] != [i.filename for i in b.infolist()]:
            raise Unsafe("zip member list changed")
        bad = b.testzip()
        if bad is not None:
            raise Unsafe("zip crc failure: %s" % bad)
    return changed


_ZIPN_TEXT = (".xml", ".model", ".rels", ".json", ".txt", ".html", ".svg", ".config", ".vcxml")


def _zipn_member_plan(eng, mname, data):
    low = mname.lower()
    if len(data) > (512 << 20) or not data:
        return None
    if not (low.endswith(_ZIPN_TEXT) or low in ("document.xml", "guidocument.xml")):
        return None
    if b"\x00" in data[:4096]:
        return None
    plan = plan_text(eng, data, len(data), "xml", mname)
    if not plan:
        return None
    buf = bytearray(data)
    E.apply_plan(buf, plan)
    return bytes(buf), len(plan)


def h_zipn(eng, src, dst, name):
    """Zip-based native formats (FreeCAD, 3MF, Fusion 360 ...). Members that
    are not edited are copied bit for bit, compressed bytes included."""
    from . import rawzip
    total = 0
    try:
        with open(src, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as buf:
            ents, _c = rawzip.entries(buf)
            changes = {}
            for idx, e in enumerate(ents):
                if e["name"].endswith(b"/") or e["method"] not in (0, 8) or e["usize"] > (512 << 20):
                    continue
                mname = rawzip.decode_name(e)
                low = mname.lower()
                if not (low.endswith(_ZIPN_TEXT) or low in ("document.xml", "guidocument.xml")):
                    continue
                try:
                    data = rawzip.read_member(buf, e)
                except rawzip.Unsupported:
                    continue
                res = _zipn_member_plan(eng, mname, data)
                if res:
                    changes[idx] = res[0]
                    total += res[1]
            if not changes:
                return False, {}
            with open(dst, "wb") as out:
                rawzip.rewrite(buf, out, changes)
            with open(dst, "rb") as fh2, mmap.mmap(fh2.fileno(), 0, access=mmap.ACCESS_READ) as nb:
                try:
                    rawzip.verify(buf, nb, changes)
                except ValueError as ex:
                    raise Unsafe("zip verification: %s" % ex)
        return True, {"n": total}
    except rawzip.Unsupported:
        pass
    # ZIP64 and other unusual containers: generic rewriter
    counts = [0]

    def fn(mname, data):
        res = _zipn_member_plan(eng, mname, data)
        if not res:
            return None
        counts[0] += res[1]
        return res[0]

    ch = rewrite_zip(src, dst, fn)
    if not ch:
        return False, {}
    return True, {"n": counts[0]}


# ---------------------------------------------------------------------------
# SolidWorks lock files (~$name.sldprt): the whole content is a user name
# ---------------------------------------------------------------------------
_LISTS = None


def h_swlock(eng, src, dst, name):
    global _LISTS
    from . import pseudo
    if _LISTS is None:
        from .build_dict import Lists
        _LISTS = Lists()
    with open(src, "rb") as fh:
        raw = fh.read()
    body = raw.rstrip(b"\x00")
    if not body or len(raw) > 256:
        return False, {}
    wide = len(body) >= 4 and body[1::2].count(0) >= len(body) // 2 - 1 and body[1] == 0
    try:
        text = (body + (b"\x00" if wide and len(body) % 2 else b"")).decode("utf-16-le") if wide else body.decode("latin-1")
    except UnicodeDecodeError:
        return False, {}
    if any(ord(c) < 32 for c in text):
        return False, {}
    toks = pseudo.tokens_of(text)
    if not toks or _LISTS.is_generic(toks):
        return False, {}
    ps = "".join(t if (t in _LISTS.struct or t.isdigit() and len(t) < 4) else eng.pz.token(t) for t in toks)
    new = pseudo.project(text, ps)
    nb = new.encode("utf-16-le" if wide else "latin-1")
    out = nb[:len(body)] + raw[len(body):]
    if len(out) != len(raw) or out == raw:
        return False, {}
    with open(dst, "wb") as fh:
        fh.write(out)
    return True, {"n": 1}


# ---------------------------------------------------------------------------
# images: metadata only, pixels untouched
# ---------------------------------------------------------------------------
def h_img(eng, src, dst, name):
    from . import fmt_img
    size = os.path.getsize(src)
    if size > (256 << 20) or size < 16:
        return False, {}
    with open(src, "rb") as fh:
        raw = fh.read()
    buf = bytearray(raw)
    n = 0
    info = {}
    if raw[:2] == b"\xff\xd8":
        spans, xml, gps = fmt_img.jpeg_plan_regions(raw)
        for a, b, label, ctx, enc in spans:
            seg = raw[a:b]
            ids = [(0, len(seg))] if ctx == "id" else (
                [(r.start, r.end) for r in R.scan_userpaths(seg, len(seg))] if enc == "latin-1" else [])
            for s, e, nb in eng.plan_bytes(seg, ids, True, fixed=True):
                buf[a + s:a + e] = nb
                n += 1
        for a, b in xml:
            seg = raw[a:b]
            for s, e, nb in plan_text(eng, seg, len(seg), "xml", fixed=True):
                buf[a + s:a + e] = nb
                n += 1
        if gps:
            for a, b in gps:
                if any(buf[a:b]):
                    buf[a:b] = b"\x00" * (b - a)
                    info["gps_removed"] = 1
            n += info.get("gps_removed", 0)
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        for a, b, crc_off, typ in fmt_img.png_text_chunks(raw):
            seg = raw[a:b]
            key = seg.split(b"\x00", 1)[0].lower()
            ids = [(0, len(seg))] if key in (b"author", b"artist", b"copyright", b"creator") else \
                [(r.start, r.end) for r in R.scan_userpaths(seg, len(seg))]
            sub = eng.plan_bytes(seg, ids, True, fixed=True, wide=False)
            sub = [(s, e, nb) for s, e, nb in sub if s > len(key)]
            if sub:
                for s, e, nb in sub:
                    buf[a + s:a + e] = nb
                    n += 1
                fmt_img.png_fix_crc(buf, a, b, crc_off)
    if not n or len(buf) != len(raw):
        return False, {}
    # validation: the image still decodes to exactly the same pixels
    try:
        import io
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(io.BytesIO(raw)) as ia, Image.open(io.BytesIO(bytes(buf))) as ib:
            if ia.size != ib.size or ia.mode != ib.mode:
                raise Unsafe("image geometry changed")
            if ia.size[0] * ia.size[1] <= 40_000_000 and ia.tobytes() != ib.tobytes():
                raise Unsafe("image pixels changed")
    except Unsafe:
        raise
    except Exception as ex:
        raise Unsafe("image no longer decodes: %s" % type(ex).__name__)
    with open(dst, "wb") as fh:
        fh.write(buf)
    info["n"] = n
    return True, info


# ---------------------------------------------------------------------------
# Creo / Pro-ENGINEER files and Parasolid binary transmit headers
# ---------------------------------------------------------------------------
def cstring_ok(buf, n):
    """Predicate: position lies inside a printable run that is terminated by a
    NUL byte and starts after a non-printable byte (a C string value)."""
    def ok(pos):
        a = pos
        lim = max(0, pos - 512)
        while a > lim and 0x20 <= buf[a - 1] < 0x7F:
            a -= 1
        if a == lim and a > 0:
            return False
        b = pos
        lim = min(n, pos + 512)
        while b < lim and 0x20 <= buf[b] < 0x7F:
            b += 1
        if b >= n or buf[b] != 0:
            return False
        return (b - a) >= 3
    return ok


def h_creo(eng, src, dst, name):
    """Creo models keep attribute records as NUL-terminated ASCII strings
    ("user_name\\0VALUE\\0"). Only such string values are edited, always with
    a replacement of the same length, so the table of contents, section
    offsets and all binary data stay exactly where they were."""
    size = os.path.getsize(src)
    with open(src, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as buf:
        if buf[:5] != b"#UGC:":
            return False, {}
        ids = []
        i = buf.find(b"user_name\x00")
        while i >= 0:
            a = i + 10
            b = buf.find(b"\x00", a, min(size, a + 200))
            if b > a and b"modif_time" in bytes(buf[b:b + 16]):
                ids.append((a, b))          # revision record: user_name, modif_time
            i = buf.find(b"user_name\x00", i + 10)
        hdr_end = buf.find(b"#-END_OF_UGC_HEADER", 0, 4096)
        if hdr_end > 0:
            h = buf.find(b"#- HOST", 0, hdr_end)
            if h >= 0:
                e = buf.find(b"\n", h, hdr_end)
                if e > h + 8:
                    ids.append((h + 8, e))
        for r in R.scan_userpaths(buf, size):
            ids.append((r.start, r.end))
        ids = sorted(set(ids))
        flat = []
        for a, b in ids:
            if flat and a < flat[-1][1]:
                continue
            flat.append((a, b))
        plan = eng.plan_bytes(buf, flat, cstring_ok(buf, size), fixed=True, wide=False)
        sections = [m for m in (buf.find(b"#END_OF_UGC"), buf.find(b"#UGC_TOC"))]
    if not plan:
        return False, {}
    patch_file(src, dst, plan)
    with open(dst, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as nb:
        if len(nb) != size or [nb.find(b"#END_OF_UGC"), nb.find(b"#UGC_TOC")] != sections:
            raise Unsafe("creo structure changed")
    return True, {"n": len(plan)}


def h_xb(eng, src, dst, name):
    """Parasolid binary transmit file: only the leading text header."""
    size = os.path.getsize(src)
    with open(src, "rb") as fh:
        head = fh.read(8192)
    end = head.find(b"**END_OF_HEADER")
    if not head.startswith(b"**ABCDEFG") or end < 0:
        return False, {}
    ids = [m.span(1) for m in _XT_ID.finditer(head[:end])]
    ids += [(r.start, r.end) for r in R.scan_userpaths(head, end)]
    ids = sorted(set(ids))
    flat = []
    for a, b in ids:
        if flat and a < flat[-1][1]:
            continue
        flat.append((a, b))
    plan = eng.plan_bytes(head, flat, True, 0, end, fixed=True, wide=False)
    if not plan:
        return False, {}
    patch_file(src, dst, plan)
    if os.path.getsize(dst) != size:
        raise Unsafe("size changed")
    return True, {"n": len(plan)}


# ---------------------------------------------------------------------------
# DWG R2004 and later
# ---------------------------------------------------------------------------
_DWG_PLAIN_SECTIONS = frozenset(("AcDb:SummaryInfo", "AcDb:AppInfo", "AcDb:AppInfoHistory",
                                 "AcDb:FileDepList", "AcDb:RevHistory"))


def h_dwg(eng, src, dst, name):
    """Text of the plain string sections of a DWG (summary information,
    application info and its history, file dependency list). Each page is
    edited in its decompressed form through its literal bytes, or directly
    when the section is stored uncompressed; page checksums are rewritten.
    Pages the parser cannot prove it understands are left untouched."""
    from . import fmt_dwg
    size = os.path.getsize(src)
    if size > (512 << 20):
        return False, {}
    with open(src, "rb") as fh:
        raw = fh.read()
    if raw[:6] not in fmt_dwg.VERSIONS:
        return False, {}
    pages = fmt_dwg.pages(raw)
    if not pages:
        return False, {}
    # Only sections that are plain records of strings are edited. The object
    # and header sections are bit streams protected by their own CRC-16 per
    # object / per section; a byte edit there would invalidate that CRC.
    names = fmt_dwg.section_names(raw)
    if not names:
        return False, {"note": "dwg-section-map-unreadable"}
    buf = bytearray(raw)
    n = 0
    refused = 0
    for pg in pages:
        if names.get(pg["hdr"][1]) not in _DWG_PLAIN_SECTIONS:
            continue
        plain, mode = fmt_dwg.read_page(raw, pg)
        if plain is None:
            continue
        plan = plan_strings(eng, plain)
        if not plan:
            continue
        nd = bytearray(plain)
        E.apply_plan(nd, plan)
        if fmt_dwg.rewrite_page(buf, pg, bytes(nd), mode):
            n += len(plan)
        else:
            refused += 1
    if not n:
        return False, ({"dwg_refused": refused} if refused else {})
    # validation: identical page layout, every checksum valid, same size
    after = fmt_dwg.pages(bytes(buf))
    before = fmt_dwg.pages(raw)
    if len(buf) != len(raw) or [(p["off"], p["csize"]) for p in before] != [(p["off"], p["csize"]) for p in after]:
        raise Unsafe("dwg page layout changed")
    with open(dst, "wb") as fh:
        fh.write(buf)
    info = {"n": n}
    if refused:
        info["dwg_refused"] = refused
    return True, info
