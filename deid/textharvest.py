"""Pass 1b - derive identities from visible drawing text.

Input : pdflines.jsonl (short text lines of PDF drawings, per source group,
        written by pdfharvest.py) and a first dictionary built from metadata.
Output: harvest/values_text.jsonl - extra (label, value) records for
        build_dict.py, each scoped to the group it was seen in.

Rules (deliberately conservative; every result is scoped to its own group):
  * name completion - a short line in which a known person token stands next
    to other capitalised words is a full name ("Jane Doe" when "Jane"
    is known from metadata);
  * labelled values  - "Drawn by: X", "Dessiné par X", "Author - X";
  * organisations    - short repeated lines with a legal form or an
    institution word ("Foo Robotics GmbH", "University of X");
  * copyright lines  - "(c) 2020 Foo Bar";
  * e-mail addresses and web addresses.
"""
import collections
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deid import util, pseudo  # noqa: E402
from deid import engine as E  # noqa: E402
from deid.build_dict import Lists, _RE_EMAIL, _RE_URL  # noqa: E402
from deid.regions import valid_host  # noqa: E402

_LABEL = re.compile(
    r"(?i)^\s*(?:drawn|designed|checked|approved|created|modell?ed|author|engineer|"
    r"dessin[ée]|gezeichnet|gepr[uü]ft|dibujado|dise[ñn]ado|revisado|aprobado|dissenyat|projetado|"
    r"desenhado|verificado|disegnato|controllato)\s*(?:by|par|von|por|per|da)?\s*[:\-–]\s*(.+)$")
_LEGAL = re.compile(
    r"(?i)(?:\b|(?<=\s))(gmbh|inc\.?|ltd\.?|llc|corp\.?|corporation|s\.a\.s?\.?|s\.l\.|b\.v\.|s\.r\.l\.?|"
    r"s\.p\.a\.?|co\.,? ?ltd\.?|pty|plc|oy|a/s)$")
_INSTITUTION = re.compile(
    r"(?i)\b(university|universit[äéàa]t?|universidad|universit[eé]|institut[eo]?|college|"
    r"laborator(y|ies|io)|fundaci[oó]n?|foundation|consortium|polit[eé]cnic[oa]|hochschule|observatory)\b")
_PARTICLE = {"of", "de", "la", "del", "for", "and", "für", "fur", "und", "di", "da", "do", "the", "at", "y", "e",
             "van", "von", "der", "den", "le", "du", "des", "&"}
_COPY = re.compile(r"(?i)(?:©|\(c\)|copyright)\s*(?:\(c\)\s*)?(?:\d{4}(?:\s*[-–]\s*\d{4})?)?[\s,]*(?:by\s+)?(.{3,60})$")
_WORD = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", re.U)


def _load_foreign():
    p = os.path.join(util.DATA_DIR, "words_foreign.txt")
    if not os.path.exists(p):
        return frozenset()
    with open(p, encoding="utf-8") as fh:
        return frozenset(w.strip() for w in fh if w.strip())


FOREIGN = _load_foreign()


def name_like(tokens_raw, L, known=(), strict=True):
    """True when the words form a personal name: every word is capitalised
    (or an initial / particle) and none is ordinary vocabulary, except words
    that are themselves known names."""
    if not 1 <= len(tokens_raw) <= 4:
        return False
    odd = 0
    for w in tokens_raw:
        c = pseudo.canon(w)
        if not c or any(ch.isdigit() for ch in c):
            return False
        if w.lower() in _PARTICLE and len(tokens_raw) > 2:
            continue
        if not (w[0].isupper() or len(w) <= 2):
            return False
        if c in L.generic_tok or c in L.calendar or c in L.eng or c in L.struct:
            return False
        if len(c) <= 2:
            continue                      # initial
        if c in known or not (c in L.words or (strict and c in FOREIGN)):
            odd += 1
        else:
            return False                  # an ordinary dictionary word (any language)
    return odd >= 1


