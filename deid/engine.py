"""Dictionary matching and same-length replacement.

The engine holds the identity dictionary as an Aho-Corasick automaton over a
normalised view of the data (lower case, accents folded). One automaton serves

  * byte views  - a file or stream decoded as latin-1 after a 256-entry
                  normalising translate; UTF-8 and UTF-16LE spellings of every
                  entry are extra keys, so one pass finds all encodings;
  * text views  - real Python strings (PDF metadata, XML values ...).

Every replacement has exactly the length of what it replaces, in characters
and in encoded bytes, so nothing else in a file ever moves.

Tiers:  A - distinctive; replaced anywhere text is eligible.
        B - ambiguous (short or an ordinary word); replaced only inside
            identity fields (ctx "id").
"""
import bisect
import json

import ahocorasick

from . import pseudo

JOINERS = (" ", "", "-", "_", ".")


def _byte_table():
    t = bytearray(range(256))
    for b in range(256):
        c = pseudo.canon_char(chr(b))
        if len(c) == 1 and ord(c) < 128:
            t[b] = ord(c)
    return bytes(t)


BYTE_TABLE = _byte_table()


class _TextTable(dict):
    def __missing__(self, o):
        c = pseudo.canon_char(chr(o))
        v = ord(c) if len(c) == 1 else o
        self[o] = v
        return v


_TEXT_TABLE = _TextTable()


def norm_bytes(raw):
    return raw.translate(BYTE_TABLE).decode("latin-1")


def norm_text(s):
    if s.isascii():
        return s.lower()
    return s.translate(_TEXT_TABLE)


class Entry:
    __slots__ = ("eid", "tokens", "keep", "tier", "cls", "pseudo", "mask", "nchar", "groups")


