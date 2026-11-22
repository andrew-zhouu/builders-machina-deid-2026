"""Scope terms: the words of the name of a file's source.

A PDF comes from a source folder: an account and a repository ("account__project-name"), a site
("www.example.org"), a catalogue entry, one of the organisers' collections. The words of that folder name
are the names of the account and of the project, and they are identifying for every document of the source
whatever they look like: a project called "Blue Lantern" or "Quiet Harbour" is named by ordinary words.
With --scope-terms each of these words gets its token pseudonym wherever it stands in the text and in the
descriptive metadata of the source's PDFs, also inside a longer word ("Lanterns" of project "Lantern"),
because a reader looking for the name finds it there too.

  words of four letters and more: replaced wherever the letters stand
  words of three letters:         replaced as whole words only
  not taken: numbers, content hashes, top-level domains and "www", and the words by which the
             organisers' own collections are filed (research, round3, validate, extracted ...)
  the name of a source: account and repository; site; catalogue entry; in the organisers' collections the
             first folder below the data set that names a source. Folders further down are not part of it.

A replacement must not spell a scope term itself (a pseudonym could, by chance): such a pseudonym is
shifted until it does not.
"""
import re

from . import pseudo

_HEX = re.compile(r"^[0-9a-f]{10,40}$")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_CAMEL2 = re.compile(r"[A-Z]{2,}|[A-Z]?[a-z]+|\d+")      # "ABCDword" read as ABCD + word as well as ABC + Dword
_SITE = frozenset("www com org net edu gov io de uk ch fr it es nl co ac eu us ca au jp cn in info me app dev "
                  "pages html htm php blog wiki docs static files download downloads public".split())
_STRUCT = frozenset("assembly search drawing drawings spreadsheet originals broad github curated extracted engine "
                    "engines assets machines robots scientific round validate local review almost collections "
                    "qualified research educational industrial root technical library specified references only "
                    "paired parts recommended reference caveats pdfs pdf gate mass preamp files fabrication "
                    "reviewed corresponding pending five new top zip step models model data images docs "
                    "previous cases complete secondary preview strong complex below target flat contents".split())
# joining words of a name: never a name themselves
_JOIN = frozenset("the and for via with from les des der von van del los las una und par sur per con".split())
_ARCH = re.compile(r"\.(zip|tar|gz|tgz|7z|rar|extracted)$", re.I)
_CACHE = {}


def _named(comp):
    """True when a folder name of the organisers' collections names a source (and is not only one of the
    words by which the collections are filed)."""
    for part in re.split(r"[^A-Za-z0-9]+", _ARCH.sub("", comp)):
        for c in set(_CAMEL.findall(part)) | set(_CAMEL2.findall(part)):
            c = c.lower()
            if c.isalpha() and c.isascii() and len(c) >= 3 and c not in _STRUCT and c not in _JOIN:
                return True
    return False


def source_depth(parts):
    """Index of the folder that names the source of a file of the organisers' collections: the first
    folder below the data set whose name says more than how the collection is filed
    ("research/round3/industrial/<source>/...", "validate_<source>/...", "machines/<source>/drawings/...").
    Folders further down are the inner structure of that source, like the folders inside a repository."""
    for d in range(3, len(parts) - 1):
        if _named(parts[d]):
            return d
    return None


def names_of(rel):
    """(folder names that name the source of `rel`, words to leave out)."""
    a = rel.split("!/")[0].split("/")
    if len(a) >= 3 and a[0] == "sources":
        if a[1] in ("github", "gitlab"):
            return a[2].split("__"), frozenset()
        if a[1] == "catalog":
            return [a[2]], frozenset()
        if a[1] == "web":
            return [a[2]], _SITE
        if a[1] == "existing":
            d = source_depth(a)
            return ([a[d]] if d is not None else []), _STRUCT
    if len(a) >= 2 and a[0] == "archives":
        return _ARCH.sub("", a[1]).split("__"), frozenset()
    return [], frozenset()


def terms_of(rel):
    """(long terms, three-letter terms) of the source of `rel`, lower case."""
    names, skip = names_of(rel)
    longs = set()
    shorts = set()
    for name in names:
        name = _ARCH.sub("", name)
        for part in re.split(r"[^A-Za-z0-9]+", name):
            if not part:
                continue
            if _HEX.match(part.lower()) and any(ch.isdigit() for ch in part):
                continue                      # the hash that tells two snapshots of one repository apart
            for c in set(_CAMEL.findall(part)) | set(_CAMEL2.findall(part)):
                c = c.lower()
                if not c.isalpha() or not c.isascii() or c in skip or c in _JOIN:
                    continue
                if len(c) >= 4:
                    longs.add(c)
                elif len(c) == 3:
                    shorts.add(c)
    return frozenset(longs), frozenset(shorts)


CURRENT = [None, None]      # (corpus path, output path) of the file being processed (set by the driver)


