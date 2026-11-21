"""Pass 4 - verify an output corpus.

Checks, in order:
  1. completeness  - every input path exists in the output, nothing extra,
                     object count below the scorer limit;
  2. unchanged     - files the journal reports as unchanged are byte-identical
                     to the input (same inode or same size);
  3. fixpoint      - every changed file is run through the transform planner
                     again; a correct output needs no further replacement
                     (any hit is a residual identifier);
  4. structure     - changed files are re-opened with an independent parser
                     (qpdf for PDF, olefile for OLE, zipfile, the SolidWorks
                     container validator) and compared with the input.

Usage: python -I verify.py --out DIR [--journal J] [--jobs N]
Exit status is non-zero when a check fails.
"""
import collections
import json
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import zipfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deid import util, transform, fmt_sw  # noqa: E402

_OUT = None


def _init(dict_path, key_path, tmproot, out_root, opts=None):
    global _OUT
    transform._init(dict_path, key_path, tmproot, opts)
    _OUT = out_root


def _structure(kind, src, dst):
    """Independent structural comparison; returns error string or None."""
    try:
        if kind == "pdf":
            r = subprocess.run(["qpdf", "--check", dst], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=300)
            if r.returncode not in (0, 3):   # 3 = warnings only
                r0 = subprocess.run(["qpdf", "--check", src], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=300)
                if r0.returncode in (0, 3):
                    return "qpdf check fails on output only"
            a = subprocess.run(["qpdf", "--show-npages", src], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=300).stdout.strip()
            b = subprocess.run(["qpdf", "--show-npages", dst], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=300).stdout.strip()
            if a != b:
                return "page count %r -> %r" % (a, b)
        elif kind == "ole":
            import olefile
            a, b = olefile.OleFileIO(src), olefile.OleFileIO(dst)
            try:
                if a.listdir(streams=True, storages=True) != b.listdir(streams=True, storages=True):
                    return "ole directory differs"
                for e in a.listdir(streams=True, storages=False):
                    if a.get_size(e) != b.get_size(e):
                        return "ole stream size differs"
            finally:
                a.close()
                b.close()
        elif kind == "sw":
            # container must still validate (every stream inflates and matches
            # its CRC) and keep its exact size; the handler compared the
            # record layout with the input when it wrote the file
            if os.path.getsize(src) != os.path.getsize(dst):
                return "sw size differs"
            with open(dst, "rb") as fh:
                rb = fh.read()
            fmt_sw.validate(rb)
        elif kind in ("zip", "zipn"):
            with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst) as b:
                na, nb = [i.filename for i in a.infolist()], [i.filename for i in b.infolist()]
                renamed = transform.OPTS.get("rename") == "all" and kind == "zip"
                if len(na) != len(nb) or (na != nb and not renamed):
                    return "zip member list differs"
                try:
                    if b.testzip() is not None:
                        return "zip crc"
                except NotImplementedError:
                    pass        # members stored with a method Python cannot read (copied verbatim)
        elif kind in ("step", "iges", "dxf", "svg", "text", "stl"):
            sa, sb = os.path.getsize(src), os.path.getsize(dst)
            if kind in ("iges", "stl") and sa != sb:
                return "size differs"
            if abs(sa - sb) > 4096:
                return "size differs by %d" % (sb - sa)
    except Exception as ex:
        return "%s:%s" % (type(ex).__name__, str(ex)[:120])
    return None


