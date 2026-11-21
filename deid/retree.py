"""Build a renamed copy of an existing output corpus without reprocessing.

When only top-level source directories are renamed, file contents are the
same as in the unrenamed run. This tool hard-links every file of an existing
output tree into a new tree under its renamed path and writes a journal with
the path mapping (field "o").

Usage: python -I retree.py --src OUT1/corpus --dst OUT2/corpus --journal J1 --out-journal J2
"""
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deid import util, paths as P  # noqa: E402
from deid import engine as E  # noqa: E402


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--out-journal", required=True)
    ap.add_argument("--dict", default=os.path.join(util.WORK, "dict", "dict.json"))
    ap.add_argument("--key", default=os.path.join(util.WORK, "secret.key"))
    ap.add_argument("--level", default="owner", choices=("owner", "top"))
    args = ap.parse_args(argv)
    with open(args.key, "rb") as fh:
        eng = E.Engine(args.dict, fh.read())
    recs = {}
    with open(args.journal, encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            recs[r["p"]] = r
    taken = {}
    n = changed = 0
    with open(args.out_journal, "w", encoding="utf-8") as jf:
        for rel in sorted(recs):
            r = dict(recs[rel])
            eng.group = util.group_of(rel)
            new = P.map_path(eng, rel, args.level)
            if new in taken:
                new = rel
            if new in taken:
                raise SystemExit("path collision: %s" % rel)
            taken[new] = rel
            src = os.path.join(args.src, r.get("o", rel))
            dst = os.path.join(args.dst, new)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.lexists(dst):
                os.unlink(dst)
            os.link(src, dst)
            r.pop("o", None)
            if new != rel:
                r["o"] = new
                changed += 1
            r.pop("st", None)
            jf.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    print("linked %d files, %d under a renamed path" % (n, changed))


if __name__ == "__main__":
    main(sys.argv[1:])