def _name_parts(parts):
    """The folder names (as a list) that name the source of a path given as its components."""
    if len(parts) >= 3 and parts[0] == "sources":
        if parts[1] in ("github", "gitlab"):
            return parts[2].split("__")
        if parts[1] in ("catalog", "web"):
            return [parts[2]]
        if parts[1] == "existing":
            d = source_depth(parts)
            return [parts[d]] if d is not None else []
    return []


def _from_folder(rel, out_rel, terms):
    """{term: pseudonym} read off the source folder's new name: where the
    dictionary already renamed the part of the folder name that holds a term
    (an account, a known project name, also as part of a longer name), the
    term takes the letters that stand in its place, so that the folder and
    the text of its documents show the same new name."""
    a = rel.split("/")
    b = out_rel.split("/")
    if len(a) != len(b):
        return {}
    ni = _name_parts(a)
    if a[1] == "existing":
        d = source_depth(a)
        no = [b[d]] if d is not None else []
    elif a[1] in ("github", "gitlab"):
        no = b[2].split("__")
    else:
        no = [b[2]]
    if len(ni) != len(no):
        return {}
    mp = {}
    for x, y in zip(ni, no):
        if len(x) != len(y):
            continue
        for t in sorted(terms, key=len, reverse=True):
            if t in mp:
                continue
            for m in re.finditer(re.escape(t), x, re.I):
                s, e = m.span()
                cand = y[s:e].lower()
                if cand != x[s:e].lower() and cand.isalpha() and cand.isascii() and len(cand) == len(t):
                    mp[t] = cand
                    break
    return mp


def compiled(rel):
    """(matcher, {term: pseudonym from the folder name}) for the scope terms
    of `rel`, or None when its source has none."""
    key = rel.split("!/")[0].rsplit("/", 1)[0]
    if key in _CACHE:
        return _CACHE[key]
    longs, shorts = terms_of(rel)
    alts = []
    if longs:
        alts.append("(?:%s)" % "|".join(sorted((re.escape(t) for t in longs), key=len, reverse=True)))
    if shorts:
        alts.append("(?<![A-Za-z])(?:%s)(?![A-Za-z])" % "|".join(sorted(re.escape(t) for t in shorts)))
    val = None
    if alts:
        mp = {}
        if CURRENT[0] == rel and CURRENT[1]:
            try:
                mp = _from_folder(rel, CURRENT[1], longs | shorts)
            except Exception:
                mp = {}
        val = (re.compile("|".join(alts), re.I), mp)
    if len(_CACHE) > 4096:
        _CACHE.clear()
    if CURRENT[0] == rel or val is None:
        _CACHE[key] = val               # an embedded file does not know the folder's new name: not kept
    return val


def merge(eng, text, taken=()):
    """The spans of `taken` (dictionary matches) together with the scope
    terms of `text`, sorted. A dictionary match that lies inside a term gives
    way to the term (same name, and the term's pseudonym is the one the
    folder shows); a dictionary match that reaches beyond the term (a longer
    name that holds it) stands."""
    st = eng.scope_terms
    res = list(taken)
    if st is None or not text or len(text) > 200000:
        return res
    rx, mp = st
    for m in rx.finditer(text):
        s, e = m.span()
        inside = []
        cross = False
        for sp in res:
            if sp[0] < e and sp[1] > s:
                if sp[0] >= s and sp[1] <= e:
                    inside.append(sp)
                else:
                    cross = True
                    break
        if cross:
            continue
        word = text[s:e]
        c = pseudo.canon(word)
        if len(c) != len(word):
            continue
        ps = mp.get(c) or eng.pz.token(c)
        if len(ps) != len(c):
            continue
        rep = "".join(pseudo._shape(ch, p) for ch, p in zip(word, ps))
        if rep == word:
            continue
        for sp in inside:
            res.remove(sp)
        res.append((s, e, rep))
    res.sort()
    return res


_VOWELS = "aeiou"
_CONS = "bcdfghjklmnpqrstvwxyz"


def _shift(text, k):
    out = []
    for ch in text:
        low = ch.lower()
        if low in _VOWELS:
            n = _VOWELS[(_VOWELS.index(low) + k) % len(_VOWELS)]
        elif low in _CONS:
            n = _CONS[(_CONS.index(low) + k) % len(_CONS)]
        else:
            out.append(ch)
            continue
        out.append(n.upper() if ch.isupper() else n)
    return "".join(out)


def guard(eng, spans_):
    """No replacement may spell a scope term of the document's source."""
    if eng.scope_terms is None:
        return spans_
    rx = eng.scope_terms[0]
    out = []
    for s, e, rep in spans_:
        if rep and rx.search(rep):
            for k in range(1, 12):
                alt = _shift(rep, k)
                if not rx.search(alt):
                    rep = alt
                    break
        out.append((s, e, rep))
    return out
