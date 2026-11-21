"""Pass 2 - build the identity dictionary from the harvest.

Reads harvest/values.jsonl, turns identity-field values into dictionary
entries (token sequences with a class and a tier), assigns collision-free
pseudonyms and writes:

  dict.json    entries + pseudonym table        (private - never submitted)
  review.tsv   human-readable listing for audit (private)

Tiers
  A  distinctive: replaced in any eligible text, corpus-wide
  L  plausible name but short / made of ordinary words: replaced in free text
     only inside the source groups where it was seen as an identity; elsewhere
     only inside identity fields
  B  ambiguous: replaced only inside identity fields

Usage: python -I build_dict.py [--harvest DIR] [--out DIR]
"""
import collections
import html
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deid import util, pseudo  # noqa: E402
from deid.tlds import TLDS  # noqa: E402


def _load_lines(name):
    out = []
    with open(os.path.join(util.DATA_DIR, name), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def _load_words(name):
    out = set()
    for line in _load_lines(name):
        out.update(w.lower() for w in line.split())
    return out


class Lists:
    def __init__(self):
        gen = _load_lines("generic_values.txt")
        sw = _load_lines("software.txt")
        self.generic = set(pseudo.canon(x) for x in gen)
        self.software = set(pseudo.canon(x) for x in sw)
        self.generic_tok = set(t for x in gen + sw for t in pseudo.tokens_of(x))
        self.struct = _load_words("structural_words.txt")
        self.eng = _load_words("engineering_terms.txt")
        self.calendar = _load_words("calendar_words.txt")
        self.words = set(_load_lines("words_en.txt")) | self.calendar
        self.proper = set(_load_lines("words_proper.txt"))
        self.safe_domains = _load_words("safe_domains.txt")
        vend = _load_lines("vendors.txt")
        self.vendors = set(pseudo.canon(x) for x in vend)
        self.vendor_tok = set(t for x in vend for t in pseudo.tokens_of(x) if len(t) >= 3)

    def is_vendor(self, tokens):
        toks = [t for t in tokens if not t.isdigit()]
        if not toks:
            return False
        if "".join(toks) in self.vendors:
            return True
        hit = [t for t in toks if t in self.vendor_tok]
        rest = [t for t in toks if t not in self.vendor_tok]
        return bool(hit) and all((t in self.struct or t in self.words or len(t) < 3) for t in rest)

    def is_generic(self, tokens):
        toks = [t for t in tokens if not t.isdigit() and len(t) > 1]
        if not toks:
            return True
        j = "".join(toks)
        if j in self.generic or j in self.software:
            return True
        if len(toks) == 1:
            base = toks[0].rstrip("0123456789")
            if base and (base in self.generic or base in self.software):
                return True
        if all((t in self.generic_tok or t in self.struct or t in self.eng or t in self.calendar)
               for t in toks):
            return True
        return False

    def common(self, t):
        return t in self.words or t in self.eng or t in self.struct


_VERSIONISH = re.compile(r"^(v?\d+(\.\d+)*|sp\d+|x64|x86|r\d+|20\d\d)$")
_RE_DOMUSER = re.compile(r"^\s*([^\\/@\s()]{1,40})\\+([^\\/()]{1,60}?)\s*(?:\(([^)]{1,60})\)?)?\s*$")
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_RE_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\"']+")
_RE_PAREN = re.compile(r"\(([^)]{1,80})\)")
_RE_SPLIT = re.compile(r"\s*(?:[;|/&\n\r\t]|,|\band\b|\bund\b|\bet\b|\by\b)\s*", re.I)
def _foreign():
    p = os.path.join(util.DATA_DIR, "words_foreign.txt")
    if not os.path.exists(p):
        return frozenset()
    with open(p, encoding="utf-8") as fh:
        return frozenset(w.strip() for w in fh if w.strip())


FOREIGN_WORDS = _foreign()


def _large():
    p = os.path.join(util.DATA_DIR, "words_en_large.txt")
    if not os.path.exists(p):
        return frozenset()
    with open(p, encoding="utf-8") as fh:
        return frozenset(w.strip() for w in fh if w.strip())


