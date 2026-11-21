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


_JOIN = frozenset("/\\._-@:~0123456789")
# values that mean "no value", and profile folders that belong to no one
_ABSENT = frozenset((
    "unknown", "none", "null", "n a", "na", "nan", "anonymous", "unspecified", "not specified", "tbd", "tba",
    "undefined", "empty", "author", "name", "your name", "user name", "username", "company", "company name",
    "organization", "organisation", "public", "default", "default user", "all users", "defaultuser0", "shared",
    "desconocido", "unbekannt", "inconnu", "sin nombre",
    "未指定", "未知", "无", "無", "不明", "不详", "未設定", "未设置", "匿名", "なし", "нет", "неизвестно"))


def _alpha_script(u):
    """Code point of a letter in a script that writes words with spaces
    between them (accented Latin, Greek, Cyrillic, Armenian)?"""
    return (0xC0 <= u <= 0x24F or 0x370 <= u <= 0x58F) and chr(u).isalpha()


def _wide_letter(orig, i):
    """Is the UTF-16LE code unit at offset i of the byte view such a letter?"""
    try:
        return _alpha_script(ord(orig[i]) | (ord(orig[i + 1]) << 8))
    except IndexError:
        return False


def _utf8_letter_before(orig, s):
    """Does a two-byte UTF-8 character that is such a letter end right before s?"""
    try:
        b1, b0 = ord(orig[s - 1]), ord(orig[s - 2])
    except IndexError:
        return False
    if 0xC2 <= b0 <= 0xDF and 0x80 <= b1 <= 0xBF:
        return _alpha_script(((b0 & 0x1F) << 6) | (b1 & 0x3F))
    return False


def _looks_wide(seg):
    """Is this value UTF-16LE text? Latin text has zero high bytes. Text in
    another script is recognised by its narrow reading, which would hold
    control characters (the high byte of Cyrillic is 0x04, of Greek 0x03),
    while its wide reading is letters, digits and ordinary punctuation."""
    n = len(seg)
    if n < 4 or n % 2:
        return False
    if not any(seg[1::2]):
        return all(seg[0::2][:2])
    if any(c < 0x20 for c in seg):
        try:
            t = seg.decode("utf-16-le")
        except UnicodeDecodeError:
            return False
        return all(ch.isalnum() or ch in " ._-'()~" for ch in t.rstrip("\x00"))
    return False


def _decode_value(core):
    """(text, codec) for the bytes of a narrow field value: UTF-8, else GBK
    when that reading is ideographs throughout, else the Western code page."""
    try:
        return core.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        t = core.decode("gbk")
        if any("\u4e00" <= ch <= "\u9fff" for ch in t) and all(
                ch < "\u0080" or "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef" for ch in t):
            return t, "gbk"
    except UnicodeDecodeError:
        pass
    try:
        return core.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return core.decode("latin-1"), "latin-1"


class Entry:
    __slots__ = ("eid", "tokens", "keep", "tier", "cls", "pseudo", "mask", "nchar", "groups", "word")


