"""Pass 1 - harvest identity candidates from structured fields.

Read-only. For every corpus file (and every archive member, recursively) the
identity-bearing fields we understand are read and emitted as (label, value)
records. The aggregate drives the dictionary build.

Usage:  python -I harvest.py [--prefix P] [--limit N] [--jobs N]
"""
import collections
import json
import mmap
import multiprocessing as mp
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deid import util  # noqa: E402
from deid import regions as R  # noqa: E402
from deid import fmt_step, fmt_iges, fmt_sw, oleprops, archives  # noqa: E402

MAX_DEPTH = 4
_TMPROOT = os.path.join(util.WORK, "tmp")


def _p21_decode(s):
    s = s.replace("''", "'").replace("\\\\", "\\")
    if "\\X" in s or "\\S\\" in s:
        s = re.sub(r"\\X2\\((?:[0-9A-Fa-f]{4})+)\\X0\\",
                   lambda m: "".join(chr(int(m.group(1)[i:i + 4], 16)) for i in range(0, len(m.group(1)), 4)), s)
        s = re.sub(r"\\X\\([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), s)
        s = re.sub(r"\\S\\(.)", lambda m: chr(ord(m.group(1)) + 128), s)
    return s


def _emit_regions(buf, regs, out, decode=None):
    for r in regs:
        v = r.text(buf)
        if decode:
            v = decode(v)
        v = v.strip()
        if v:
            out.append((r.label, v[:300]))


def _anchors(buf, n, out, text=True, utf16=False):
    _emit_regions(buf, R.scan_userpaths(buf, n), out)
    if utf16:
        _emit_regions(buf, R.scan_userpaths_utf16(buf, n), out)
    _emit_regions(buf, R.scan_urls(buf, n, limit=3000), out)
    if text:
        ems = R.scan_emails(buf, n, limit=5000)
        # pair local/domain into full addresses too
        for i in range(0, len(ems) - 1, 2):
            a, b = ems[i], ems[i + 1]
            out.append(("email", bytes(buf[a.start:b.end]).decode("latin-1")))
        _emit_regions(buf, ems, out)


_XT_KEYS = re.compile(rb"\b(USER|SITE|FILE|MC_ID|KEY|DATE)=([^;\r\n]{1,200});")
_PS_KEYS = re.compile(rb"^%%(For|Title|Creator|Author|Copyright|Routing)\s*:\s*(.{1,200}?)\r?$", re.M)
_DXF_LSB = re.compile(rb"\$LASTSAVEDBY\s*\r?\n\s*1\s*\r?\n([^\r\n]{1,200})")
_SVG_KEYS = re.compile(
    rb"<(dc:creator|dc:publisher|dc:rights|dc:contributor|dc:title|cc:Agent)\b[^>]*>(.{0,400}?)</\1>", re.S)
_SVG_ATTR = re.compile(rb"\b(sodipodi:docname|inkscape:export-filename|sodipodi:absref|xlink:href)=\"([^\"]{1,300})\"")
_TAGS = re.compile(rb"<[^>]+>")
_FC_PROP = re.compile(
    rb"<Property name=\"(CreatedBy|LastModifiedBy|Company|Comment|FileName|Label|License|LicenseURL|Id|"
    rb"TransientDir|Uid)\"[^>]*>\s*<\w+ value=\"([^\"]{0,400})\"")
_3MF_META = re.compile(rb"<metadata\s+name=\"([^\"]{1,60})\"[^>]*>([^<]{0,400})</metadata>")
_XMP_KEYS = re.compile(
    rb"<(dc:creator|dc:title|dc:description|dc:rights|dc:publisher|pdf:Keywords|pdf:Author|xmp:Author|"
    rb"photoshop:AuthorsPosition|pdfx:Company|pdfx:Manager|pdfx:SourceModified|xmpRights:Owner|"
    rb"photoshop:Credit|Iptc4xmpCore:CreatorContactInfo)\b[^>]*>(.{0,800}?)</\1>", re.S)
_CREO_HDR = re.compile(rb"^#- (HOST|LINK|DBID|UOBJ_ID|MACH)\s+(.{0,80}?)\s*\\?\r?$", re.M)


def _xml_text(b):
    return util.squash(util.decode_best(_TAGS.sub(b" ", b)))


def h_step(buf, n, out):
    regs, _ = fmt_step.step_regions(buf, n)
    _emit_regions(buf, regs, out, _p21_decode)
    _anchors(buf, n, out)


def h_iges(buf, n, out):
    _emit_regions(buf, fmt_iges.iges_regions(buf, n), out)
    _anchors(buf, min(n, 1 << 20), out)


def h_text(buf, n, out, name=""):
    head = bytes(buf[:8192])
    for m in _XT_KEYS.finditer(head):
        out.append(("xt." + m.group(1).decode().lower(), util.decode_best(m.group(2)).strip()))
    for m in _PS_KEYS.finditer(head):
        out.append(("ps." + m.group(1).decode().lower(), util.decode_best(m.group(2)).strip()))
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        txt = util.decode_best(bytes(buf[:min(n, 4 << 20)])).encode("utf-8")
        _anchors(txt, len(txt), out)
    else:
        _anchors(buf, n, out)


def h_dxf(buf, n, out):
    for m in _DXF_LSB.finditer(bytes(buf[:min(n, 1 << 20)])):
        out.append(("dxf.lastsavedby", util.decode_best(m.group(1)).strip()))
    _anchors(buf, n, out)


def h_svg(buf, n, out):
    head = bytes(buf[:min(n, 2 << 20)])
    for m in _SVG_KEYS.finditer(head):
        v = _xml_text(m.group(2))
        if v:
            out.append(("svg." + m.group(1).decode().replace(":", "_"), v[:300]))
    for m in _SVG_ATTR.finditer(head):
        v = util.decode_best(m.group(2))
        if not v.startswith(("data:", "#")):
            out.append(("svg." + m.group(1).decode().replace(":", "_"), v))
    _anchors(buf, n, out)


def h_stl(buf, n, out):
    head = bytes(buf[:80])
    if head[:5].lower() == b"solid":
        line = bytes(buf[:300]).split(b"\n", 1)[0]
        out.append(("stl.solid", util.decode_best(line[5:]).strip()))
    else:
        txt = head.rstrip(b"\x00 ").decode("latin-1")
        if txt and sum(1 for c in txt if 32 <= ord(c) < 127) >= len(txt) * 0.9:
            out.append(("stl.header", txt.strip()))


def h_creo(buf, n, out):
    head = bytes(buf[:4096])
    for m in _CREO_HDR.finditer(head):
        v = m.group(2).decode("latin-1").strip()
        if v:
            out.append(("creo." + m.group(1).decode().lower(), v))
    # revision records: "user_name\0<VALUE>\0"
    seen = set()
    i = buf.find(b"user_name\x00")
    while i >= 0 and len(seen) < 50:
        a = i + 10
        b = buf.find(b"\x00", a, min(n, a + 120))
        if b > a and b"modif_time" in bytes(buf[b:b + 16]):
            v = bytes(buf[a:b])
            if v not in seen and all(0x20 <= c < 0x7F for c in v):
                seen.add(v)
                out.append(("creo.user", v.decode("latin-1").strip()))
        i = buf.find(b"user_name\x00", i + 10)
    # revision comments: "Hostname: '<workstation>'"
    hosts = set()
    i = buf.find(b"Hostname: '")
    while i >= 0 and len(hosts) < 50:
        a = i + 11
        b = buf.find(b"'", a, min(n, a + 64))
        if b > a:
            v = bytes(buf[a:b])
            if v not in hosts and all(0x21 <= c < 0x7F for c in v):
                hosts.add(v)
                out.append(("creo.hostname", v.decode("latin-1")))
        i = buf.find(b"Hostname: '", i + 11)
    _anchors(buf, n, out, text=False)


def h_bin(buf, n, out):
    _anchors(buf, n, out, text=False, utf16=True)


_SW_CORE = re.compile(
    rb"<((?:dc|cp|dcterms):(?:creator|lastModifiedBy|title|subject|description|keywords|category))\b[^>]*>"
    rb"([^<]{1,400})</\1>")
_SW_APP = re.compile(rb"<(Company|Manager|Template)>([^<]{1,300})</\1>")
_SW_PROP = re.compile(
    rb"<property\b[^>]*\bname=\"([^\"]{1,80})\"[^>]*>\s*<vt:(?:lpstr|lpwstr)>([^<]{1,400})</vt:")


def h_sw(buf, n, out):
    streams = fmt_sw.parse(buf)
    if streams is None:
        out.append(("_sw.unparsed", "1"))
        _anchors(buf, n, out, text=False, utf16=True)
        return
    out.append(("_sw.streams", str(len(streams))))
    for s in streams:
        if s.usize > (64 << 20) or s.name in ("Preview", "PreviewPNG"):
            continue
        try:
            data = fmt_sw.read_stream(buf, s)
        except Exception:
            out.append(("_error", "sw-stream"))
            continue
        low = s.name.lower()
        if low.endswith("core.xml"):
            for m in _SW_CORE.finditer(data):
                out.append(("sw.core." + m.group(1).decode().split(":")[1].lower(), util.decode_best(m.group(2)).strip()))
        elif low.endswith("app.xml"):
            for m in _SW_APP.finditer(data):
                out.append(("sw.app." + m.group(1).decode().lower(), util.decode_best(m.group(2)).strip()))
        elif low.startswith("docprops/"):
            for m in _SW_PROP.finditer(data):
                out.append(("sw.prop." + util.decode_best(m.group(1)), util.decode_best(m.group(2)).strip()[:300]))
        _anchors(data, len(data), out, text=False, utf16=True)


def h_ole(path, buf, n, out):
    import olefile
    try:
        ole = olefile.OleFileIO(path)
    except Exception as e:  # corrupt compound file
        out.append(("_error", "ole:" + type(e).__name__))
        _anchors(buf, n, out, text=False, utf16=True)
        return
    try:
        for entry in ole.listdir(streams=True, storages=False):
            try:
                st = ole.openstream(entry)
                if not entry[-1].startswith("\x05"):
                    if st.read(4) != b"\xfe\xff\x00\x00":
                        continue
                    st.seek(0)
                data = st.read()
            except Exception:
                continue
            for ps in oleprops.parse(data):
                raw = data[ps.off:ps.off + ps.nbytes]
                v = raw.decode(ps.enc, "replace").strip()
                if v:
                    out.append((ps.label(), v[:300]))
    finally:
        ole.close()
    _anchors(buf, n, out, text=False, utf16=True)


def h_pdf(path, out):
    import pikepdf
    try:
        pdf = pikepdf.open(path)
    except Exception as e:
        out.append(("_error", "pdf:" + type(e).__name__))
        return
    try:
        try:
            info = pdf.trailer.get("/Info")
            if info is not None:
                for k in list(info.keys()):
                    try:
                        v = info[k]
                        if isinstance(v, pikepdf.String):
                            s = str(v).strip()
                            if s:
                                out.append(("pdf.info." + str(k).lstrip("/").lower(), s[:300]))
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            md = pdf.Root.get("/Metadata")
            if md is not None:
                xmp = md.read_bytes()
                for m in _XMP_KEYS.finditer(xmp):
                    v = _xml_text(m.group(2))
                    if v:
                        out.append(("pdf.xmp." + m.group(1).decode().replace(":", "_"), v[:300]))
                _anchors(xmp, len(xmp), out)
        except Exception:
            pass
        try:
            out.append(("_pdf.pages", str(len(pdf.pages))))
            if "/Names" in pdf.Root and "/EmbeddedFiles" in pdf.Root.Names:
                out.append(("_pdf.attachments", "1"))
        except Exception:
            pass
    finally:
        pdf.close()


def h_img(path, out):
    from PIL import Image, ExifTags
    try:
        with Image.open(path) as im:
            for k, v in (im.info or {}).items():
                if isinstance(v, str) and k.lower() in ("author", "title", "description", "copyright",
                                                        "comment", "source", "software", "artist"):
                    if v.strip():
                        out.append(("img.info." + k.lower(), v.strip()[:300]))
            try:
                ex = im.getexif()
            except Exception:
                ex = None
            if ex:
                for tag in (315, 33432, 270, 305, 316, 40093, 40091, 40092, 40095):
                    v = ex.get(tag)
                    if isinstance(v, bytes):
                        v = v.decode("utf-16-le", "replace") if tag >= 40091 else v.decode("latin-1", "replace")
                    if isinstance(v, str) and v.strip("\x00 "):
                        out.append(("img.exif." + ExifTags.TAGS.get(tag, str(tag)).lower(), v.strip("\x00 ")[:300]))
                try:
                    if ex.get_ifd(0x8825):
                        out.append(("img.exif.gps", "present"))
                except Exception:
                    pass
            if "xmp" in im.info or "XML:com.adobe.xmp" in im.info:
                x = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")
                if isinstance(x, str):
                    x = x.encode("utf-8", "replace")
                for m in _XMP_KEYS.finditer(x):
                    v = _xml_text(m.group(2))
                    if v:
                        out.append(("img.xmp." + m.group(1).decode().replace(":", "_"), v[:300]))
    except Exception as e:
        out.append(("_error", "img:" + type(e).__name__))


def h_zipn(path, name, out):
    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:
        out.append(("_error", "zip:" + type(e).__name__))
        return
    with zf:
        for zi in zf.infolist():
            if zi.is_dir() or zi.file_size > (32 << 20) or zi.flag_bits & 1:
                continue
            low = zi.filename.lower()
            try:
                if low in ("document.xml", "guidocument.xml"):
                    data = zf.read(zi)
                    for m in _FC_PROP.finditer(data[:1 << 17]):
                        v = util.decode_best(m.group(2)).strip()
                        if v:
                            out.append(("fcstd." + m.group(1).decode().lower(), v[:300]))
                    _anchors(data, min(len(data), 4 << 20), out)
                elif low.endswith(".model"):
                    with zf.open(zi) as fh:
                        data = fh.read(1 << 18)
                    for m in _3MF_META.finditer(data):
                        v = util.decode_best(m.group(2)).strip()
                        if v:
                            out.append(("3mf." + m.group(1).decode("latin-1").lower(), v[:300]))
                elif low.endswith((".xml", ".json", ".rels", ".dat", ".txt", ".config", ".html")) and zi.file_size < (4 << 20):
                    data = zf.read(zi)
                    _anchors(data, len(data), out, text=util.looks_text(data[:2048]), utf16=True)
            except Exception:
                out.append(("_error", "zipmember"))


def harvest_one(logical, path, depth=0):
    """Return list of result dicts (one per file, including archive members)."""
    res = []
    out = []
    kind = "?"
    size = 0
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(4096)
            kind = util.kind_of(logical.rsplit("!/", 1)[-1], head)
            if size == 0:
                return [{"p": logical, "k": "empty", "s": 0, "r": []}]
            if kind == "pdf":
                h_pdf(path, out)
            elif kind == "img":
                h_img(path, out)
            elif kind == "zipn":
                h_zipn(path, logical, out)
            elif kind in archives.ARCHIVE_KINDS:
                if depth < MAX_DEPTH:
                    tmp = tempfile.mkdtemp(prefix="hv", dir=_TMPROOT)
                    try:
                        try:
                            members = archives.extract(path, kind, logical.rsplit("!/", 1)[-1], tmp)
                        except Exception as e:
                            out.append(("_error", "archive:" + type(e).__name__))
                            members = []
                        out.append(("_archive.members", str(len(members))))
                        for mname, mpath in members:
                            res.extend(harvest_one(logical + "!/" + mname, mpath, depth + 1))
                    finally:
                        shutil.rmtree(tmp, ignore_errors=True)
            else:
                with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as buf:
                    n = size
                    if kind == "step":
                        h_step(buf, n, out)
                    elif kind == "iges":
                        h_iges(buf, n, out)
                    elif kind == "dxf":
                        h_dxf(buf, n, out)
                    elif kind == "svg":
                        h_svg(buf, n, out)
                    elif kind == "text":
                        h_text(buf, n, out, logical)
                    elif kind == "stl":
                        h_stl(buf, n, out)
                    elif kind == "creo":
                        h_creo(buf, n, out)
                    elif kind == "ole":
                        h_ole(path, buf, n, out)
                    elif kind == "sw":
                        h_sw(buf, n, out)
                    else:
                        h_bin(buf, n, out)
    except Exception as e:
        out.append(("_error", "%s:%s" % (type(e).__name__, str(e)[:80])))
    res.append({"p": logical, "k": kind, "s": size, "r": out})
    return res


def _work(item):
    rel, _size = item
    return harvest_one(rel, os.path.join(util.INPUT, rel))


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--out", default=os.path.join(util.WORK, "harvest"))
    ap.add_argument("--only-ext", default=None,
                    help="comma list of extensions: harvest only these files and write values_<tag>.jsonl")
    ap.add_argument("--tag", default="extra")
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(_TMPROOT, exist_ok=True)
    files = [f for f in util.walk_input() if f[0].startswith(args.prefix)]
    if args.only_ext:
        exts = set(args.only_ext.lower().split(","))
        files = [f for f in files if util.split_ext(f[0]) in exts]
    if args.limit:
        step = max(1, len(files) // args.limit)
        files = files[::step][:args.limit]
    # big files first so the pool tail is short
    files.sort(key=lambda f: -f[1])
    t0 = time.time()
    agg = {}
    kinds = collections.Counter()
    nfiles = 0
    fname = "files.jsonl" if not args.only_ext else "files_%s.jsonl" % args.tag
    vname = "values.jsonl" if not args.only_ext else "values_%s.jsonl" % args.tag
    with open(os.path.join(args.out, fname), "w") as ff, mp.Pool(args.jobs) as pool:
        for results in pool.imap_unordered(_work, files, chunksize=16):
            for r in results:
                nfiles += 1
                kinds[r["k"]] += 1
                top = r["p"].split("!/", 1)[0]
                grp = util.group_of(top)
                errs = [v for l, v in r["r"] if l == "_error"]
                ff.write(json.dumps({"p": r["p"], "k": r["k"], "s": r["s"], "n": len(r["r"]), "e": errs},
                                    ensure_ascii=False) + "\n")
                for label, value in set(r["r"]):
                    if label.startswith("_"):
                        continue
                    a = agg.get((label, value))
                    if a is None:
                        a = agg[(label, value)] = [0, set(), r["p"]]
                    a[0] += 1
                    if len(a[1]) < 400:
                        a[1].add(grp)
            if nfiles % 5000 < len(results):
                sys.stderr.write("\r%d files %.0fs" % (nfiles, time.time() - t0))
    with open(os.path.join(args.out, vname), "w") as vf:
        for (label, value), (cnt, grps, ex) in sorted(agg.items(), key=lambda kv: (kv[0][0], -kv[1][0])):
            vf.write(json.dumps({"l": label, "v": value, "c": cnt, "g": sorted(grps), "x": ex},
                                ensure_ascii=False) + "\n")
    sys.stderr.write("\nharvested %d files in %.0fs; %d distinct values\n" % (nfiles, time.time() - t0, len(agg)))
    print(json.dumps(kinds.most_common()))


if __name__ == "__main__":
    main(sys.argv[1:])