class Engine:
    def __init__(self, dictionary, key, protect=None):
        """dictionary: dict with 'entries' (list of {t, k, o, tier, cls}) and
        optional 'table' (token -> pseudonym)."""
        if isinstance(dictionary, str):
            with open(dictionary, encoding="utf-8") as fh:
                dictionary = json.load(fh)
        self.pz = pseudo.Pseudonymizer(key, dictionary.get("table"))
        self.group = None      # source group of the file being processed
        self.stats = {}        # eid -> replacements (reset per file by callers)
        self.entries = []
        self.A = ahocorasick.Automaton()
        keys = {}
        for d in dictionary["entries"]:
            e = Entry()
            e.eid = len(self.entries)
            e.tokens = d["t"]
            e.keep = d.get("k") or [0] * len(e.tokens)
            e.tier = d.get("tier", "A")
            e.cls = d.get("cls", "")
            ps = []
            mask = []
            for tok, keep in zip(e.tokens, e.keep):
                ps.append(tok if keep else self.pz.token(tok))
                mask.append(("1" if keep else "0") * len(tok))
            e.pseudo = "".join(ps)
            e.mask = "".join(mask)
            e.nchar = len(e.pseudo)
            self.entries.append(e)
            e.groups = frozenset(d.get("g") or ()) if e.tier == "L" else None
            variants = set()
            toks = e.tokens
            exact = bool(d.get("x"))
            if len(toks) == 1:
                joiners = ("",)
            else:
                joiners = () if exact else JOINERS
            surfaces = set(j.join(toks) for j in joiners)
            for o in d.get("o") or ():
                if pseudo.tokens_of(o) != toks:
                    continue
                surfaces.add(o)
                if not o.isascii():
                    for form in (o, o.lower(), o.upper(), o.title()):
                        if pseudo.tokens_of(form) != toks:
                            continue
                        variants.add((norm_bytes(form.encode("utf-8")), "u8"))
                        try:
                            variants.add((norm_bytes(form.encode("gbk")), "gbk"))
                        except UnicodeEncodeError:
                            pass
            for sfc in surfaces:
                variants.add((norm_text(sfc), "x"))
                variants.add((norm_bytes(sfc.encode("utf-16-le")), "u16"))
            for k, enc in variants:
                if len(k) < 2:
                    continue
                old = keys.get(k)
                # prefer the plain variant, then the higher tier
                if old is None or (enc == "x" and old[1] != "x") or \
                        (old[1] == enc and e.tier == "A" and self.entries[old[0]].tier != "A"):
                    keys[k] = (e.eid, enc, len(k))
        # protected file names (tier "P"): matched like entries but never replaced
        if protect:
            if isinstance(protect, str):
                with open(protect, encoding="utf-8") as fh:
                    protect = json.load(fh)
            for stem in protect:
                toks = pseudo.tokens_of(stem)
                if not toks:
                    continue
                e = Entry()
                e.eid = len(self.entries)
                e.tokens = toks
                e.keep = [1] * len(toks)
                e.tier = "P"
                e.cls = "FILE"
                e.pseudo = "".join(toks)
                e.mask = "1" * len(e.pseudo)
                e.nchar = len(e.pseudo)
                e.groups = None
                self.entries.append(e)
                for k, enc in ((norm_text(stem), "x"), (norm_bytes(stem.encode("utf-16-le")), "u16")):
                    if len(k) >= 3 and k not in keys:
                        keys[k] = (e.eid, enc, len(k))
        # Two automata: A holds every key and is used inside identity fields;
        # F holds only keys that may be replaced in free text (tiers A, L and
        # protected names) and is the one run over whole files. Keeping the
        # short, ambiguous tier-B keys out of F avoids millions of useless
        # candidate matches in large geometry files.
        # F8 is F without the UTF-16 spellings; it is used for text files,
        # where UTF-16 keys cannot occur and only slow the scan down.
        self.F = ahocorasick.Automaton()
        self.F8 = ahocorasick.Automaton()
        nfree = nfree8 = 0
        for k, v in keys.items():
            self.A.add_word(k, v)
            if self.entries[v[0]].tier != "B":
                self.F.add_word(k, v)
                nfree += 1
                if v[1] != "u16":
                    self.F8.add_word(k, v)
                    nfree8 += 1
        self.nkeys = len(keys)
        self.nfree = nfree
        if keys:
            self.A.make_automaton()
        if nfree:
            self.F.make_automaton()
        if nfree8:
            self.F8.make_automaton()
        else:
            self.F8 = self.F

    # ------------------------------------------------------------------
    @staticmethod
    def _camel(orig, i):
        """True when a word boundary by case change lies between i-1 and i."""
        a, b = orig[i - 1], orig[i]
        if a.islower() and b.isupper():
            return True
        if a.isupper() and b.isupper() and i + 1 < len(orig) and orig[i + 1].islower():
            return True
        if a.isalpha() != b.isalpha() and a.isalnum() and b.isalnum():
            return False
        return False

    @staticmethod
    def _camel16(orig, i, n):
        """_camel for UTF-16LE byte views (ASCII code units only)."""
        a, b = orig[i - 2], orig[i]
        if a.islower() and b.isupper():
            return True
        if a.isupper() and b.isupper() and i + 3 < n and orig[i + 3] == "\x00" and orig[i + 2].islower():
            return True
        return False

    def _hits(self, view, orig, bytes_view, free_only=False, wide=True):
        """All boundary-valid dictionary matches: (start, end, eid, enc)."""
        out = []
        if not self.nkeys or (free_only and not self.nfree):
            return out
        n = len(view)
        ents = self.entries
        auto = self.A if not free_only else (self.F if wide else self.F8)
        for end, (eid, enc, klen) in auto.iter(view):
            e = end + 1
            s = e - klen
            if ents[eid].tier == "P":
                # a protected file name counts only when an extension follows
                if enc == "u16":
                    if not (bytes_view and view[e:e + 2] == ".\x00" and e + 2 < n and view[e + 2].isalnum()):
                        continue
                    if s >= 2 and view[s - 1] == "\x00" and view[s - 2].isalnum() and view[s - 2] < "\x80":
                        continue
                else:
                    if not (view[e:e + 1] == "." and e + 1 < n and view[e + 1].isalnum()):
                        continue
                    if s > 0 and view[s - 1].isalnum():
                        continue
                out.append((s, e, eid, enc))
                continue
            if enc == "u16":
                if not bytes_view:
                    continue
                loose = ents[eid].nchar >= 5
                if s >= 2 and view[s - 1] == "\x00" and view[s - 2].isalnum() and view[s - 2] < "\x80":
                    if not (loose and self._camel16(orig, s, n)):
                        continue
                if e + 1 < n and view[e + 1] == "\x00" and view[e].isalnum() and view[e] < "\x80":
                    if not (loose and self._camel16(orig, e, n)):
                        continue
            else:
                loose = ents[eid].nchar >= 5
                if s > 0 and view[s - 1].isalnum():
                    if not (loose and self._camel(orig, s)):
                        continue
                if e < n and view[e].isalnum():
                    if not (loose and self._camel(orig, e)):
                        continue
                if enc != "x" and not bytes_view:
                    continue
            out.append((s, e, eid, enc))
        return out

    def _render(self, ent, surface):
        """Replacement text for an entry matched as `surface` (a str)."""
        out = []
        k = 0
        ps, mask = ent.pseudo, ent.mask
        for ch in surface:
            if pseudo.canon_char(ch):
                if k >= len(ps):
                    return None
                out.append(ch if mask[k] == "1" else pseudo._shape(ch, ps[k]))
                k += 1
            else:
                out.append(ch)
        if k != len(ps):
            return None
        return "".join(out)

    @staticmethod
    def _select(hits):
        """Leftmost-longest, non-overlapping."""
        hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
        out = []
        last = -1
        for h in hits:
            if h[0] >= last:
                out.append(h)
                last = h[1]
        return out

    # ------------------------------------------------------------------
    def _free_ok(self, ent):
        """May this entry be replaced in free text of the current group?"""
        if ent.tier == "A":
            return True
        return ent.tier == "L" and self.group in ent.groups

    def spans_text(self, text, ctx="text"):
        """Dictionary matches in a real string as (start, end, replacement)."""
        if not text or not self.nkeys:
            return []
        view = norm_text(text)
        if len(view) != len(text):
            return []
        hits = [h for h in self._hits(view, text, False)
                if ctx == "id" or self.entries[h[2]].tier == "P" or self._free_ok(self.entries[h[2]])]
        out = []
        for s, e, eid, _enc in self._select(hits):
            ent = self.entries[eid]
            if ent.tier == "P" or pseudo.canon(text[s:e]) != "".join(ent.tokens):
                continue
            rep = self._render(ent, text[s:e])
            if rep is None or rep == text[s:e]:
                continue
            out.append((s, e, rep))
            self.stats[eid] = self.stats.get(eid, 0) + 1
        return out

    def replace_text(self, text, ctx="text"):
        """Replace dictionary matches in a real string. Returns (new, count)."""
        if not text or not self.nkeys:
            return text, 0
        view = norm_text(text)
        if len(view) != len(text):
            return text, 0
        hits = [h for h in self._hits(view, text, False)
                if ctx == "id" or self.entries[h[2]].tier == "P" or self._free_ok(self.entries[h[2]])]
        if not hits:
            return text, 0
        parts = []
        pos = 0
        cnt = 0
        for s, e, eid, _enc in self._select(hits):
            if self.entries[eid].tier == "P":
                continue
            if pseudo.canon(text[s:e]) != "".join(self.entries[eid].tokens):
                continue
            rep = self._render(self.entries[eid], text[s:e])
            if rep is None or rep == text[s:e]:
                continue
            parts.append(text[pos:s])
            parts.append(rep)
            pos = e
            cnt += 1
            self.stats[eid] = self.stats.get(eid, 0) + 1
        parts.append(text[pos:])
        return "".join(parts), cnt

    CHUNK = 32 << 20
    OVERLAP = 4096

    def plan_bytes(self, raw, id_spans=(), text_ok=True, lo=0, hi=None, fixed=True, wide=True):
        """Find replacements in a bytes-like object (bytes, bytearray, mmap).

        id_spans : sorted list of (start, end) spans with identity context.
        text_ok  : True, False/None, or callable(pos)->bool telling whether
                   free-text (tier A/L) replacement is allowed at a position.
        lo, hi   : restrict the scan to raw[lo:hi].
        wide     : also look for UTF-16LE spellings in free text (binary
                   streams); text files pass wide=False.
        fixed    : drop replacements whose encoded byte length differs from
                   the original (only possible for accented UTF-8 text);
                   with fixed=False they are returned and the caller must
                   cope with the shorter replacement.
        Returns a list of (start, end, replacement_bytes), non-overlapping.
        Large inputs are scanned in overlapping chunks.
        """
        if not self.nkeys:
            return []
        n = len(raw) if hi is None else hi
        hits = []
        # 1. identity spans: every tier, scanned with the full automaton
        for a0, b0 in id_spans:
            a0 = max(a0, lo)
            b0 = min(b0, n)
            if b0 <= a0:
                continue
            la = max(lo, a0 - 2)
            lb = min(n, b0 + 2)
            view = norm_bytes(bytes(raw[la:lb]))
            for s, e, eid, enc in self._hits(view, _LazyOrig(raw, la), True):
                s += la
                e += la
                if s >= a0 and e <= b0:
                    hits.append((s, e, eid, enc))
        # 2. free text: tiers A / L (and protected names) only
        if text_ok:
            pos = lo
            while pos < n:
                a = max(lo, pos - 2)
                b = min(n, pos + self.CHUNK + self.OVERLAP)
                view = norm_bytes(bytes(raw[a:b]))
                limit = pos + self.CHUNK
                for s, e, eid, enc in self._hits(view, _LazyOrig(raw, a), True, free_only=True, wide=wide):
                    s += a
                    e += a
                    if s < pos or s >= limit:
                        continue
                    ent = self.entries[eid]
                    if ent.tier != "P":
                        if not self._free_ok(ent):
                            continue
                        if text_ok is not True:
                            # predicates marked .span get the whole match
                            if getattr(text_ok, "span", False):
                                if not text_ok(s, e):
                                    continue
                            elif not text_ok(s):
                                continue
                    hits.append((s, e, eid, enc))
                pos = limit
        if not hits:
            return []
        hits = list(set(hits))
        out = []
        for s, e, eid, enc in self._select(hits):
            if self.entries[eid].tier == "P":
                continue
            seg = bytes(raw[s:e])
            codec = {"x": None, "u8": "utf-8", "u16": "utf-16-le"}.get(enc, enc)
            try:
                if codec is None:
                    try:
                        surface = seg.decode("ascii")
                        codec = "ascii"
                    except UnicodeDecodeError:
                        surface = seg.decode("latin-1")
                        codec = "latin-1"
                else:
                    surface = seg.decode(codec)
                # final verification on the real (un-normalised) characters
                if pseudo.canon(surface) != "".join(self.entries[eid].tokens):
                    continue
                rep = self._render(self.entries[eid], surface)
                if rep is None:
                    continue
                nb = rep.encode(codec)
            except (UnicodeDecodeError, UnicodeEncodeError):
                continue
            if nb == seg or (fixed and len(nb) != len(seg)):
                continue
            out.append((s, e, nb))
            self.stats[eid] = self.stats.get(eid, 0) + 1
        return out


class _LazyOrig:
    """Index access to original (un-normalised) characters of a byte view."""
    __slots__ = ("raw", "lo")

    def __init__(self, raw, lo):
        self.raw = raw
        self.lo = lo

    def __getitem__(self, i):
        return chr(self.raw[self.lo + i])

    def __len__(self):
        return len(self.raw) - self.lo


def apply_plan(buf, plan):
    """Apply (start, end, bytes) replacements to a bytearray in place
    (right to left, so replacements of a different length are fine too)."""
    for s, e, nb in sorted(plan, reverse=True):
        buf[s:e] = nb
    return len(plan)