LARGE_WORDS = _large()


def _repo_exclude():
    p = os.path.join(util.DATA_DIR, "repo_exclude.txt")
    out = set()
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if not line.startswith("#"):
                    out.update(line.split())
    return frozenset(out)


REPO_EXCLUDE = _repo_exclude()


def ordinary_word(tok, L):
    """True when a token is ordinary vocabulary in a broad sense: a dictionary
    word (large English list, several other languages), its plural, or such
    a word plus digits. Used to keep descriptive repository names out of the
    dictionary."""
    def known(w):
        return len(w) >= 3 and (w in LARGE_WORDS or w in FOREIGN_WORDS or L.common(w) or w in L.generic_tok
                                or w in L.software)

    base = tok.strip("0123456789")
    if not base:
        return True
    for cand in {base, base.rstrip("s"), tok}:
        if known(cand):
            return True
    return False
_SHORT_FOLDERS = frozenset("""docume escrit deskto mydocu misdoc mesdoc eigene progra downlo descar telech
onedri dropbo google nextcl appdat micros window bureau schrei pictur imagen bilder musica videos creati
proyec projec projet projek trabaj travai arbeit materi univer escuel school classe curso course clases
solidw invent autode fusion catia freeca kicad github gitlab bitbuc reposi source develo workspa archiv
backup copias nuevac neuero newfol nouvea carpet ordner dossie""".split())
_RE_DOTTED = re.compile(r"^[A-Za-z0-9-]{2,}(?:\.[A-Za-z0-9-]{2,})+$")
_RE_SHORT = re.compile(r"^([A-Za-z0-9_$-]{1,6})~\d(\..{1,3})?$")
_SECOND_LEVEL = {"co", "com", "ac", "org", "net", "edu", "gov", "or", "ne", "go"}

_PERSON_LABELS = {
    "step.author", "step.authorization", "step.person.last", "step.person.first", "step.person.middle",
    "pdf.info.author", "pdf.xmp.dc_creator", "pdf.xmp.xmp_Author", "pdf.xmp.pdf_Author",
    "pdf.xmp.xmpRights_Owner", "pdf.xmp.photoshop_Credit", "pdf.info.manager",
    "ole.si.author", "ole.si.lastauthor", "ole.dsi.manager", "ole.invdtc.lastsavedby", "ole.seext.fullname",
    "ole.inv.designer", "ole.inv.engineer", "ole.inv.checkedby", "ole.inv.engapprovedby",
    "ole.inv.mfgapprovedby", "ole.inv.3", "ole.inv.34",
    "fcstd.createdby", "fcstd.lastmodifiedby", "dxf.lastsavedby", "iges.author",
    "sw.core.creator", "sw.core.lastmodifiedby", "svg.dc_creator", "svg.cc_Agent", "svg.dc_contributor",
    "creo.user",
    "ps.for", "ps.author", "ps.routing", "img.exif.artist", "img.info.author", "img.info.artist",
    "img.exif.xpauthor", "xt.user", "3mf.designer", "3mf.author", "img.xmp.dc_creator",
}
_ORG_LABELS = {
    "step.org", "step.org.name", "ole.dsi.company", "fcstd.company", "iges.org", "sw.app.company",
    "sw.app.manager", "xt.site", "svg.dc_publisher", "pdf.xmp.dc_publisher", "pdf.xmp.pdfx_Company",
}
_ACCOUNT_LABELS = {"userpath.user", "email.local"}
# custom property names (several languages) whose value names a person / an organisation
_NAME_PERSON = re.compile(
    r"(?i)(saved\s*by|enregistrement par|guardado por|gespeichert von|salvato da|"
    r"\bauth?or|\bauteur|\bautore|user\s*name|\bbenutzer|\busuario|"
    r"drawn|draw\s*by|dessin[eé]|gezeichnet|dibujad|disegnat|dissenyat|"
    r"designed|designer|dise[nñ]ad|konstrukteur|projetad|"
    r"check(ed)?\s*by|\bchecked\b|v[eé]rifi|gepr[uü]ft|revisad|comprobad|verificat|"
    r"approved|appro(u)?v[eé]|freigegeben|aprobad|aprovat|"
    r"\bengineer|ing[eé]nieur|ingenier|created\s*by|\bcreator\b|erstellt von|creado por|"
    r"\bresponsible|\bowner\b|\bcontact\b|modell?ed\s*by)")
