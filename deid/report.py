"""Write README.txt for a submission from the transform journal.

The README lists how to run the pipeline, what each pass does, what is and is
not covered, and every file that could not be processed (by final path), so
nothing is dropped silently.

Usage: python -I report.py --journal J --out README.txt [--attempt N]
"""
import collections
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deid import util  # noqa: E402

HEAD = """MACHINA CAD DE-ID - SUBMISSION NOTES (attempt %(attempt)s)
================================================================

Layout
  corpus/      de-identified corpus, one output file per input file
  code/        the complete pipeline that produced it (this README describes it)
  README.txt   this file

HOW TO RUN
----------
Requirements: Ubuntu 24.04, Python 3.12, and the packages
    sudo apt-get install -y python3-venv p7zip-full ncompress qpdf poppler-utils
    python3 -m venv venv && venv/bin/pip install -r code/requirements.txt

Environment (all paths are examples):
    export DEID_INPUT=/data/input          # raw corpus (read-only)
    export DEID_WORK=/data/work            # private state: dictionary, key, journal, temp
    export DEID_OUT=/data/out/corpus       # output corpus

Whole pipeline in one command (harvest, dictionary, transform, verify, notes):
    bash code/run_all.sh venv/bin/python         # DEID_RENAME=all renames paths as well

Passes (each is restartable; run Python in isolated mode, see code/README.md):
    venv/bin/python -I code/deid/harvest.py                 # 1. read identity fields from every file
    venv/bin/python -I code/deid/pdfharvest.py              #    title-block text and repeated images of PDFs
    venv/bin/python -I code/deid/build_dict.py              # 2. build identity dictionary + pseudonyms
    venv/bin/python -I code/deid/textharvest.py             #    identities completed from drawing text
    venv/bin/python -I code/deid/build_dict.py              #    final dictionary (reads the corpus manifest
                                                            #    to treat exact copies of a file alike)
    venv/bin/python -I code/deid/transform.py --out $DEID_OUT %(flags)s
    venv/bin/python -I code/deid/verify.py --out $DEID_OUT %(flags)s
    venv/bin/python -I code/deid/report.py --out README.txt # this file

The pseudonym key ($DEID_WORK/secret.key) is generated on first use and is not
part of the submission; with a different key the same pipeline produces the
same structure with different pseudonyms. The identity dictionary and the
journal stay in $DEID_WORK and are not submitted.

WHAT THE PIPELINE DOES
----------------------
* Identity discovery. Pass 1 reads the fields where identities live: STEP
  header and PERSON/ORGANIZATION/ADDRESS entities, IGES global section, PDF
  Info and XMP, OLE property sets (SolidWorks, Inventor, Solid Edge), the
  SolidWorks 2015+ container (docProps XML, serialized strings), FreeCAD /
  3MF / Fusion archives, DXF header variables, SVG metadata, image EXIF, and
  user-name components of file-system paths, e-mail addresses and URLs
  anywhere. Archives are opened recursively.
* Dictionary. Values are split into identities (person, organisation,
  account, host, domain) with alias handling ("DOMAIN\\user (HOST)", e-mail
  addresses, 8.3 short names, separators and case). Each identity gets one
  deterministic pseudonym (HMAC-keyed), the same everywhere in the corpus;
  distinct identities get distinct pseudonyms (collision-checked). Pseudonyms
  are pronounceable, keep the length, letter case and punctuation of the
  original, and digits stay digits, so identity fields stay populated and
  fixed-width records, string length prefixes and stream sizes stay valid.
* Protection of engineering content. Placeholders ("Administrator",
  "Unknown"), CAD/office software names, engineering vocabulary, ordinary
  dictionary words, calendar words and catalogue-part vendors are never
  replaced in free text. Short or ordinary-looking names are replaced only in
  identity fields, or in free text of the source projects where they were
  seen as an identity. A census of the run demotes any entry that behaves
  like vocabulary.
* Format-aware writing. Only text-bearing positions that the handler
  understands are edited (string literals in STEP, counted strings in ACIS,
  text group codes in DXF, text nodes / attribute values in XML, comments in
  mesh and G-code files, property-set strings in OLE files, length-prefixed
  strings in SolidWorks streams). Geometry, numeric data and format keywords
  are never touched. Native containers are patched in place: OLE streams keep
  their size; SolidWorks 2015+ streams are re-deflated to exactly the same
  compressed size and their CRC-32 fields updated, so no offset moves. Every
  written file is re-parsed and compared with the input before it is
  accepted; on any doubt the original bytes are kept and the file is listed
  below.
* Files with nothing to replace are byte-identical to the input.

"""

