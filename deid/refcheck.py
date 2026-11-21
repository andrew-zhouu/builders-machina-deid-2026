"""Reference-resolution check.

For native assemblies and drawings (SolidWorks old and new containers,
Inventor, Solid Edge) collect the file names they mention and test whether a
file of that name exists in the same source group of the tree being checked.
Run on input and output: the resolved fraction must not drop.

Usage: python -I refcheck.py ROOT [--prefix P] [--limit N] [--map journal.jsonl]
"""
import collections
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deid import util, fmt_sw  # noqa: E402

_EXTS = ("sldprt", "sldasm", "slddrw", "ipt", "iam", "idw", "ipn", "par", "psm", "asm", "dft")
_REF16 = re.compile((r"(?:[^\x00-\x1f\\/:*?\"<>|]\x00){1,120}\.\x00(?:%s)\x00" % "|".join(
    "".join("[%s%s]\x00" % (c, c.upper()) for c in e)[:-1] for e in _EXTS)).encode("latin-1"))
_REF8 = re.compile((r"[^\x00-\x1f\\/:*?\"<>|]{1,120}\.(?i:%s)\b" % "|".join(_EXTS)).encode("latin-1"))
_HOLDERS = ("sldasm", "slddrw", "iam", "idw", "ipn", "asm", "dft")


def refs_in(data):
    out = set()
    for m in _REF16.finditer(data):
        try:
            out.add(m.group().decode("utf-16-le").lower())
        except UnicodeDecodeError:
            pass
    for m in _REF8.finditer(data):
        s = m.group()
        if b"\x00" not in s:
            out.add(s.decode("latin-1").lower())
    return out


def file_refs(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:8] == util.OLE_MAGIC:
        return refs_in(raw)
    streams = fmt_sw.parse(raw)
    out = set()
    if streams:
        for s in streams:
            if s.usize > (32 << 20) or s.name in ("Preview", "PreviewPNG"):
                continue
            try:
                out |= refs_in(fmt_sw.read_stream(raw, s))
            except Exception:
                pass
    return out


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    files = [p for p, _s in util.walk_input(args.root) if p.startswith(args.prefix)]
    by_dir = collections.defaultdict(set)
    for p in files:
        parts = p.split("/")
        by_dir["/".join(parts[:3])].add(parts[-1].lower())
    holders = [p for p in files if util.split_ext(p) in _HOLDERS]
    if args.limit:
        holders = holders[::max(1, len(holders) // args.limit)][:args.limit]
    tot = res = nfiles = 0
    for p in holders:
        try:
            refs = file_refs(os.path.join(args.root, p))
        except Exception:
            continue
        names = by_dir["/".join(p.split("/")[:3])]
        own = p.rsplit("/", 1)[-1].lower()
        refs = {r.strip() for r in refs if r.strip() != own}
        if not refs:
            continue
        nfiles += 1
        tot += len(refs)
        res += sum(1 for r in refs if r in names)
    print("%s: %d holder files with references, %d distinct references, %d resolve (%.1f%%)" % (
        args.root, nfiles, tot, res, 100.0 * res / max(1, tot)))


if __name__ == "__main__":
    main(sys.argv[1:])