_NAME_ORG = re.compile(
    r"(?i)(\bcompany|\bazienda|\bfirma\b|\bempresa|soci[eé]t[eé]|entreprise|\bcustomer|\bclient\b|"
    r"\bkunde\b|organi[sz]ation)")
_NAME_NOT = re.compile(
    r"(?i)(date|datum|fecha|\bdata\b|\btime\b|\bnum|number|\bno\b|\bcode\b|\brev|status|stamp|"
    r"\bid\b|version|tool|software|system|applic)")


def name_class(name):
    """Class implied by a (custom) property name, or None."""
    if _NAME_NOT.search(name):
        return None
    if _NAME_PERSON.search(name):
        return "PERSON"
    if _NAME_ORG.search(name):
        return "ORG"
    return None


def label_class(label):
    if label in _PERSON_LABELS:
        return "PERSON"
    if label in _ORG_LABELS:
        return "ORG"
    if label in _ACCOUNT_LABELS:
        return "ACCOUNT"
    if label == "email.domain":
        return "DOMAIN"
    if label.startswith(("ole.ud.", "sw.prop.", "pdf.info.")):
        name = label.split(".", 2)[2]
        if name.lower() in ("title", "subject", "keywords", "creator", "producer", "comments", "author"):
            return "PERSON" if name.lower() == "author" else None
        return name_class(name)
    return None


def _maybe_gbk(v):
    """Undo cp1252 mojibake of GBK text (two-byte runs of high characters)."""
    if v.isascii():
        return v
    try:
        raw = v.encode("cp1252")
    except UnicodeEncodeError:
        return v
    runs = re.findall(rb"[\x80-\xff]+", raw)
    if runs and all(len(r) % 2 == 0 for r in runs):
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            return v
    return v


def registrable(host):
    labels = host.lower().strip(".").split(".")
    if len(labels) < 2 or labels[-1] not in TLDS:
        return None, None
    n = 3 if (len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL) else 2
    return ".".join(labels[-n:]), n - 1