class Engine:
    def __init__(self, dictionary, key, protect=None):
        """dictionary: dict with 'entries' (list of {t, k, o, tier, cls}) and
        optional 'table' (token -> pseudonym)."""
        if isinstance(dictionary, str):
            with open(dictionary, encoding="utf-8") as fh:
                dictionary = json.load(fh)
        self.pz = pseudo.Pseudonymizer(key, dictionary.get("table"))
        self.tokflag = dictionary.get("tokflag") or {}
        lists = dictionary.get("lists") or {}
        self.generic = frozenset(lists.get("generic") or ())
        self.keeptok = frozenset(lists.get("keeptok") or ())
        self.common = frozenset(lists.get("common") or ())
        self.proper = frozenset(lists.get("proper") or ())
        self.struct = frozenset(lists.get("struct") or ("pc", "laptop", "desktop", "notebook", "inc", "ltd", "gmbh",
                                                        "llc", "corp", "co", "university", "lab", "labs"))
        self.group = None      # source group of the file being processed
        self.stats = {}        # eid -> replacements (reset per file by callers)
        # Document scope: identities known to belong to the file being
        # processed (found in its identity fields, its text or its path).
        # They are replaced everywhere in that file, whatever their tier.
        self.no_protect = False    # True while protected file names are to be ignored
        self.bare = set()          # protected names that also count without a file extension
        self.extra_groups = frozenset()   # source groups that hold exact copies of the current file
        self.links = dictionary.get("links") or {}   # group -> groups that share files with it
        self.scoped = set()        # eids replaceable in free text of this file
        self.scoped_sub = set()    # eids matched even inside a longer word
        self.single = {}           # token -> eid of its single-token entry
        self._scope_auto = None
        self.keys_of = {}
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
            e.word = d.get("w", 0)      # 1: a dictionary word in some language, 2: common vocabulary
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
                e.word = 0
                e.pseudo = "".join(toks)
                e.mask = "1" * len(e.pseudo)
                e.nchar = len(e.pseudo)
                e.groups = None
                self.entries.append(e)
                # a name that looks like a part or drawing number (several
                # words, a digit or a joining character) is the file's name
                # wherever it is written, with or without an extension
                if len(stem) >= 8 and len(toks) >= 2 and any(c.isdigit() or c in "_-" for c in stem):
                    self.bare.add(e.eid)
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
        self.ndict = len(self.entries)     # entries above this index are run-time name parts
        for k, v in keys.items():
            self.keys_of.setdefault(v[0], []).append((k, v))
        for e in self.entries:
            if len(e.tokens) == 1 and e.tier != "P":
                self.single.setdefault(e.tokens[0], e.eid)
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

    @staticmethod
    def _digit_edge(ent, view, outside, inside):
        """A name of four or more characters that ends (or starts) with a
        letter and is followed (or preceded) by a digit: "ACME2", "A3ACME".
        The digit is a version or code written against the name, so the name
        still counts as a word of its own."""
        return (ent.nchar >= 4 and "0" <= view[outside] <= "9" and view[inside].isalpha()
                and view[inside] < "\x80")

    def _hits(self, view, orig, bytes_view, free_only=False, wide=True, auto=None):
        """All boundary-valid dictionary matches: (start, end, eid, enc)."""
        out = []
        binary = bytes_view and getattr(self, "_binary", False)
        if not self.nkeys or (free_only and not self.nfree and auto is None):
            return out
        n = len(view)
        ents = self.entries
        if auto is None:
            auto = self.A if not free_only else (self.F if wide else self.F8)
        for end, (eid, enc, klen) in auto.iter(view):
            e = end + 1
            s = e - klen
            if ents[eid].tier == "P":
                if self.no_protect:
                    continue
                # a protected file name counts when an extension follows; a
                # name that looks like a part number also counts on its own,
                # as a complete word
                if enc == "u16":
                    if not (bytes_view and view[e:e + 2] == ".\x00" and e + 2 < n and view[e + 2].isalnum()):
                        if eid not in self.bare or not bytes_view:
                            continue
                        if e + 1 < n and view[e + 1] == "\x00" and view[e].isalnum():
                            continue
                    if s >= 2 and view[s - 1] == "\x00" and view[s - 2].isalnum() and view[s - 2] < "\x80":
                        continue
                else:
                    if not (view[e:e + 1] == "." and e + 1 < n and view[e + 1].isalnum()):
                        if eid not in self.bare:
                            continue
                        if e < n and view[e].isalnum():
                            continue
                    if s > 0 and view[s - 1].isalnum():
                        continue
                out.append((s, e, eid, enc))
                continue
            if eid in self.scoped_sub:
                if enc == "u16" and not bytes_view:
                    continue
                if enc not in ("x", "u16") and not bytes_view:
                    continue
                out.append((s, e, eid, enc))
                continue
            if enc == "u16":
                if not bytes_view:
                    continue
                # a letter of an alphabetic script outside ASCII right next to
                # the match (Cyrillic, Greek, accented Latin): the match is
                # part of a longer word
                # (only when the match itself begins / ends with such a letter:
                # next to an ASCII name the neighbouring unit may be binary
                # data; and not when the unit is the length byte of a string
                # marker FF FE FF <n>)
                if (s >= 2 and _wide_letter(orig, s) and _wide_letter(orig, s - 2) and not (
                        s >= 4 and orig[s - 2] == "\xff" and orig[s - 3] == "\xfe" and orig[s - 4] == "\xff")):
                    continue
                if e + 1 < n and _wide_letter(orig, e - 2) and _wide_letter(orig, e):
                    continue
                loose = ents[eid].nchar >= 5 or eid in self.scoped
                if s >= 2 and view[s - 1] == "\x00" and view[s - 2].isalnum() and view[s - 2] < "\x80":
                    if not ((loose and self._camel16(orig, s, n)) or self._digit_edge(ents[eid], view, s - 2, s)):
                        continue
                if e + 1 < n and view[e + 1] == "\x00" and view[e].isalnum() and view[e] < "\x80":
                    if not ((loose and self._camel16(orig, e, n)) or self._digit_edge(ents[eid], view, e, e - 2)):
                        continue
            else:
                loose = ents[eid].nchar >= 5 or eid in self.scoped
                # UTF-8 text: the character before the match is a letter of an
                # alphabetic script outside ASCII (its last byte is not a
                # letter in the byte view, so the test below cannot see it)
                if (bytes_view and s >= 2 and orig[s] >= "\xc2" and "\x80" <= orig[s - 1] < "\xc0"
                        and _utf8_letter_before(orig, s)):
                    continue
                # In a binary stream a byte >= 0x80 next to an ASCII name is a
                # tag or length byte, not a letter: it ends the word.
                if s > 0 and view[s - 1].isalnum() and not (binary and orig[s - 1] >= "\x80"):
                    if not ((loose and self._camel(orig, s)) or self._digit_edge(ents[eid], view, s - 1, s)
                            or (binary and ents[eid].nchar >= 4 and _tagged(orig, view, s, e, n))):
                        continue
                if e < n and view[e].isalnum() and not (binary and orig[e] >= "\x80"):
                    if not ((loose and self._camel(orig, e)) or self._digit_edge(ents[eid], view, e, e - 1)):
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
        """May this entry be replaced in free text of the current file?"""
        if ent.tier == "A":
            return True
        if ent.tier == "L" and self._home(ent):
            return True
        return ent.eid in self.scoped

    def _home(self, ent):
        """Is the current file in a source group where this identity was seen
        (its own group, or the group of an exact copy of the file)?"""
        if self.group in ent.groups:
            return True
        return bool(self.extra_groups) and not self.extra_groups.isdisjoint(ent.groups)

    # ------------------------------------------------------------------
    # document scope
    # ------------------------------------------------------------------
    def scope_begin(self):
        self.scoped = set()
        self.scoped_sub = set()
        self._scope_auto = None

    def scope_save(self):
        return (self.scoped, self.scoped_sub, self._scope_auto, self.no_protect, self.group, self.extra_groups)

    def scope_restore(self, state):
        (self.scoped, self.scoped_sub, self._scope_auto, self.no_protect, self.group, self.extra_groups) = state

    def scope_promote(self, eids):
        """Add identities to the scope of the current file. For a name of
        several words its distinctive words are added too, so a surname that
        stands alone elsewhere in the document is also replaced. Returns True
        when the scope grew."""
        before = len(self.scoped)
        for eid in eids:
            ent = self.entries[eid]
            if ent.tier == "P":
                continue
            if len(ent.tokens) == 1:
                if ent.word != 2:
                    self.scoped.add(eid)
                continue
            self.scoped.add(eid)
            for tok, keep in zip(ent.tokens, ent.keep):
                if keep or len(tok) < 4 or tok.isdigit():
                    continue            # short parts ("lab" of "acme-robo-lab") are too ambiguous alone
                sid = self.single.get(tok)
                if sid is None:
                    sid = self._virtual(tok)
                if sid is not None and self.entries[sid].word != 2:
                    self.scoped.add(sid)
        if len(self.scoped) == before:
            return False
        for eid in self.scoped:
            ent = self.entries[eid]
            if len(ent.tokens) == 1 and not ent.word:
                t = ent.tokens[0]
                if len(t) >= 6 or (len(t) >= 5 and any(c.isdigit() for c in t)):
                    self.scoped_sub.add(eid)
        auto = ahocorasick.Automaton()
        n = 0
        for eid in self.scoped:
            ent = self.entries[eid]
            if ent.tier in ("B", "V"):         # these keys are not in the free-text automata
                for k, v in self.keys_of.get(eid, ()):
                    auto.add_word(k, v)
                    n += 1
        if n:
            auto.make_automaton()
            self._scope_auto = auto
        else:
            self._scope_auto = None
        return True

    def field_pseudonym(self, text, force=False):
        """Pseudonym for the value of an identity field (author, company,
        last saved by) that the dictionary left unchanged. A value that is a
        placeholder, a product name, a sentence or ordinary words is returned
        as it is; otherwise every name-like word gets its token pseudonym
        (the same one that word has anywhere else in the corpus). Returns
        (new text, list of pseudonymised tokens).

        force=True is for fields that hold an account or a person and nothing
        else (last saved by, author, the user folder of a stored path): there
        even an ordinary-looking account name ("Administrator", "user") is
        the identity, so every word is replaced. Only values that say "no
        value" and the system profile folders stay."""
        if not text or len(text) > (120 if force else 64):
            return text, []
        toks = pseudo.tokens_of(text)
        if not toks or len(toks) > (8 if force else 5):
            return text, []
        if force:
            if " ".join(toks) in _ABSENT:
                return text, []
            names = [t for t in toks if any(c.isalpha() for c in t) and t not in self.struct]
            if not names:
                names = [t for t in toks if any(c.isalpha() for c in t)]
            if not names and len(toks) == 1 and toks[0].isdigit() and 4 <= len(toks[0]) <= 12:
                names = list(toks)
            if not names:
                return text, []
            return self._pseudonymise_words(text, set(names))
        if not self.common:
            return text, []
        if "".join(toks) in self.generic:
            return text, []
        names = []
        for t in toks:
            if t in self.keeptok or t in self.generic:
                continue
            if t.isdigit():
                if len(toks) == 1 and 4 <= len(t) <= 12:
                    names.append(t)
                continue
            if (len(t) < 2 and t.isascii()) or not any(c.isalpha() for c in t):
                continue
            base = t.rstrip("0123456789")
            if base != t and (base in self.keeptok or base in self.generic or base in self.common):
                continue                    # "staff1", "user2"
            if t in self.proper or t not in self.common:
                names.append(t)
        if not names:
            return text, []
        return self._pseudonymise_words(text, set(names))

    def _pseudonymise_words(self, text, want):
        out = []
        cur = []
        done = []

        def flush():
            if not cur:
                return
            word = "".join(cur)
            c = pseudo.canon(word)
            if c in want:
                ps = self.pz.token(c)
                k = 0
                for ch in word:
                    if pseudo.canon_char(ch) and k < len(ps):
                        out.append(pseudo._shape(ch, ps[k]))
                        k += 1
                    else:
                        out.append(ch)
                done.append(c)
            else:
                out.append(word)
            del cur[:]

        for ch in text:
            if pseudo.canon_char(ch):
                cur.append(ch)
            else:
                flush()
                out.append(ch)
        flush()
        new = "".join(out)
        return (new, done) if new != text else (text, [])

    def words_pseudonym(self, text):
        """Replace every word of a free-form value by its token pseudonym
        (letters stay letters, digits inside a word stay digits, numbers and
        punctuation are kept): for descriptive metadata values that are
        treated as identifying as a whole. Returns the new text."""
        if not text or len(text) > 2000:
            return text
        want = set(t for t in pseudo.tokens_of(text) if any(c.isalpha() for c in t) and len(t) >= 2)
        if not want or " ".join(pseudo.tokens_of(text)) in _ABSENT:
            return text
        return self._pseudonymise_words(text, want)[0]

    def number_pseudonym(self, text):
        """Replace every run of two or more digits by its token pseudonym
        (digits stay digits, same length): telephone numbers."""
        out = []
        cur = []

        def flush():
            if cur:
                run = "".join(cur)
                out.append(self.pz.token(run) if len(run) >= 2 else run)
                del cur[:]

        for ch in text:
            if "0" <= ch <= "9":
                cur.append(ch)
            else:
                flush()
                out.append(ch)
        flush()
        return "".join(out)

    def _virtual(self, tok, force=False):
        """Entry for one word of a longer name that has no entry of its own
        ("smith" of "Smith Robotics"). It exists only for document scope: it
        is never matched unless its name is in scope for the current file."""
        if tok not in self.pz.table and not force:
            return None
        e = Entry()
        e.eid = len(self.entries)
        e.tokens = [tok]
        e.keep = [0]
        e.tier = "V"
        e.cls = "PART"
        e.word = self.tokflag.get(tok, 0)
        if not e.word and tok in self.common:
            e.word = 2
        elif not e.word and tok in self.proper:
            e.word = 1
        e.pseudo = self.pz.token(tok)
        e.mask = "0" * len(tok)
        e.nchar = len(e.pseudo)
        e.groups = None
        self.entries.append(e)
        self.single[tok] = e.eid
        keys = [(norm_text(tok), (e.eid, "x", len(norm_text(tok))))]
        k16 = norm_bytes(tok.encode("utf-16-le"))
        keys.append((k16, (e.eid, "u16", len(k16))))
        self.keys_of[e.eid] = keys
        return e.eid

    def scope_tokens(self, tokens):
        """Put single words into the scope of the current file (words of an
        identity-field value that was pseudonymised without a dictionary
        entry). Returns True when the scope grew."""
        ids = set()
        for t in tokens:
            if len(t) < 3 and not (len(t) == 2 and not t.isascii()):
                continue
            sid = self.single.get(t)
            if sid is None:
                sid = self._virtual(t, force=True)
            if sid is not None:
                ids.add(sid)
        return self.scope_promote(ids) if ids else False

    def _text_hits(self, view, text):
        hits = self._hits(view, text, False)
        if self._scope_auto is not None:
            more = self._hits(view, text, False, auto=self._scope_auto)
            if more:
                hits = list(set(hits) | set(more))
        return hits

    def match_ids(self, text, ctx="text"):
        """Entry ids matched in a real string (same rules as spans_text)."""
        if not text or not self.nkeys:
            return set()
        view = norm_text(text)
        if len(view) != len(text):
            return set()
        hits = [h for h in self._text_hits(view, text)
                if ctx == "id" or (self.entries[h[2]].tier != "P" and self._free_ok(self.entries[h[2]]))]
        return {h[2] for h in self._select(hits) if self.entries[h[2]].tier != "P"}

    def _scope_surface_ok(self, ent, surface, prev="", nxt=""):
        """A scoped name that is also an ordinary word of some language is
        replaced only where it is written as a proper noun (capitalised) or
        as part of an address, path or identifier ("/acme-docs", "acme.org",
        "acme_v2")."""
        if ent.word == 1 and ent.eid in self.scoped and ent.tier != "A" and \
                not (ent.tier == "L" and self._home(ent)):
            if surface[:1].isupper():
                return True
            return (prev in _JOIN and prev != "") or (nxt in _JOIN and nxt != "")
        return True

    def spans_text(self, text, ctx="text"):
        """Dictionary matches in a real string as (start, end, replacement)."""
        if not text or not self.nkeys:
            return []
        view = norm_text(text)
        if len(view) != len(text):
            return []
        hits = [h for h in self._text_hits(view, text)
                if ctx == "id" or self.entries[h[2]].tier == "P" or self._free_ok(self.entries[h[2]])]
        out = []
        for s, e, eid, _enc in self._select(hits):
            ent = self.entries[eid]
            if ent.tier == "P" or pseudo.canon(text[s:e]) != "".join(ent.tokens):
                continue
            if ctx != "id" and not self._scope_surface_ok(ent, text[s:e], text[s - 1:s] if s else "", text[e:e + 1]):
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
        hits = [h for h in self._text_hits(view, text)
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
            if ctx != "id" and not self._scope_surface_ok(self.entries[eid], text[s:e],
                                                           text[s - 1:s] if s else "", text[e:e + 1]):
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

    def plan_bytes(self, raw, id_spans=(), text_ok=True, lo=0, hi=None, fixed=True, wide=True, binary=False,
                   force_ids=None):
        """Find replacements in a bytes-like object (bytes, bytearray, mmap).

        id_spans : sorted list of (start, end) spans with identity context.
        text_ok  : True, False/None, or callable(pos)->bool telling whether
                   free-text (tier A/L) replacement is allowed at a position.
        lo, hi   : restrict the scan to raw[lo:hi].
        wide     : also look for UTF-16LE spellings in free text (binary
                   streams); text files pass wide=False.
        binary   : the input is a binary stream (not a text file): bytes
                   >= 0x80 beside an ASCII match count as word boundaries.
        fixed    : drop replacements whose encoded byte length differs from
                   the original (only possible for accented UTF-8 text);
                   with fixed=False they are returned and the caller must
                   cope with the shorter replacement.
        Returns a list of (start, end, replacement_bytes), non-overlapping.
        Large inputs are scanned in overlapping chunks.
        """
        if not self.nkeys:
            return []
        self._binary = bool(binary)
        try:
            out = self._plan_bytes(raw, id_spans, text_ok, lo, hi, fixed, wide)
        finally:
            self._binary = False
        if force_ids:
            out = self._force_spans(raw, out, force_ids, fixed)
        return out

    def _force_spans(self, raw, out, spans, fixed):
        """Account / person fields the dictionary left as they were: replace
        the whole value word by word (see field_pseudonym, force=True)."""
        extra = []
        for sp in spans:
            a, b = sp[0], sp[1]
            hint = sp[2] if len(sp) > 2 else None      # the value's codec, or "narrow", when the caller knows
            if b <= a or b - a > 400 or any(s < b and e > a for s, e, _nb in out):
                continue
            seg = bytes(raw[a:b])
            if hint is None:
                wide = _looks_wide(seg)
            else:
                wide = hint == "utf-16-le" and len(seg) % 2 == 0
                if hint == "utf-16-le" and not wide:
                    continue
            if wide:
                core = seg
                while len(core) >= 2 and core[-2:] == b"\x00\x00":
                    core = core[:-2]
                codec = "utf-16-le"
                if not core:
                    continue
                try:
                    text = core.decode(codec)
                except UnicodeDecodeError:
                    continue
            else:
                core = seg.rstrip(b"\x00")
                if not core:
                    continue
                if a >= 7 and bytes(raw[a - 6:a]) == b"/home/" and chr(raw[a - 7]).isalnum():
                    continue              # "site.org/home/about/": a web address, not a home folder
                text = None
                if hint and hint not in ("narrow", "utf-8", "ascii", "cp1252", "latin-1"):
                    try:
                        text, codec = core.decode(hint), hint       # the code page the container declares
                    except (UnicodeDecodeError, LookupError):
                        text = None
                if text is None:
                    text, codec = _decode_value(core)
            if "<" in text or ">" in text or "&" in text or "\ufffd" in text:
                continue                  # markup or an escaped character: not a plain value
            new, toks = self.field_pseudonym(text, force=True)
            if not toks or new == text:
                continue
            try:
                nb = new.encode(codec)
            except UnicodeEncodeError:
                continue
            if len(nb) != len(core) and fixed:
                continue
            extra.append((a, a + len(core), nb))
            self.stats[-1] = self.stats.get(-1, 0) + 1
        if not extra:
            return out
        return sorted(out + extra)

    def _plan_bytes(self, raw, id_spans, text_ok, lo, hi, fixed, wide):
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
                found = self._hits(view, _LazyOrig(raw, a), True, free_only=True, wide=wide)
                if self._scope_auto is not None:
                    found = found + self._hits(view, _LazyOrig(raw, a), True, free_only=True, wide=wide,
                                               auto=self._scope_auto)
                for s, e, eid, enc in found:
                    s += a
                    e += a
                    if s < pos or s >= limit:
                        continue
                    ent = self.entries[eid]
                    if ent.tier != "P":
                        if not self._free_ok(ent):
                            continue
                        if self._binary and ent.nchar < 4 and enc != "u16" and not _long_run(raw, s, e, n):
                            continue      # a 3-letter name in binary data: too easily a chance match
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
                if not self._scope_surface_ok(self.entries[eid], surface,
                                              chr(raw[s - 1]) if s > 0 else "", chr(raw[e]) if e < len(raw) else ""):
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


def _tagged(orig, view, s, e, n):
    """Binary records put a tag or length byte in front of a string, and that
    byte can happen to be a letter or digit ("\x05OACME3_020.PRT"). A match
    is accepted behind one or two such bytes when they follow a
    non-printable byte and the match goes on like an identifier or file
    name (digit, underscore, dot or hyphen next)."""
    if e >= n or view[e] not in "0123456789_.-":
        return False
    k = s - 1
    if k >= 1 and view[k - 1].isalnum():
        k -= 1                               # two tag bytes
    if k == 0:
        return True
    c = orig[k - 1]
    return not (" " <= c < "\x7f")


def _long_run(raw, s, e, n, need=12):
    """Is raw[s:e] part of a run of at least `need` printable ASCII bytes?"""
    a = s
    while a > 0 and s - a < need and 0x20 <= raw[a - 1] < 0x7F:
        a -= 1
    b = e
    while b < n and (b - a) < need and 0x20 <= raw[b] < 0x7F:
        b += 1
    return b - a >= need


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
