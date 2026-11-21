"""ISO 10303-21 (STEP) field location.

Only string literals are ever touched. Header fields (FILE_NAME author,
organization, authorisation, name; FILE_DESCRIPTION) and the string arguments
of PERSON / ORGANIZATION / ADDRESS entity instances are returned as Regions.
"""
from .regions import Region

_KWCHARS = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789abcdefghijklmnopqrstuvwxyz")
_SPACE = set(b" \t\r\n")


def p21_strings(buf, a, b):
    """Tokenise buf[a:b] starting at an opening '('.

    Yields (argidx, start, end, has_newline) for every string literal, where
    argidx is the index of the top-level argument the literal belongs to.
    Stops at the parenthesis matching the first '('. Returns via StopIteration
    the offset just past that parenthesis.
    """
    i = a
    depth = 0
    arg = 0
    while i < b:
        c = buf[i]
        if c == 0x27:  # '
            j = i + 1
            while True:
                k = buf.find(b"'", j, b)
                if k < 0:
                    return b
                if k + 1 < b and buf[k + 1] == 0x27:
                    j = k + 2
                    continue
                break
            seg = buf[i + 1:k]
            yield (arg, i + 1, k, (b"\n" in seg or b"\r" in seg))
            i = k + 1
            continue
        if c == 0x2F and i + 1 < b and buf[i + 1] == 0x2A:  # /*
            k = buf.find(b"*/", i + 2, b)
            if k < 0:
                return b
            i = k + 2
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth <= 0:
                return i + 1
        elif c == 0x2C and depth == 1:
            arg += 1
        elif c == 0x3B and depth <= 0:
            return i
        i += 1
    return b


def _mk(buf, s, e, nl, label, ctx):
    if not nl:
        return Region(s, e, label, ctx)
    omap = [o for o in range(s, e) if buf[o] not in (0x0A, 0x0D)]
    if not omap:
        return None
    return Region(omap[0], omap[-1] + 1, label, ctx, omap=omap)


_HEADER = (
    (b"FILE_DESCRIPTION", {0: ("step.description", "text")}),
    (b"FILE_NAME", {0: ("step.name", "text"), 2: ("step.author", "id"), 3: ("step.org", "id"),
                    4: ("step.preprocessor", "text"), 5: ("step.system", "text"),
                    6: ("step.authorization", "id")}),
    (b"FILE_SCHEMA", {0: ("step.schema", "keep")}),
)

_PERSON = {0: ("step.person.id", "id"), 1: ("step.person.last", "id"), 2: ("step.person.first", "id"),
           3: ("step.person.middle", "id"), 4: ("step.person.prefix", "text"),
           5: ("step.person.suffix", "text")}
_ORG = {0: ("step.org.id", "id"), 1: ("step.org.name", "id"), 2: ("step.org.desc", "text")}
_ADDR_NAMES = ("internal_location", "street_number", "street", "postal_box", "town", "region",
               "postal_code", "country", "fax", "phone", "email", "telex")
_ADDR = {i: ("step.address." + nm, "id") for i, nm in enumerate(_ADDR_NAMES)}
_ADDR[13] = ("step.address.desc", "text")
_ENT = {b"PERSON": _PERSON, b"ORGANIZATION": _ORG, b"ADDRESS": _ADDR, b"PERSONAL_ADDRESS": _ADDR,
        b"ORGANIZATIONAL_ADDRESS": _ADDR}


def header_bounds(buf, n=None):
    n = len(buf) if n is None else n
    h = buf.find(b"HEADER", 0, min(n, 1 << 20))
    if h < 0:
        return -1, -1
    e = buf.find(b"ENDSEC", h, min(n, h + (4 << 20)))
    if e < 0:
        e = min(n, h + (1 << 16))
    return h, e


def step_regions(buf, n=None, entities=True):
    """Return (regions, header_end) for a STEP file held in buf (bytes/mmap)."""
    n = len(buf) if n is None else n
    regs = []
    h, e = header_bounds(buf, n)
    if h < 0:
        return regs, 0
    for kw, labels in _HEADER:
        k = buf.find(kw, h, e)
        if k < 0:
            continue
        p = buf.find(b"(", k, e)
        if p < 0:
            continue
        for arg, s, t, nl in p21_strings(buf, p, e):
            lab = labels.get(arg)
            if lab and t > s:
                r = _mk(buf, s, t, nl, lab[0], lab[1])
                if r:
                    regs.append(r)
    if entities:
        seen = set()
        for kw in (b"PERSON", b"ORGANIZATION", b"ADDRESS"):
            i = buf.find(kw, e, n)
            while i >= 0:
                j = i
                while j > 0 and buf[j - 1] in _KWCHARS:
                    j -= 1
                q = i + len(kw)
                full = bytes(buf[j:q])
                labels = _ENT.get(full)
                if labels is not None and j not in seen:
                    # keyword must directly follow '=' (simple entity instance)
                    b0 = j - 1
                    while b0 > 0 and buf[b0] in _SPACE:
                        b0 -= 1
                    p = q
                    while p < n and buf[p] in _SPACE:
                        p += 1
                    if buf[b0] == 0x3D and p < n and buf[p] == 0x28:
                        seen.add(j)
                        lim = min(n, p + 8192)
                        for arg, s, t, nl in p21_strings(buf, p, lim):
                            lab = labels.get(arg)
                            if lab and t > s:
                                r = _mk(buf, s, t, nl, lab[0], lab[1])
                                if r:
                                    regs.append(r)
                i = buf.find(kw, i + len(kw), n)
    return regs, e


def in_string(buf, pos, lo=0):
    """True when byte offset pos lies inside a string literal. Looks back to
    the previous statement terminator (';' followed by a line break)."""
    a = max(lo, pos - 65536)
    k = max(buf.rfind(b";\n", a, pos), buf.rfind(b";\r", a, pos))
    start = a if k < 0 else k + 1
    return bytes(buf[start:pos]).count(b"'") % 2 == 1


def in_comment(buf, pos):
    """True when pos lies inside a /* ... */ comment (looks back 16 KB)."""
    a = max(0, pos - 16384)
    o = buf.rfind(b"/*", a, pos)
    if o < 0:
        return False
    c = buf.rfind(b"*/", o, pos)
    return c < 0