LIMITS = """LIMITATIONS IN THIS ATTEMPT
---------------------------
* File and folder names are unchanged in this attempt, including names that
  carry an account, project or domain name. To keep references valid, a file
  name that contains an identity is also left as it is wherever another file
  refers to it (name followed by its extension).
* Visible text inside PDF page content, text rendered as vector outlines or
  scanned images, logos and embedded preview images are not edited in this
  attempt. PDF document metadata (Info dictionary and XMP) is.
* Images: pixels are never changed. EXIF / XMP text tags are pseudonymised
  and GPS position data is emptied; logos inside pictures are not treated.
* Binary bodies that are not parsed in this attempt are passed through
  unchanged: DWG, CATIA V5, JT, NX, Parasolid binary, U3D, the body of Creo
  files, and zlib-compressed sub-streams ("__ZLB") of pre-2015 SolidWorks
  files.
* RAR archives cannot be re-created with free tools; they are passed through.
  Zip-based files that use a compression method Python cannot write are
  passed through as well (listed below).
* Part numbers and project codes are replaced only where they coincide with a
  harvested identity. Catalogue-part vendors keep their names in free text
  and bills of material.
"""


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default=os.path.join(util.WORK, "journal.jsonl"))
    ap.add_argument("--out", default="README.txt")
    ap.add_argument("--attempt", default="1")
    ap.add_argument("--limits", default=None, help="file with the limitations section (optional)")
    ap.add_argument("--flags", default="[options]", help="transform options used for this build (shown in the notes)")
    args = ap.parse_args(argv)
    recs = {}
    with open(args.journal, encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            recs[r["p"]] = r
    kinds = collections.Counter()
    changed = collections.Counter()
    errors = []
    notes = []
    empty = []
    nrep = 0
    for p, r in sorted(recs.items()):
        p = r.get("o", p)          # always report the final (output) path
        kinds[r["k"]] += 1
        changed[r["k"]] += r["c"]
        nrep += r.get("n", 0) if r["c"] else 0
        if r["k"] == "empty" or r.get("s", 1) == 0:
            empty.append(p)
        if r.get("error"):
            errors.append((p, r["k"], r["error"]))
        if r.get("note"):
            notes.append((p, r["k"], r["note"]))
        if r.get("pdfnote"):
            # pages whose visible text was kept because the rewritten page failed a check
            for part in r["pdfnote"].split("; "):
                notes.append((p, r["k"], re.sub(r"page \d+ ", "one page: ", part)))
        if r.get("sw_nofit"):
            notes.append((p, r["k"], "%d stream(s) could not be re-stored in place and were left as they were" % r["sw_nofit"]))
        for m in r.get("members") or ():
            if m.get("error"):
                errors.append((p + " :: member " + m["m"], m["k"], m["error"]))
            if m.get("note"):
                notes.append((p + " :: member " + m["m"], m["k"], m["note"]))
    out = [HEAD % {"attempt": args.attempt, "flags": args.flags}]
    if args.limits and os.path.exists(args.limits):
        out.append(open(args.limits, encoding="utf-8").read())
    else:
        out.append(LIMITS)
    out.append("\nRUN SUMMARY\n-----------\n")
    out.append("files in: %d    files out: %d    files modified: %d    replacements: %d\n\n" % (
        len(recs), len(recs), sum(changed.values()), nrep))
    out.append("%-10s %9s %9s   (kind as detected from content)\n" % ("kind", "files", "modified"))
    for k, c in kinds.most_common():
        out.append("%-10s %9d %9d\n" % (k, c, changed[k]))
    out.append("\nSOURCE DEFECTS AND EXCEPTIONS (final paths)\n-------------------------------------------\n")
    out.append("Empty input files (0 bytes), passed through as empty files: %d\n" % len(empty))
    for p in empty:
        out.append("  " + p + "\n")

    def neutral(msg):
        # error texts may quote temp or output paths, checksums and command
        # lines; keep the exception class and a neutral reason only
        msg = re.split(r"(?:[\w.~-]*/){2,}", msg)[0]
        msg = re.sub(r"Command '\['(\w+)'.*", r"external tool '\1' failed", msg)
        msg = re.sub(r"0x[0-9a-fA-F]+", "0x..", msg)
        msg = re.sub(r"\(b'.*", "", msg)
        return msg.strip(" :'\"([")[:110]

    out.append("\nFiles the handler could not parse or validate; the original bytes are kept (%d):\n" % len(errors))
    by = collections.defaultdict(list)
    for p, k, e in errors:
        by[(k, neutral(e))].append(p)
    for (k, e), ps in sorted(by.items(), key=lambda kv: -len(kv[1])):
        out.append("\n [%s] %s  (%d files)\n" % (k, e, len(ps)))
        for p in ps:
            out.append("   " + p + "\n")
    out.append("\nOther notes (%d):\n" % len(notes))
    byn = collections.defaultdict(list)
    for p, k, e in notes:
        byn[(k, e)].append(p)
    for (k, e), ps in sorted(byn.items(), key=lambda kv: -len(kv[1])):
        out.append("\n [%s] %s  (%d files)\n" % (k, e, len(ps)))
        for p in ps[:400]:
            out.append("   " + p + "\n")
        if len(ps) > 400:
            out.append("   ... and %d more of the same kind\n" % (len(ps) - 400))
    text = "".join(out)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print("wrote %s (%d bytes, %d errors, %d notes)" % (args.out, len(text), len(errors), len(notes)))


if __name__ == "__main__":
    main(sys.argv[1:])
