"""Collect short visible-text lines of PDF drawings per source group.

Title-block text (company, drawn-by, addresses, phone numbers) repeats across
the drawings of a project. This pass extracts the short text lines of the
first pages of every PDF and aggregates them per source group with document
frequencies. The result is review material for curating identities that never
occur in a metadata field; it is private and not part of the submission.

Usage: python -I pdfharvest.py [--jobs N] [--out FILE]
"""
import collections
import json
import multiprocessing as mp
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deid import util  # noqa: E402

_NUMERIC = re.compile(r"^[\s\d.,:;+\-±°ØøxX/()<>=%\"'#*RrMm]*$")


def image_digests(doc, max_pages=3):
    """(digest, display fraction of the page, pixel width, pixel height) of the
    images on the first pages."""
    import hashlib
    out = {}
    for page in doc:
        if page.number >= max_pages:
            break
        area = abs(page.rect.width * page.rect.height) or 1.0
        try:
            images = page.get_images(full=True)
        except Exception:
            continue
        for img in images[:60]:
            xref = img[0]
            try:
                rects = page.get_image_rects(xref)
                raw = doc.xref_stream_raw(xref)
            except Exception:
                continue
            if not rects or not raw:
                continue
            frac = max(abs(r.width * r.height) for r in rects) / area
            out[hashlib.md5(raw).hexdigest()] = (round(frac, 5), img[2], img[3])
    return out


def _one(rel):
    import pymupdf
    out = set()
    try:
        doc = pymupdf.open(os.path.join(util.INPUT, rel))
    except Exception:
        return rel, None, None
    try:
        if doc.needs_pass:
            return rel, None, None
        try:
            imgs = image_digests(doc)
        except Exception:
            imgs = {}
        for page in doc:
            if page.number >= 4:
                break
            try:
                text = page.get_text("text")
            except Exception:
                continue
            for line in text.split("\n"):
                line = util.squash(line)
                if 3 <= len(line) <= 70 and not _NUMERIC.match(line):
                    out.add(line)
            if len(out) > 1500:
                break
    finally:
        doc.close()
    return rel, sorted(out)[:1500], imgs


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--out", default=os.path.join(util.WORK, "pdflines.jsonl"))
    args = ap.parse_args(argv)
    files = [p for p, s in util.walk_input() if p.lower().endswith(".pdf") and s > 0]
    by = collections.defaultdict(collections.Counter)
    ndocs = collections.Counter()
    imgdocs = collections.defaultdict(collections.Counter)
    imginfo = {}
    with mp.Pool(args.jobs) as pool:
        for rel, lines, imgs in pool.imap_unordered(_one, files, chunksize=8):
            if lines is None:
                continue
            g = util.group_of(rel)
            ndocs[g] += 1
            for ln in lines:
                by[g][ln] += 1
            for dg, info in (imgs or {}).items():
                imgdocs[g][dg] += 1
                imginfo[dg] = info
    # logo candidates: small images repeated on several drawings of a group
    logos = {}
    for g, cnt in imgdocs.items():
        need = 3 if ndocs[g] >= 6 else 2
        pick = [dg for dg, c in cnt.items()
                if c >= need and imginfo[dg][0] < 0.05 and imginfo[dg][1] >= 24 and imginfo[dg][2] >= 12]
        if pick:
            logos[g] = sorted(pick)
    with open(os.path.join(os.path.dirname(args.out), "pdflogos.json"), "w", encoding="utf-8") as fh:
        json.dump(logos, fh)
    print("groups with repeated small images (logo candidates): %d, images: %d" % (
        len(logos), sum(len(v) for v in logos.values())))
    with open(args.out, "w", encoding="utf-8") as fh:
        for g in sorted(by):
            fh.write(json.dumps({"g": g, "docs": ndocs[g], "lines": by[g].most_common(4000)}, ensure_ascii=False) + "\n")
    print("groups with PDF text: %d, pdfs: %d" % (len(by), sum(ndocs.values())))


if __name__ == "__main__":
    main(sys.argv[1:])