class Builder:
    def __init__(self, demote=None):
        self.L = Lists()
        self.cand = {}   # tokens tuple -> dict
        self.demote = demote or {}   # tokens tuple -> forced tier (from the census)
        self.surname_alias = {}      # surname token -> account token it was cut from

    @staticmethod
    def _plausible(tok):
        """Reject tokens that are decoding garbage (characters from scripts
        that do not occur together in real names)."""
        if tok.isascii():
            return True
        scripts = set()
        for ch in tok:
            o = ord(ch)
            if o < 0x250:
                scripts.add("latin")
            elif 0x370 <= o < 0x400:
                scripts.add("greek")
            elif 0x400 <= o < 0x530:
                scripts.add("cyrillic")
            elif 0x3040 <= o < 0x3100 or 0x4E00 <= o < 0xA000:
                scripts.add("cjk")
            elif 0xAC00 <= o < 0xD7B0:
                scripts.add("hangul")
            elif 0x590 <= o < 0x700:
                scripts.add("semitic")
            elif 0xE00 <= o < 0xE80:
                scripts.add("thai")
            else:
                return False
        return len(scripts - {"latin"}) <= 1

    def add(self, tokens, cls, surface, label, count, groups, keep=None, exact=False):
        tokens = tuple(tokens)
        if not tokens or self.L.is_generic(tokens):
            return
        if not all(self._plausible(t) for t in tokens):
            return
        if all(len(set(t)) <= 2 for t in tokens if not t.isdigit()) and not any(t.isdigit() for t in tokens):
            return      # "iiiiiiii", "xxxx": filler, not a name
        c = self.cand.get(tokens)
        if c is None:
            c = self.cand[tokens] = {"cls": collections.Counter(), "o": collections.Counter(),
                                     "labels": collections.Counter(), "n": 0, "g": set(), "keep": keep,
                                     "exact": exact}
        c["cls"][cls] += count
        if surface:
            c["o"][surface] += count
        c["labels"][label] += count
        c["n"] += count
        c["g"].update(groups)
        if not exact:
            c["exact"] = False

    def add_dotted(self, surface, cls, label, count, groups):
        """A dotted, space-free name used as an organisation / author
        ("cad.parts", "acme.io"): a site or project name. Kept as an exact
        phrase even when its parts are ordinary words."""
        tokens = pseudo.tokens_of(surface)
        if len(tokens) < 2 or sum(len(t) for t in tokens) < 6:
            return False
        if "".join(tokens) in self.L.generic or "".join(tokens) in self.L.software:
            return False
        reg, _n = registrable(surface)
        if reg and (reg in self.L.safe_domains or surface.lower() in self.L.safe_domains):
            return False
        c = self.cand.setdefault(tuple(tokens), {"cls": collections.Counter(), "o": collections.Counter(),
                                                 "labels": collections.Counter(), "n": 0, "g": set(),
                                                 "keep": [0] * len(tokens), "exact": True})
        c["cls"][cls] += count
        c["o"][surface] += count
        c["labels"][label] += count
        c["n"] += count
        c["g"].update(groups)
        return True

    def add_phrase(self, surface, cls, label, count, groups, singles=True):
        surface = surface.strip(" \t\"'.,;:-_")
        tokens = pseudo.tokens_of(surface)
        if (cls in ("ORG", "PERSON") and tokens and self.L.is_generic(tokens) and _RE_DOTTED.match(surface)
                and all(t.isalpha() for t in tokens)):
            # made of ordinary words, but written as a site / project name
            self.add_dotted(surface, cls, label, count, groups)
            return
        if not tokens or len(tokens) > 8:
            return
        while tokens and _VERSIONISH.match(tokens[-1]) and len(tokens) > 1:
            tokens = tokens[:-1]
            surface = None
        if self.L.is_generic(tokens):
            return
        self.add(tokens, cls, surface, label, count, groups)
        if singles and len(tokens) > 1:
            for t in tokens:
                if len(t) < 2 or t.isdigit() or t in self.L.struct or t in self.L.generic_tok:
                    continue
                if cls not in ("PERSON", "ACCOUNT") and (
                        self.L.common(t) or len(t) < 4 or t in self.L.proper or ordinary_word(t, self.L)):
                    continue        # organisation names: only their distinctive words stand alone
                self.add((t,), cls, None, label, count, groups)

    def add_domain(self, host, label, count, groups):
        reg, nsuffix = registrable(host)
        if not reg or reg in self.L.safe_domains or host.lower() in self.L.safe_domains:
            return
        labels = reg.split(".")
        main = labels[0]
        if self.L.is_vendor(pseudo.tokens_of(main)):
            return          # catalogue vendor: data-sheet links are engineering data
        tokens = pseudo.tokens_of(reg)
        ntail = len(pseudo.tokens_of(".".join(labels[1:])))
        keep = [0] * (len(tokens) - ntail) + [1] * ntail
        if self.L.is_generic(tokens[:len(tokens) - ntail]):
            return
        self.add(tokens, "DOMAIN", reg, label, count, groups, keep=keep, exact=True)
        self.add_phrase(main, "DOMAIN", label, count, groups)

    def add_person_value(self, v, label, count, groups):
        v = html.unescape(v).strip().strip("\"'")
        if not v:
            return
        m = _RE_DOMUSER.match(v)
        if m and "\\" in v:
            dom, user, host = m.group(1), m.group(2), m.group(3)
            self.add_phrase(dom, "HOST", label, count, groups, singles=False)
            self.add_account(user, label, count, groups)
            if host:
                self.add_phrase(host, "HOST", label, count, groups, singles=False)
            return
        for em in _RE_EMAIL.findall(v):
            local, dom = em.split("@", 1)
            self.add_account(local, label, count, groups)
            self.add_domain(dom, label, count, groups)
        v = _RE_EMAIL.sub(" ", v)
        for u in _RE_URL.findall(v):
            host = re.sub(r"^https?://", "", u).split("/")[0]
            self.add_domain(host, label, count, groups)
            mm = re.match(r"(?:https?://)?(?:www\.)?(?:github|gitlab)\.com/([^/\s]+)", u)
            if mm:
                self.add_account(mm.group(1), label, count, groups)
        v = _RE_URL.sub(" ", v)
        for par in _RE_PAREN.findall(v):
            if "." in par and registrable(par)[0]:
                self.add_domain(par, label, count, groups)
            else:
                self.add_phrase(par, "PERSON", label, count, groups)
        v = _RE_PAREN.sub(" ", v)
        for part in _RE_SPLIT.split(v):
            part = part.strip()
            if part:
                self.add_phrase(part, "PERSON", label, count, groups)

    def add_account(self, v, label, count, groups):
        v = v.strip()
        if v.isdigit():
            # employee / user numbers used as account names
            if 4 <= len(v) <= 12:
                c = self.cand.setdefault((v,), {"cls": collections.Counter(), "o": collections.Counter(),
                                                "labels": collections.Counter(), "n": 0, "g": set(),
                                                "keep": None, "exact": False})
                c["cls"]["ACCOUNT"] += count
                c["labels"][label] += count
                c["n"] += count
                c["g"].update(groups)
            return
        m = _RE_SHORT.match(v)
        if m:
            prefix = m.group(1).lower()
            if len(prefix) >= 4 and any(g.startswith(prefix) for g in self.L.generic):
                return
            if prefix in _SHORT_FOLDERS:
                return          # 8.3 name of an ordinary folder, not of a profile
            tokens = pseudo.tokens_of(v)
            keep = [0] + [1 if t.isdigit() else 0 for t in tokens[1:]]
            self.add(tokens, "ACCOUNT", v, label, count, groups, keep=keep, exact=True)
            return
        self.add_phrase(v, "ACCOUNT", label, count, groups)

    # ------------------------------------------------------------------
    def tier(self, tokens, c):
        t0 = self._tier(tokens, c)
        if c.get("cap") and t0 == "A" and all(l == "path.repo" for l in c["labels"]):
            t0 = c["cap"]
        # identities known only from visible text stay scoped to their own group
        if t0 == "A" and c["labels"] and all(l.startswith("pdftext.") for l in c["labels"]):
            t0 = "L"
        if c.get("force") and t0 in (None, "B"):
            return c["force"]
        if c.get("force_min") and t0 in (None, "B"):
            return c["force_min"]
        return t0

    def _tier(self, tokens, c):
        L = self.L
        if tuple(tokens) in self.demote:
            return self.demote[tuple(tokens)]
        if L.is_vendor(tokens) and not c.get("exact"):
            return "B"
        if len(tokens) == 1:
            t = tokens[0]
            if t.isdigit():
                return "B" if len(t) >= 4 else None
            if len(t) < 2 or not any(ch.isalpha() for ch in t):
                return None
            if not t.isascii():
                return "A" if len(t) >= 2 else "B"
            if L.common(t) or len(t) < 4:
                return "B"
            if t in L.proper:
                return "L"
            mixed = any(ch.isdigit() for ch in t)
            ngroups = len(c["g"])
            # corpus-wide replacement needs a long token, or one seen as an
            # identity in several independent source groups
            if len(t) >= 8 or (len(t) >= 6 and (mixed or ngroups >= 2)):
                return "A"
            return "L"
        total = sum(len(t) for t in tokens)
        if total < 5:
            return "B"
        if c.get("exact"):
            return "A"
        distinctive = [t for t in tokens if not L.common(t) and len(t) >= 3 and not t.isdigit()]
        if distinctive:
            return "A"
        return "L"

    def finish(self, key):
        entries = []
        for tokens, c in self.cand.items():
            tier = self.tier(tokens, c)
            if tier is None:
                continue
            keep = c["keep"]
            if keep is None:
                keep = [1 if (len(tokens) > 1 and t in self.L.struct) else 0 for t in tokens]
                if all(keep):
                    keep = [0] * len(tokens)
            exact = bool(c.get("exact"))
            if len(tokens) > 1 and tier == "L":
                exact = True
            if c.get("alias_of"):
                self.surname_alias[tokens[0]] = c["alias_of"]
            entries.append({"t": list(tokens), "k": keep, "o": [s for s, _n in c["o"].most_common(12)],
                            "tier": tier, "cls": c["cls"].most_common(1)[0][0], "x": 1 if exact else 0,
                            "n": c["n"], "g": sorted(c["g"]),
                            "labels": [l for l, _n in c["labels"].most_common(4)]})
        entries.sort(key=lambda e: (e["t"]))
        toks = set()
        for e in entries:
            for t, k in zip(e["t"], e["k"]):
                if not k:
                    toks.add(t)
        pz = pseudo.Pseudonymizer(key)
        table = pz.build_table(toks, reserved=self.L.words | self.L.eng)
        self.resolve_aliases(entries, table)
        return entries, table

    def resolve_aliases(self, entries, table):
        """Give alias spellings of one identity the same pseudonym letters.

        * "janedoe" and "Jane Doe": a single token that is the concatenation
          of a multi-word identity gets the concatenation of that identity's
          word pseudonyms.
        * "jdoe" and "John Doe" (same source project): an account made of
          a first initial plus a surname (or a first name plus a surname
          initial) is composed from the person's word pseudonyms.
        A composed value is used only if no other token already maps to it."""
        used = set(table.values())
        single = {e["t"][0]: e for e in entries if len(e["t"]) == 1}

        def ps(tok, keep):
            return tok if keep else table.get(tok, tok)

        def assign(tok, value):
            if tok not in table or value == table[tok] or value in used or value == tok:
                return
            if len(value) != len(tok):
                return
            used.discard(table[tok])
            table[tok] = value
            used.add(value)

        # surname cut from an account name: reuse the matching part of the
        # account's pseudonym (account "smithj" -> "kabide", surname "Smith" -> "Kabid")
        for sur, account in self.surname_alias.items():
            if sur in table and account in table and len(account) == len(sur) + 1:
                pa = table[account]
                assign(sur, pa[1:] if account[1:] == sur else pa[:-1])
        multi = [e for e in entries if len(e["t"]) > 1]
        for e in multi:
            if e.get("x") and e["cls"] == "DOMAIN":
                continue
            sq = "".join(e["t"])
            if sq in single:
                assign(sq, "".join(ps(t, k) for t, k in zip(e["t"], e["k"])))
        # initial + surname accounts
        people = collections.defaultdict(list)
        for e in multi:
            if e["cls"] == "PERSON" and 2 <= len(e["t"]) <= 4 and all(t.isalpha() for t in e["t"]):
                for g in e["g"]:
                    people[g].append(e)
        for tok, se in single.items():
            if se["cls"] not in ("ACCOUNT", "PERSON") or len(tok) < 4 or not tok.isalpha() or tok not in table:
                continue
            cands = {}
            for g in se["g"]:
                for e in people.get(g, ()):
                    f, l = e["t"][0], e["t"][-1]
                    if len(f) < 2 or len(l) < 2:
                        continue
                    pf, pl = ps(f, e["k"][0]), ps(l, e["k"][-1])
                    for pat, val in ((f[0] + l, pf[0] + pl), (f + l[0], pf + pl[0]), (l + f[0], pl + pf[0]),
                                     (f[0] + l[:len(tok) - 1], pf[0] + pl[:len(tok) - 1])):
                        if pat == tok and len(val) == len(tok):
                            cands[val] = 1
            if len(cands) == 1:
                assign(tok, next(iter(cands)))


