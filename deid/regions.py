"""Regions: byte spans of a file that carry text we understand.

A Region names a span of bytes (optionally discontiguous, via `omap`) together
with a label (which field it is), a context and an encoding:

  ctx "id"   - an identity field (author, company, user-name path component);
               every dictionary entry may be replaced here.
  ctx "text" - free text; only distinctive (tier A) entries are replaced.

The generic scanners at the bottom find user-name path components, e-mail
addresses and URLs by cheap literal anchors followed by a local regex.
"""
import re

from .tlds import TLDS


class Region:
    __slots__ = ("start", "end", "label", "ctx", "enc", "omap")

    def __init__(self, start, end, label, ctx="text", enc="latin-1", omap=None):
        self.start = start
        self.end = end
        self.label = label
        self.ctx = ctx
        self.enc = enc
        self.omap = omap

    def read(self, buf):
        if self.omap is None:
            return bytes(buf[self.start:self.end])
        return bytes(buf[o] for o in self.omap)

    def text(self, buf):
        raw = self.read(buf)
        if self.enc == "latin-1":
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("cp1252", "replace")
        return raw.decode(self.enc, "replace")

    def __repr__(self):
        return "Region(%d,%d,%s,%s,%s)" % (self.start, self.end, self.label, self.ctx, self.enc)


# --------------------------------------------------------------------------
# user-name path components
# --------------------------------------------------------------------------
_SEP = b"\\/"
_COMP_BAD = set(b'\\/:*?"<>|\r\n\x00\'')
_USER_ANCHORS = (b"Users", b"USERS", b"users", b"Documents and Settings", b"documents and settings",
                 b"DOCUMENTS AND SETTINGS", b"DOCUME~1", b"docume~1",
                 b"Dokumente und Einstellungen", b"/home/")
_PROFILE_ROOTS = (b"DOCUME~1", b"docume~1", b"Documents and Settings", b"documents and settings",
                  b"DOCUMENTS AND SETTINGS", b"Dokumente und Einstellungen")


def _component(buf, j, n, maxlen=64):
    """Return end offset of a path component starting at j (must be followed by
    a separator), or -1."""
    k = j
    lim = min(n, j + maxlen + 1)
    while k < lim:
        c = buf[k]
        if c in _COMP_BAD or c < 0x20:
            break
        k += 1
    if k == j or k >= n or k - j > maxlen:
        return -1
    if buf[k] not in _SEP:
        return -1
    return k


def scan_userpaths(buf, n=None, lo=0):
    """ASCII/UTF-8 user-name components after Users\\, Documents and Settings\\,
    /home/ ... Returns Regions (ctx id, label userpath.user)."""
    n = len(buf) if n is None else n
    out = []
    seen = set()
    for anc in _USER_ANCHORS:
        i = buf.find(anc, lo, n)
        while i >= 0:
            if anc == b"/home/":
                j = i + len(anc)
                ok = True
            else:
                ok = i > 0 and buf[i - 1] in _SEP
                if ok and anc in _PROFILE_ROOTS:
                    # only directly under a drive root; "...\\<user>\\DOCUME~1" is the
                    # user's own Documents folder, not the profile directory
                    k0 = i - 1
                    while k0 > 0 and buf[k0 - 1] in _SEP:
                        k0 -= 1
                    ok = k0 > 0 and buf[k0 - 1] == 0x3A
                j = i + len(anc)
                # one or more separators (doubled in STEP/JSON/C strings)
                k = j
                while k < n and k - j < 4 and buf[k] in _SEP:
                    k += 1
                ok = ok and k > j
                j = k
            if ok and j not in seen:
                e = _component(buf, j, n)
                if e > 0:
                    seen.add(j)
                    out.append(Region(j, e, "userpath.user", "id"))
            i = buf.find(anc, i + 1, n)
    return out


_U16_USER = re.compile(
    r"(?i)(?:(?<=[\\/])Users|(?<=:[\\/])(?:Documents and Settings|DOCUME~1))[\\/]{1,4}"
    r"([^\\/:*?\"<>|\r\n\x00']{1,64})(?=[\\/])")


