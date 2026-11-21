"""Census guard: use the per-entry replacement statistics of a transform run to
find dictionary entries that behave like ordinary vocabulary rather than like
an identity, and demote them.

An entry is suspicious when it was replaced in many source groups where it was
never seen in an identity field (its "home" groups). Such entries are demoted
to tier L (home groups only), or to B when even that looks unsafe.

Usage: python -I census.py --journal J [--dict D] [--out demote.json]
"""
import collections
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deid import util  # noqa: E402


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default=os.path.join(util.WORK, "journal.jsonl"))
    ap.add_argument("--dict", default=os.path.join(util.WORK, "dict", "dict.json"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    out = args.out or os.path.join(os.path.dirname(args.dict), "demote.json")
    with open(args.dict, encoding="utf-8") as fh:
        entries = json.load(fh)["entries"]
    hits = collections.Counter()
    groups = collections.defaultdict(set)
    files = collections.Counter()
    with open(args.journal, encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            st = r.get("st")
            if not st:
                continue
            g = util.group_of(r["p"])
            for eid, c in st.items():
                eid = int(eid)
                hits[eid] += c
                files[eid] += 1
                groups[eid].add(g)
    demote = []
    if os.path.exists(out):
        with open(out, encoding="utf-8") as fh:
            demote = json.load(fh)
    known = {tuple(t) for t, _tier in demote}
    report = []
    for eid, gs in groups.items():
        e = entries[eid]
        if e["tier"] != "A" or tuple(e["t"]) in known:
            continue
        home = set(e["g"])
        outside = gs - home
        # short single words that spread far beyond the projects where they
        # were seen as an identity behave like vocabulary, not like a name
        if len(e["t"]) == 1 and len(e["t"][0]) < 9 and len(outside) >= 5 and len(outside) > 3 * len(home):
            demote.append([e["t"], "L"])
            report.append((len(outside), len(home), hits[eid], files[eid], " ".join(e["t"]), e["cls"]))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(demote, fh, ensure_ascii=False)
    report.sort(reverse=True)
    for r in report[:80]:
        print("demote A->L: outside_groups=%d home=%d hits=%d files=%d  %s (%s)" % r)
    print("%d entries demoted in total" % len(demote))


if __name__ == "__main__":
    main(sys.argv[1:])