def path_candidates(files_jsonl):
    """Owner names and web domains carried by top-level corpus directories."""
    seen = {}
    with open(files_jsonl, encoding="utf-8") as fh:
        for line in fh:
            p = json.loads(line)["p"].split("!/", 1)[0]
            parts = p.split("/")
            if len(parts) < 4 or parts[0] != "sources":
                continue
            seen.setdefault((parts[1], parts[2]), util.group_of(p))
    return seen


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--harvest", default=os.path.join(util.WORK, "harvest"))
    ap.add_argument("--out", default=os.path.join(util.WORK, "dict"))
    ap.add_argument("--key", default=os.path.join(util.WORK, "secret.key"))
    ap.add_argument("--demote", default=None, help="census output (JSON list of [tokens, tier])")
    ap.add_argument("--no-curated", action="store_true", help="ignore data/curated_identities.tsv")
    ap.add_argument("--no-text", action="store_true", help="ignore harvest/values_text.jsonl")
    args = ap.parse_args(argv)
    demote = {}
    dpath = args.demote or os.path.join(args.out, "demote.json")
    if os.path.exists(dpath):
        with open(dpath, encoding="utf-8") as fh:
            demote = {tuple(t): tier for t, tier in json.load(fh)}
    os.makedirs(args.out, exist_ok=True)
    if not os.path.exists(args.key):
        with open(args.key, "wb") as fh:
            fh.write(os.urandom(32))
        os.chmod(args.key, 0o600)
    key = open(args.key, "rb").read()

    B = Builder(demote)
    nvals = 0
    import glob
    vfiles = [os.path.join(args.harvest, "values.jsonl")] + sorted(
        f for f in glob.glob(os.path.join(args.harvest, "values_*.jsonl"))
        if os.path.basename(f) != "values_text.jsonl")
    import itertools

    def _lines():
        for vf in vfiles:
            with open(vf, encoding="utf-8") as fh:
                for line in fh:
                    yield line

    with open(os.devnull) as _unused:
        for line in _lines():
            d = json.loads(line)
            label, v, cnt, groups = d["l"], _maybe_gbk(d["v"]), d["c"], d["g"]
            cls = label_class(label)
            if cls is None:
                continue
            nvals += 1
            if cls == "PERSON":
                B.add_person_value(v, label, cnt, groups)
            elif cls == "ORG":
                for em in _RE_EMAIL.findall(v):
                    B.add_domain(em.split("@", 1)[1], label, cnt, groups)
                vv = _RE_URL.sub(" ", _RE_EMAIL.sub(" ", html.unescape(v)))
                if registrable(vv.strip())[0]:
                    B.add_domain(vv.strip(), label, cnt, groups)
                else:
                    for part in re.split(r"\s*[;|\n\r\t]\s*", vv):
                        B.add_phrase(part, "ORG", label, cnt, groups)
            elif cls == "ACCOUNT":
                B.add_account(v, label, cnt, groups)
            elif cls == "DOMAIN":
                B.add_domain(v, label, cnt, groups)
    # identities derived from visible drawing text (textharvest.py), if present
    tpath = os.path.join(args.harvest, "values_text.jsonl")
    if os.path.exists(tpath) and not args.no_text:
        with open(tpath, encoding="utf-8") as fh:
            for line in fh:
                d = json.loads(line)
                label, v, cnt, groups = d["l"], d["v"], d["c"], d["g"]
                if label == "pdftext.person":
                    B.add_person_value(v, label, cnt, groups)
                elif label == "pdftext.org":
                    B.add_phrase(v, "ORG", label, cnt, groups)
                elif label == "pdftext.surname" and "|" in v:
                    sur, account = v.split("|", 1)
                    before = (sur,) in B.cand
                    B.add((sur,), "PERSON", None, label, cnt, groups)
                    if (sur,) in B.cand:
                        B.cand[(sur,)].setdefault("alias_of", account)
                        if not before:
                            B.cand[(sur,)]["force"] = "L"
                elif label == "pdftext.email" and "@" in v:
                    local, dom = v.split("@", 1)
                    B.add_account(local, label, cnt, groups)
                    B.add_domain(dom, label, cnt, groups)
    repo_groups = collections.defaultdict(list)
    cands = path_candidates(os.path.join(args.harvest, "files.jsonl"))
    for (src, top), grp in cands.items():
        if src in ("github", "gitlab"):
            parts = [p for p in top.split("__") if p]
            if src == "gitlab" and parts and parts[0] == "gitlab.com":
                parts = parts[1:]
            if len(parts) >= 2:
                repo_groups[parts[1]].append(grp)
    for (src, top), grp in cands.items():
        if src in ("github", "gitlab"):
            parts = top.split("__")
            if src == "gitlab" and parts and parts[0] == "gitlab.com":
                parts = parts[1:]
            if parts:
                B.add_account(parts[0], "path.owner", 1, [grp])
            # distinctive words of the repository name are project names of this group
            rp = [p for p in parts[1:] if p]
            if rp and not args.no_curated:
                repo = rp[0]
                for tok in pseudo.tokens_of(repo):
                    if (len(tok) >= 5 and not tok.isdigit() and not B.L.common(tok) and tok not in B.L.proper
                            and tok not in B.L.generic_tok and not B.L.is_vendor([tok])
                            and not any(v in tok for v in B.L.vendor_tok if len(v) >= 5)
                            and tok not in B.L.software and not ordinary_word(tok, B.L)
                            and tok not in REPO_EXCLUDE):
                        before = (tok,) in B.cand
                        B.add((tok,), "PROJECT", None, "path.repo", 1, repo_groups.get(repo, [grp]))
                        if (tok,) in B.cand and not before:
                            B.cand[(tok,)]["force"] = "L"
                            B.cand[(tok,)]["cap"] = "L"
        elif src == "web":
            B.add_domain(top, "path.domain", 1, [grp])
    # curated identities (folder names / visible text), scoped by path prefix
    cur = os.path.join(util.DATA_DIR, "curated_identities.tsv")
    if os.path.exists(cur) and not args.no_curated:
        all_groups = sorted(set(path_candidates(os.path.join(args.harvest, "files.jsonl")).values()))
        with open(os.path.join(args.harvest, "files.jsonl"), encoding="utf-8") as fh:
            all_groups = sorted(set(all_groups) | set(util.group_of(json.loads(l)["p"].split("!/", 1)[0]) for l in fh))
        with open(cur, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#") or line.count("\t") < 2:
                    continue
                scope, cls, surface = line.split("\t", 2)
                groups = [g for g in all_groups if g.startswith(scope) or (g + "/").startswith(scope)]
                if not groups:
                    continue
                before = set(B.cand)
                if cls == "DOMAIN":
                    B.add_domain(surface, "curated", 1, groups)
                else:
                    B.add_phrase(surface, "PERSON" if cls == "PERSON" else "ORG", "curated", 1, groups,
                                 singles=(cls == "PERSON"))
                # a curated name is replaced inside its scope even when short
                for tokens in set(B.cand) - before:
                    B.cand[tokens]["force"] = "L"
                toks = tuple(pseudo.tokens_of(surface))
                if toks in B.cand and len(toks) == 1 and "force" not in B.cand[toks]:
                    B.cand[toks]["force_min"] = "L"
    entries, table = B.finish(key)
    with open(os.path.join(args.out, "dict.json"), "w", encoding="utf-8") as fh:
        json.dump({"entries": entries, "table": table}, fh, ensure_ascii=False)
    with open(os.path.join(args.out, "review.tsv"), "w", encoding="utf-8") as fh:
        fh.write("tier\tcls\tn\tgroups\ttokens\tpseudonym\tsurfaces\tlabels\n")
        for e in sorted(entries, key=lambda e: (e["tier"], e["cls"], -e["n"])):
            ps = " ".join(t if k else table[t] for t, k in zip(e["t"], e["k"]))
            fh.write("%s\t%s\t%d\t%d\t%s\t%s\t%s\t%s\n" % (
                e["tier"], e["cls"], e["n"], len(e["g"]), " ".join(e["t"]), ps,
                " | ".join(e["o"][:4]), ",".join(e["labels"])))
    tiers = collections.Counter((e["tier"], e["cls"]) for e in entries)
    print("values used: %d; entries: %d; tokens: %d" % (nvals, len(entries), len(table)))
    print(sorted(tiers.items()))


if __name__ == "__main__":
    main(sys.argv[1:])
