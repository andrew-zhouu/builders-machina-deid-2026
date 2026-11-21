"""Pass 3 - transform the corpus.

Every input file produces exactly one output file at the same relative path.
Files that need no change are hard-linked (byte-identical); changed files are
written fresh and validated by their handler. Any handler failure falls back
to the original bytes and is recorded in the journal, so no file is dropped.

Usage: python -I transform.py --out DIR [--prefix P] [--limit N] [--jobs N]
"""
import gzip
import json
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import zipfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deid import util, archives  # noqa: E402
from deid import engine as E  # noqa: E402
from deid import handlers as H  # noqa: E402

MAX_DEPTH = 4
_ENGINE = None
_TMPROOT = None
OPTS = {"rename": False, "pdftext": False, "creo": False, "zlb": False, "strings": False, "dwg": False,
        "pdfextra": False, "dwgobj": False, "scope": False, "nofallback": False, "pdfmeta": False}
TEXT_KINDS = ("step", "iges", "dxf", "svg", "text")


def _init(dict_path, key_path, tmproot, opts=None):
    global _ENGINE, _TMPROOT
    if opts:
        OPTS.update(opts)
    with open(key_path, "rb") as fh:
        key = fh.read()
    # File names that carry an identity are protected inside other files only
    # when paths are kept; with --rename names and references change together.
    protect = os.path.join(os.path.dirname(dict_path), "protect.json")
    use_protect = os.path.exists(protect) and OPTS["rename"] != "all"
    _ENGINE = E.Engine(dict_path, key, protect if use_protect else None)
    _TMPROOT = tmproot
    H.OPTS.update(OPTS)
    H.NESTED = _nested
    dups = os.path.join(os.path.dirname(dict_path), "dups.json")
    _COPIES.clear()
    if OPTS.get("scope") and os.path.exists(dups):
        with open(dups, encoding="utf-8") as fh:
            _COPIES.update(json.load(fh).get("copies") or {})


_COPIES = {}          # corpus path -> paths of its exact copies (PDF files; from dups.json)


def prepare(eng, rel):
    """Per-file engine state: source group, and the groups of exact copies
    of a PDF (so that every copy of one document gets the same pseudonyms)."""
    eng.group = util.group_of(rel)
    eng.stats = {}
    copies = _COPIES.get(rel)
    eng.extra_groups = frozenset(util.group_of(c) for c in copies) if copies else frozenset()


