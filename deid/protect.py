"""Reference protection for runs that keep file names unchanged.

When paths are not renamed, an identity that occurs inside a *file name* must
also stay untouched wherever that file name is written inside another file
(assembly component lists, drawing references, external references in STEP,
relative links). Otherwise the reference would no longer resolve.

This pass finds every corpus file name that contains a dictionary match and
writes the list of such names (without extension) to protect.json. The engine
then leaves those names alone when they are followed by a file extension.

Usage: python -I protect.py [--dict D] [--harvest H] [--out protect.json]
"""
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deid import util  # noqa: E402
from deid import engine as E  # noqa: E402

_VERSION = re.compile(r"\.\d+$")


def stem_of(base):
    base = _VERSION.sub("", base)          # Creo version suffix
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dict", default=os.path.join(util.WORK, "dict", "dict.json"))
    ap.add_argument("--key", default=os.path.join(util.WORK, "secret.key"))
    ap.add_argument("--harvest", default=os.path.join(util.WORK, "harvest"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    out = args.out or os.path.join(os.path.dirname(args.dict), "protect.json")
    with open(args.key, "rb") as fh:
        key = fh.read()
    eng = E.Engine(args.dict, key)
    stems = set()
    cache = {}
    n = 0
    with open(os.path.join(args.harvest, "files.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            p = json.loads(line)["p"]
            eng.group = util.group_of(p.split("!/", 1)[0])
            base = p.replace("!/", "/").rsplit("/", 1)[-1]
            k = (eng.group, base)
            hit = cache.get(k)
            if hit is None:
                st = stem_of(base)
                hit = cache[k] = (eng.replace_text(st, "text")[0] != st)
            if hit:
                n += 1
                st = stem_of(base)
                if len(st) >= 3:
                    stems.add(st)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(sorted(stems), fh, ensure_ascii=False)
    print("%d files carry an identity in their name; %d distinct names protected" % (n, len(stems)))


if __name__ == "__main__":
    main(sys.argv[1:])