def scan_userpaths_utf16(buf, n=None, lo=0):
    n = len(buf) if n is None else n
    out = []
    seen = set()
    for anc in (b"U\x00s\x00e\x00r\x00s\x00", b"U\x00S\x00E\x00R\x00S\x00", b"u\x00s\x00e\x00r\x00s\x00",
                b"D\x00o\x00c\x00u\x00m\x00e\x00n\x00t\x00s\x00 \x00a\x00n\x00d\x00",
                b"D\x00O\x00C\x00U\x00M\x00E\x00~\x001\x00"):
        i = buf.find(anc, lo, n)
        while i >= 0:
            a = max(lo, i - 4)
            win = bytes(buf[a:min(n, i + 420)])
            if len(win) % 2:
                win = win[:-1]
            txt = win.decode("utf-16-le", "replace")
            m = _U16_USER.search(txt)
            if m and m.start() <= 2:
                s = a + 2 * m.start(1)
                e = a + 2 * m.end(1)
                if s not in seen:
                    seen.add(s)
                    out.append(Region(s, e, "userpath.user", "id", "utf-16-le"))
            i = buf.find(anc, i + 2, n)
    return out


# --------------------------------------------------------------------------
# e-mail addresses and URLs
# --------------------------------------------------------------------------
_LOCAL_OK = set(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-")
_DOM_OK = set(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")


def _host_end(buf, j, n):
    k = j
    lim = min(n, j + 255)
    while k < lim and buf[k] in _DOM_OK:
        k += 1
    while k > j and buf[k - 1] in b".-":
        k -= 1
    return k


def valid_host(host):
    """host: str. True when it looks like a real DNS name with a known TLD."""
    labels = host.lower().split(".")
    if len(labels) < 2 or any(not l or len(l) > 63 for l in labels):
        return False
    if labels[-1] not in TLDS:
        return False
    return any(c.isalpha() for c in labels[-2])


def scan_emails(buf, n=None, lo=0, limit=20000):
    n = len(buf) if n is None else n
    out = []
    i = buf.find(b"@", lo, n)
    count = 0
    while i >= 0:
        count += 1
        if count > limit:
            break
        s = i
        while s > lo and i - s < 64 and buf[s - 1] in _LOCAL_OK:
            s -= 1
        while s < i and buf[s] in b".-+%_":
            s += 1
        e = _host_end(buf, i + 1, n)
        if s < i and e > i + 3:
            host = bytes(buf[i + 1:e]).decode("latin-1")
            if valid_host(host):
                out.append(Region(s, i, "email.local", "id"))
                out.append(Region(i + 1, e, "email.domain", "text"))
        i = buf.find(b"@", i + 1, n)
    return out


_URL_ANCHORS = (b"http://", b"https://", b"HTTP://", b"HTTPS://", b"www.", b"WWW.")
_PATH_OK = set(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-~%+")
_FORGES = {"github.com", "www.github.com", "gitlab.com", "www.gitlab.com", "bitbucket.org",
           "raw.githubusercontent.com"}


def scan_urls(buf, n=None, lo=0, limit=20000):
    """Hosts of URLs (label url.host) and, for code forges, the owner / repo
    path components (labels url.forge.owner / url.forge.repo, ctx id)."""
    n = len(buf) if n is None else n
    out = []
    seen = set()
    count = 0
    for anc in _URL_ANCHORS:
        i = buf.find(anc, lo, n)
        while i >= 0:
            count += 1
            if count > limit:
                return out
            j = i + (len(anc) if anc.lower() != b"www." else 0)
            if j not in seen and not (anc.lower() == b"www." and i > 0 and buf[i - 1] in b"/."):
                e = _host_end(buf, j, n)
                host = bytes(buf[j:e]).decode("latin-1")
                if e > j + 3 and valid_host(host):
                    seen.add(j)
                    out.append(Region(j, e, "url.host", "text"))
                    if host.lower() in _FORGES and e < n and buf[e] == 0x2F:
                        comps = []
                        p = e + 1
                        for _ in range(2):
                            q = p
                            while q < n and q - p < 100 and buf[q] in _PATH_OK:
                                q += 1
                            if q == p:
                                break
                            comps.append((p, q))
                            if q >= n or buf[q] != 0x2F:
                                break
                            p = q + 1
                        for idx, (a, b) in enumerate(comps):
                            if b > a and buf[b - 4:b] == b".git":
                                b -= 4
                            out.append(Region(a, b, "url.forge.owner" if idx == 0 else "url.forge.repo", "id"))
            i = buf.find(anc, i + 1, n)
    return out