def _nested(name, data, depth=1):
    """Process a file embedded in another file (a PDF attachment) with the
    handler of its own format. Returns the new bytes, or None when nothing
    changed or the embedded file could not be handled."""
    base = os.path.basename(name.replace("\\", "/")) or "embedded"
    d = tempfile.mkdtemp(prefix="emb", dir=_TMPROOT)
    try:
        src = os.path.join(d, "in")
        dst = os.path.join(d, "out")
        with open(src, "wb") as fh:
            fh.write(data)
        state = _ENGINE.scope_save()          # the enclosing document's scope must survive
        try:
            ch, _kind, _info = process(_ENGINE.group + "/embedded!/" + base, src, dst, depth)
        finally:
            _ENGINE.scope_restore(state)
        if not ch:
            return None
        with open(dst, "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def process(logical, src, dst, depth=0):
    """Transform src into dst. Returns (changed, kind, info). When changed is
    False nothing is left at dst."""
    eng = _ENGINE
    name = logical.rsplit("!/", 1)[-1]
    try:
        size = os.path.getsize(src)
        with open(src, "rb") as fh:
            head = fh.read(4096)
    except OSError as ex:
        return False, "?", {"error": "read:%s" % ex}
    kind = util.kind_of(name, head)
    if size == 0:
        return False, kind, {}
    # Scope: the identities named by the file's source folder (and by the
    # folders that hold copies of it) are removed from its contents too,
    # whatever their tier. The same group-level scope is used when paths are
    # renamed, so names and references stay in step. A PDF is read by people
    # and carries no machine references, so the rest of its path counts too.
    eng.scope_begin()
    if OPTS.get("scope"):
        try:
            from deid import paths as _P
            copies = _COPIES.get(logical, ()) if kind == "pdf" else ()
            ids = _P.group_scope(eng, logical, copies)
            if kind == "pdf":
                ids = set(ids) | _P.scope_ids(eng, logical)
                for c in copies:                  # every copy of a document is treated alike
                    ids |= _P.scope_ids(eng, c)
            eng.scope_promote(ids)
        except Exception:
            pass
    tmp = dst + ".tmp~"
    try:
        if kind in TEXT_KINDS:
            ch, info = H.h_textlike(eng, src, tmp, kind, name)
        elif kind == "stl":
            ch, info = H.h_stl(eng, src, tmp, name)
        elif kind == "pdf":
            ch, info = H.h_pdf(eng, src, tmp, name)
        elif kind == "ole":
            ch, info = H.h_ole(eng, src, tmp, name)
        elif kind == "sw":
            ch, info = H.h_sw(eng, src, tmp, name)
        elif kind == "swlock":
            ch, info = H.h_swlock(eng, src, tmp, name)
        elif kind == "img":
            ch, info = H.h_img(eng, src, tmp, name)
        elif kind == "dwg" and OPTS["dwg"]:
            ch, info = H.h_dwg(eng, src, tmp, name)
        elif kind == "creo" and OPTS["creo"]:
            ch, info = H.h_creo(eng, src, tmp, name)
        elif kind == "bin" and OPTS["creo"] and head.startswith(b"**ABCDEFG"):
            ch, info = H.h_xb(eng, src, tmp, name)
        elif kind == "zipn":
            ch, info = H.h_zipn(eng, src, tmp, name)
        elif kind in archives.ARCHIVE_KINDS:
            ch, info = do_archive(logical, src, tmp, kind, name, depth)
        else:
            ch, info = False, {}
    except Exception as ex:
        ch = False
        info = {"error": "%s:%s" % (type(ex).__name__, str(ex)[:160])}
        if not isinstance(ex, (H.Unsafe, OSError, ValueError, zipfile.BadZipFile, tarfile.TarError, EOFError)):
            info["tb"] = traceback.format_exc()[-600:]
    if ch:
        os.replace(tmp, dst)
    else:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return ch, kind, info


# ---------------------------------------------------------------------------
# archives
# ---------------------------------------------------------------------------
def _gz_header(path):
    """(name, mtime) stored in a gzip header."""
    with open(path, "rb") as fh:
        h = fh.read(10)
        if len(h) < 10 or h[:2] != b"\x1f\x8b":
            return None, 0
        flags = h[3]
        mtime = int.from_bytes(h[4:8], "little")
        name = None
        if flags & 4:
            xlen = int.from_bytes(fh.read(2), "little")
            fh.read(xlen)
        if flags & 8:
            raw = bytearray()
            while True:
                c = fh.read(1)
                if not c or c == b"\x00":
                    break
                raw += c
            name = raw.decode("latin-1")
        return name, mtime


def _repack_zip_raw(src, dst, repl, names):
    """Byte-preserving repack; returns False when the archive needs the
    generic path (ZIP64, unusual layout, very large replacement)."""
    import mmap
    from deid import rawzip
    if any(os.path.getsize(p) > (768 << 20) for p in repl.values()):
        return False
    try:
        with open(src, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as buf:
            ents, _c = rawzip.entries(buf)
            changes = {}
            for idx, path in repl.items():
                with open(path, "rb") as f2:
                    changes[idx] = f2.read()
            rnames = {}
            for idx, nn in (names or {}).items():
                enc = rawzip.encode_name(ents[idx], nn)
                if enc is None:
                    return False
                rnames[idx] = enc
            with open(dst, "wb") as out:
                rawzip.rewrite(buf, out, changes, rnames)
            with open(dst, "rb") as f3, mmap.mmap(f3.fileno(), 0, access=mmap.ACCESS_READ) as nb:
                try:
                    rawzip.verify(buf, nb, changes, rnames)
                except ValueError as ex:
                    raise H.Unsafe("zip verification: %s" % ex)
        return True
    except rawzip.Unsupported:
        return False


def _repack_zip(src, dst, repl, names=None):
    """repl: {infolist index: path of new member data}; names: {index: new name}."""
    names = names or {}
    if _repack_zip_raw(src, dst, repl, names):
        return
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", allowZip64=True) as zout:
        zout.comment = zin.comment
        for idx, zi in enumerate(zin.infolist()):
            ni = H.clone_zipinfo(zi, names.get(idx))
            if zi.is_dir():
                zout.writestr(ni, b"")
                continue
            if idx in repl:
                with open(repl[idx], "rb") as fh, zout.open(ni, "w", force_zip64=True) as w:
                    shutil.copyfileobj(fh, w, 1 << 20)
            else:
                with zin.open(zi) as fh, zout.open(ni, "w", force_zip64=zi.file_size >= (1 << 31)) as w:
                    shutil.copyfileobj(fh, w, 1 << 20)
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst) as b:
        la, lb = a.infolist(), b.infolist()
        if len(la) != len(lb):
            raise H.Unsafe("zip member count changed")
        for idx, (x, y) in enumerate(zip(la, lb)):
            if y.filename != names.get(idx, x.filename):
                raise H.Unsafe("zip member name mismatch")
        if b.testzip() is not None:
            raise H.Unsafe("zip crc failure")


def _repack_tar(src, dst, repl, names=None):
    names = names or {}
    with tarfile.open(src) as tin, tarfile.open(dst, "w", format=tin.format or tarfile.GNU_FORMAT) as tout:
        for idx, ti in enumerate(tin):
            data = None
            if ti.isfile():
                data = open(repl[idx], "rb") if idx in repl else tin.extractfile(ti)
                if idx in repl:
                    ti.size = os.path.getsize(repl[idx])
            if idx in names:
                ti.name = names[idx]
                ti.pax_headers = {k: v for k, v in (ti.pax_headers or {}).items() if k != "path"}
            try:
                tout.addfile(ti, data)
            finally:
                if data is not None:
                    data.close()
    with tarfile.open(src) as a, tarfile.open(dst) as b:
        if len(a.getnames()) != len(b.getnames()):
            raise H.Unsafe("tar member count changed")


def do_archive(logical, src, dst, kind, name, depth):
    if depth >= MAX_DEPTH:
        return False, {"note": "max-depth"}
    from deid import paths as P
    eng = _ENGINE
    rename = OPTS["rename"] == "all"       # member names change only with full renaming
    tmp = tempfile.mkdtemp(prefix="tx", dir=_TMPROOT)
    try:
        members = archives.extract(src, kind, name, tmp)
        repl = {}
        minfo = []
        total = 0
        for mname, mpath in members:
            out = mpath + ".out"
            ch, mk, info = process(logical + "!/" + mname, mpath, out, depth + 1)
            if info.get("error") or info.get("note") or ch:
                rec = {"m": mname, "k": mk, "c": int(ch)}
                rec.update({k: v for k, v in info.items() if k != "tb"})
                minfo.append(rec)
            if ch:
                total += info.get("n", 1)
                base = os.path.basename(mpath)
                key = int(base) if base.isdigit() else mpath
                repl[key] = out
        names = {}
        if rename:
            eng.scope_begin()
            if OPTS.get("scope"):
                eng.scope_promote(P.group_scope(eng, logical))
            if kind == "zip":
                with zipfile.ZipFile(src) as zf:
                    for idx, zi in enumerate(zf.infolist()):
                        nn = P.map_member(eng, zi.filename)
                        if nn != zi.filename:
                            names[idx] = nn
            elif kind == "tar":
                with tarfile.open(src) as tf:
                    for idx, ti in enumerate(tf):
                        nn = P.map_member(eng, ti.name)
                        if nn != ti.name:
                            names[idx] = nn
        gname = mtime = None
        if kind == "gz":
            gname, mtime = _gz_header(src)
            if rename and gname:
                ng = P.map_member(eng, gname)
                if ng != gname:
                    names[0] = ng
                    gname = ng
        if not repl and not names and not (rename and kind == "7z"):
            return False, ({"members": minfo} if minfo else {})
        if kind == "zip":
            _repack_zip(src, dst, repl, names)
        elif kind == "tar":
            _repack_tar(src, dst, repl, names)
        elif kind == "gz":
            data_path = repl.get(0) or members[0][1]
            with open(data_path, "rb") as fin, open(dst, "wb") as raw:
                with gzip.GzipFile(filename=gname or "", mode="wb", fileobj=raw, mtime=mtime) as gz:
                    shutil.copyfileobj(fin, gz, 1 << 20)
        elif kind == "Z":
            with open(repl[0], "rb") as fin, open(dst, "wb") as fout:
                subprocess.run(["compress", "-c"], stdin=fin, stdout=fout, check=True, timeout=900)
        elif kind == "7z":
            sub = os.path.join(tmp, "x")
            for mpath, out in repl.items():
                os.replace(out, mpath)
            nren = 0
            if rename:
                # rename bottom-up so parents keep their old names while we walk
                for dp, dns, fns in os.walk(sub, topdown=False):
                    for fn in fns:
                        if fn.endswith(".out") or fn.endswith(".tmp~"):
                            continue
                        nn = P.rename_file_component(eng, fn)
                        if nn != fn and not os.path.exists(os.path.join(dp, nn)):
                            os.rename(os.path.join(dp, fn), os.path.join(dp, nn))
                            nren += 1
                    for dn in dns:
                        nn = P.rename_dir_component(eng, dn)
                        if nn != dn and not os.path.exists(os.path.join(dp, nn)):
                            os.rename(os.path.join(dp, dn), os.path.join(dp, nn))
                            nren += 1
            if not repl and not nren:
                return False, ({"members": minfo} if minfo else {})
            for dp, _dn, fns in os.walk(sub):
                for fn in fns:
                    if fn.endswith(".out") or fn.endswith(".tmp~"):
                        os.unlink(os.path.join(dp, fn))
            out7 = os.path.join(tmp, "out.7z")
            subprocess.run(["7z", "a", "-t7z", "-bd", "-y", out7, "."], cwd=sub, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True, timeout=3600)
            os.replace(out7, dst)
        else:
            # rar: cannot be re-created with free tools; report, keep original
            return False, {"note": "rar-members-need-change", "members": minfo}
        info = {"n": total + len(names), "members": minfo}
        if names:
            info["renamed_members"] = len(names)
        return True, info
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def _work(item):
    rel, size, out_root, out_rel = item
    eng = _ENGINE
    prepare(eng, rel)
    src = os.path.join(util.INPUT, rel)
    dst = os.path.join(out_root, out_rel)
    t0 = time.time()
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.lexists(dst):
            os.unlink(dst)
        ch, kind, info = process(rel, src, dst)
        if not ch:
            os.link(src, dst)
    except Exception as ex:
        ch, kind, info = False, "?", {"error": "driver:%s:%s" % (type(ex).__name__, str(ex)[:160])}
        try:
            if not os.path.lexists(dst):
                os.link(src, dst)
        except OSError as ex2:
            info["error"] += "|link:%s" % ex2
    rec = {"p": rel, "k": kind, "s": size, "c": int(ch), "t": round(time.time() - t0, 2)}
    if out_rel != rel:
        rec["o"] = out_rel
    rec.update(info)
    if eng.stats:
        # per-entry counts for dictionary entries; name parts promoted by
        # document scope have no stable id and are only counted
        st = {k: v for k, v in eng.stats.items() if 0 <= k < eng.ndict}
        if st:
            rec["st"] = st
        extra = sum(v for k, v in eng.stats.items() if k >= eng.ndict)
        if extra:
            rec["sv"] = extra
        if eng.stats.get(-1):
            rec["sf"] = eng.stats[-1]      # account / author fields replaced without a dictionary entry
    return rec


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=util.OUT)
    ap.add_argument("--dict", default=os.path.join(util.WORK, "dict", "dict.json"))
    ap.add_argument("--key", default=os.path.join(util.WORK, "secret.key"))
    ap.add_argument("--journal", default=None)
    ap.add_argument("--prefix", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--paths", default=None, help="file with relative paths to (re)process")
    ap.add_argument("--rename", choices=("owner", "top", "all"), default=None,
                    help="rename paths that carry an identity: only the top-level source "
                         "directory (top) or every component and archive member (all)")
    ap.add_argument("--pdftext", action="store_true", help="replace identities in visible PDF text")
    ap.add_argument("--creo", action="store_true", help="edit string values in Creo files and Parasolid binary headers")
    ap.add_argument("--zlb", action="store_true", help="edit zlib-compressed sub-streams of pre-2015 SolidWorks files")
    ap.add_argument("--dwg", action="store_true", help="edit the metadata sections of DWG (R2004+) files")
    ap.add_argument("--no-field-fallback", action="store_true",
                    help="do not pseudonymise identity-field values that have no dictionary entry "
                         "(used by second-pass checks: the fallback is not idempotent by design)")
    ap.add_argument("--scope", action="store_true",
                    help="document scope: an identity found in a file's identity fields, text or path is "
                         "replaced everywhere in that file")
    ap.add_argument("--pdfmeta-all", action="store_true",
                    help="PDF: pseudonymise every descriptive metadata value word by word (title, subject, "
                         "keywords, producing software), not only the identity fields")
    ap.add_argument("--dwgobj", action="store_true",
                    help="also edit text inside DWG object data (bit level, per-object CRC kept valid)")
    ap.add_argument("--logos", default=None,
                    help="pdflogos.json from pdfharvest.py: replace repeated small images in PDFs (needs --pdftext)")
    ap.add_argument("--previews", action="store_true",
                    help="coarsen embedded sheet previews of drawing files (title block text)")
    ap.add_argument("--pdfextra", action="store_true",
                    help="PDF bookmarks, annotation authors/comments, form values; drop page thumbnails")
    ap.add_argument("--strings", action="store_true",
                    help="edit text runs (paths, user names) in otherwise unparsed streams of native files")
    args = ap.parse_args(argv)
    opts = {"rename": args.rename, "pdftext": args.pdftext, "creo": args.creo, "zlb": args.zlb,
            "strings": args.strings, "dwg": args.dwg, "dwgobj": args.dwgobj, "pdfmeta": args.pdfmeta_all, "scope": args.scope, "nofallback": args.no_field_fallback, "pdfextra": args.pdfextra,
            "previews": args.previews, "logos": args.logos}
    os.makedirs(args.out, exist_ok=True)
    tmproot = os.path.join(util.WORK, "tmp")
    os.makedirs(tmproot, exist_ok=True)
    files = [f for f in util.walk_input() if f[0].startswith(args.prefix)]
    if args.paths:
        with open(args.paths, encoding="utf-8") as fh:
            want = set(l.rstrip("\n") for l in fh if l.strip())
        files = [f for f in files if f[0] in want]
    if args.limit:
        step = max(1, len(files) // args.limit)
        files = files[::step][:args.limit]
    files.sort(key=lambda f: -f[1])
    journal = args.journal or os.path.join(util.WORK, "journal.jsonl")
    t0 = time.time()
    n = changed = errors = 0
    mode = "a" if args.paths else "w"
    # output paths (identical to the input paths unless --rename is given)
    out_rel = {rel: rel for rel, _s in files}
    if args.rename:
        from deid import paths as P
        _init(args.dict, args.key, tmproot, opts)
        taken = {}
        for rel, new in P.map_all(_ENGINE, [r for r, _s in util.walk_input()], args.rename, bool(args.scope)):
            if new in taken or len(new.encode("utf-8")) > 1000:
                new = rel                      # never merge two files into one name
            if new in taken:
                raise SystemExit("unresolvable path collision: %s" % rel)
            taken[new] = rel
            if rel in out_rel:
                out_rel[rel] = new
        sys.stderr.write("rename: %d of %d paths change\n" % (
            sum(1 for a, b in taken.items() if a != b), len(taken)))
    with open(journal, mode, encoding="utf-8") as jf, \
            mp.Pool(args.jobs, initializer=_init, initargs=(args.dict, args.key, tmproot, opts)) as pool:
        items = [(rel, size, args.out, out_rel[rel]) for rel, size in files]
        for rec in pool.imap_unordered(_work, items, chunksize=8):
            n += 1
            changed += rec["c"]
            errors += 1 if rec.get("error") else 0
            jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if n % 2000 == 0:
                sys.stderr.write("%d/%d files, %d changed, %d errors, %.0fs\n" % (n, len(files), changed, errors, time.time() - t0))
                sys.stderr.flush()
    sys.stderr.write("done: %d files, %d changed, %d errors, %.0fs\n" % (n, changed, errors, time.time() - t0))


if __name__ == "__main__":
    main(sys.argv[1:])