def org_name(line, L):
    """Trailing run of capitalised words / particles of a line, e.g.
    "... de la Fundació Exemple" -> "Fundació Exemple"."""
    words = line.replace("/", " ").replace(",", " , ").split()
    out = []
    for w in reversed(words):
        core = w.strip(".,;:()[]\"'")
        if not core:
            if out:
                out.append(w)
            continue
        if core.lower() in _PARTICLE and out:
            out.append(w)
        elif core[0].isupper() or core.isupper():
            out.append(w)
        else:
            break
    out.reverse()
    while out and out[0].strip(".,;:()[]\"'").lower() in _PARTICLE | {","}:
        out.pop(0)
    while out and out[-1] == ",":
        out.pop()
    name = " ".join(out).replace(" , ", ", ")
    toks = pseudo.tokens_of(name)
    distinct = [t for t in toks if len(t) >= 3 and not L.common(t) and t not in L.generic_tok
                and not _INSTITUTION.fullmatch(t)]
    if len(toks) < 2 or len(toks) > 7 or not distinct or any(ch.isdigit() for ch in name):
        return None
    if L.is_vendor(toks) or any(t in L.vendor_tok for t in toks):
        return None
    return name


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", default=os.path.join(util.WORK, "pdflines.jsonl"))
    ap.add_argument("--dict", default=os.path.join(util.WORK, "dict", "dict.json"))
    ap.add_argument("--key", default=os.path.join(util.WORK, "secret.key"))
    ap.add_argument("--out", default=os.path.join(util.WORK, "harvest", "values_text.jsonl"))
    args = ap.parse_args(argv)
    L = Lists()
    with open(args.key, "rb") as fh:
        key = fh.read()
    with open(args.dict, encoding="utf-8") as fh:
        dic = json.load(fh)
    eng = E.Engine(dic, key)
    person_tok = set()
    for e in dic["entries"]:
        if (e["cls"] in ("PERSON", "ACCOUNT") and len(e["t"]) == 1 and e["tier"] in ("A", "L")
                and e["t"][0].isalpha() and len(e["t"][0]) >= 4 and not L.common(e["t"][0])
                and not L.is_vendor(e["t"])):
            person_tok.add(e["t"][0])
    # accounts built from a surname plus one initial ("smithj", "jsmith"):
    # the surname alone, seen as a capitalised word on the same project's drawings,
    # is the same person
    acct = collections.defaultdict(dict)        # group -> {surname candidate: account}
    for e in dic["entries"]:
        if e["cls"] in ("ACCOUNT", "PERSON") and len(e["t"]) == 1 and e["tier"] in ("A", "L"):
            t = e["t"][0]
            if t.isalpha() and len(t) >= 7:
                for g in e["g"]:
                    for cand in (t[1:], t[:-1]):
                        if not L.common(cand) and cand not in FOREIGN:
                            acct[g].setdefault(cand, t)
    out = collections.Counter()
    ex = {}

    exclude = set()
    xp = os.path.join(util.DATA_DIR, "text_exclude.txt")
    if os.path.exists(xp):
        with open(xp, encoding="utf-8") as fh:
            exclude = set(pseudo.canon(l) for l in fh if l.strip() and not l.startswith("#"))

    def emit(label, value, g, n):
        value = util.squash(value).strip(" .,;:-–")
        if pseudo.canon(value) in exclude or any(pseudo.canon(value).startswith(x) for x in exclude if len(x) > 12):
            return
        if 3 <= len(value) <= 60:
            out[(label, value, g)] += n
            ex.setdefault((label, value, g), g)

    with open(args.lines, encoding="utf-8") as fh:
        for row in fh:
            d = json.loads(row)
            g = d["g"]
            docs = d["docs"]
            eng.group = g
            surn = acct.get(g) or {}
            for line, n in d["lines"]:
                words = _WORD.findall(line)
                if surn:
                    for w in words:
                        c = pseudo.canon(w)
                        if c in surn and w[0].isupper() and c not in person_tok:
                            out[("pdftext.surname", c + "|" + surn[c], g)] += n
                # e-mail / web addresses
                for em in _RE_EMAIL.findall(line):
                    if valid_host(em.split("@", 1)[1]):
                        emit("pdftext.email", em, g, n)
                if re.search(r"\d", _RE_EMAIL.sub("", line)) and not _COPY.search(line):
                    continue
                m = _LABEL.match(line)
                if m:
                    val = m.group(1).strip()
                    vw = _WORD.findall(val)
                    if vw and name_like(vw, L, person_tok) and len(val) <= 40 and (
                            len(vw) >= 2 or pseudo.canon(vw[0]) in person_tok):
                        emit("pdftext.person", val, g, n)
                    continue
                m = _COPY.search(line)
                if m:
                    val = re.sub(r"(?i)\ball rights reserved.*$", "", m.group(1)).strip(" .,")
                    nm = org_name(val, L)
                    if nm:
                        emit("pdftext.org", nm, g, n)
                    continue
                if not words or len(words) > 7 or len(line) > 64:
                    continue
                canon = [pseudo.canon(w) for w in words]
                # name completion around a known, distinctive person token
                if 2 <= len(words) <= 4 and any(c in person_tok for c in canon):
                    # a known name is present: the neighbouring capitalised words are
                    # name parts even when they are ordinary words of some language
                    if name_like(words, L, person_tok, strict=False) and re.fullmatch(
                            r"[^\W\d_][\w.'’-]*(?:\s+[^\W\d_][\w.'’-]*){1,3}", line.strip(), re.U):
                        emit("pdftext.person", line, g, n)
                        continue
                # organisations: legal form / institution word, repeated on several drawings
                if (n >= 2 or docs <= 3) and 2 <= len(words) <= 7:
                    if _LEGAL.search(line.strip()) or _INSTITUTION.search(line):
                        nm = org_name(line, L)
                        if nm and not L.is_vendor(pseudo.tokens_of(nm)):
                            emit("pdftext.org", nm, g, n)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for (label, value, g), n in sorted(out.items(), key=lambda kv: (kv[0][0], -kv[1])):
            fh.write(json.dumps({"l": label, "v": value, "c": n, "g": [g], "x": g}, ensure_ascii=False) + "\n")
    by = collections.Counter(k[0] for k in out)
    print("derived values:", dict(by))


if __name__ == "__main__":
    main(sys.argv[1:])