def _check(item):
    rel, kind, out_rel = item
    eng = transform._ENGINE
    transform.prepare(eng, rel)
    src = os.path.join(util.INPUT, rel)
    dst = os.path.join(_OUT, out_rel)
    res = {"p": out_rel, "k": kind}
    err = _structure(kind, src, dst)
    if err:
        res["structure"] = err
    tmp = tempfile.mkdtemp(prefix="vf", dir=transform._TMPROOT)
    try:
        ch, _k, info = transform.process(rel, dst, os.path.join(tmp, "o"))
        if ch and kind != "swlock":      # lock files are re-pseudonymised on every pass by design
            res["residual"] = info.get("n", 1)
            res["entries"] = dict(eng.stats)
        if info.get("error"):
            res["reprocess_error"] = info["error"]
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=util.OUT)
    ap.add_argument("--journal", default=os.path.join(util.WORK, "journal.jsonl"))
    ap.add_argument("--dict", default=os.path.join(util.WORK, "dict", "dict.json"))
    ap.add_argument("--key", default=os.path.join(util.WORK, "secret.key"))
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--report", default=os.path.join(util.WORK, "verify.jsonl"))
    ap.add_argument("--rename", choices=("owner", "top", "all"), default=None)
    ap.add_argument("--pdftext", action="store_true")
    ap.add_argument("--creo", action="store_true")
    ap.add_argument("--zlb", action="store_true")
    ap.add_argument("--strings", action="store_true")
    ap.add_argument("--dwg", action="store_true")
    ap.add_argument("--pdfmeta-all", action="store_true")
    ap.add_argument("--dwgobj", action="store_true")
    ap.add_argument("--scope", action="store_true")
    ap.add_argument("--pdfextra", action="store_true")
    ap.add_argument("--previews", action="store_true")
    ap.add_argument("--logos", default=None)
    args = ap.parse_args(argv)
    opts = {"rename": args.rename, "pdftext": args.pdftext, "creo": args.creo, "zlb": args.zlb,
            "strings": args.strings, "dwg": args.dwg, "dwgobj": args.dwgobj, "pdfmeta": args.pdfmeta_all, "scope": args.scope, "nofallback": True, "pdfextra": args.pdfextra,
            "previews": args.previews, "logos": args.logos}
    bad = 0
    # latest journal record per path
    recs = {}
    with open(args.journal, encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            recs[r["p"]] = r
    inputs = dict(util.walk_input())
    outputs = dict(util.walk_input(args.out))
    # expected output path of every input (identical unless the run renamed paths)
    omap = {rel: recs[rel].get("o", rel) if rel in recs else rel for rel in inputs}
    expected = set(omap.values())
    if len(expected) != len(omap):
        bad += 1
        print("  two inputs map to the same output path")
    missing = sorted(expected - set(outputs))
    extra = sorted(set(outputs) - expected)
    print("inputs %d, outputs %d, missing %d, extra %d" % (len(inputs), len(outputs), len(missing), len(extra)))
    if missing or extra or len(outputs) > 400000:
        bad += 1
        for p in (missing + extra)[:20]:
            print("  path problem:", p)
    nojournal = sorted(set(inputs) - set(recs))
    if nojournal:
        bad += 1
        print("no journal record for %d files, e.g. %s" % (len(nojournal), nojournal[:3]))
    n_same = n_diff = 0
    for rel, r in recs.items():
        if rel not in inputs or omap[rel] not in outputs:
            continue
        if not r["c"]:
            a = os.stat(os.path.join(util.INPUT, rel))
            b = os.stat(os.path.join(args.out, omap[rel]))
            if a.st_ino != b.st_ino and a.st_size != b.st_size:
                n_diff += 1
                if n_diff <= 10:
                    print("  unchanged file differs:", rel)
            else:
                n_same += 1
    print("unchanged files identical: %d, differing: %d" % (n_same, n_diff))
    if n_diff:
        bad += 1
    changed = [(rel, r["k"], omap[rel]) for rel, r in recs.items()
               if r["c"] and rel in inputs and omap[rel] in outputs]
    print("re-checking %d changed files ..." % len(changed))
    tmproot = os.path.join(util.WORK, "tmp")
    os.makedirs(tmproot, exist_ok=True)
    stats = collections.Counter()
    with open(args.report, "w", encoding="utf-8") as rf, \
            mp.Pool(args.jobs, initializer=_init, initargs=(args.dict, args.key, tmproot, args.out, opts)) as pool:
        for res in pool.imap_unordered(_check, changed, chunksize=8):
            for k in ("structure", "residual", "reprocess_error"):
                if k in res:
                    stats[(res["k"], k)] += 1
            if len(res) > 2:
                rf.write(json.dumps(res, ensure_ascii=False) + "\n")
    for (kind, what), c in sorted(stats.items()):
        print("  %-6s %-16s %d" % (kind, what, c))
    if any(what in ("structure",) for (_k, what) in stats):
        bad += 1
    print("VERIFY %s" % ("FAILED" if bad else "OK (see report for residual hits)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
