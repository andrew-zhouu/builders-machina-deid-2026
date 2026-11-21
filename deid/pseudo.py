"""Deterministic, length-preserving pseudonyms.

A canonical token (lower case, accents stripped, letters and digits only) maps
to a pronounceable token of exactly the same length: letters to letters of
the same script (Latin, Cyrillic, Greek, Hebrew, Arabic, kana, Hangul, Thai),
digits to digits, CJK ideographs to CJK ideographs. The mapping is a keyed
function (HMAC-SHA256 with a private key), so every worker derives the same
pseudonym for the same identity without shared state, and the mapping cannot
be reversed without the key.

`project` lays a pseudonym over an original surface form, keeping the
original's case, punctuation, spacing and encoded byte length.
"""
import hashlib
import hmac
import unicodedata

CONS = "bcdfghjklmnprstvz"
VOWS = "aeiou"
# frequent, neutral ideographs used for CJK tokens (all 3 bytes in UTF-8, 2 in GBK/UTF-16)
CJK_POOL = ("安白北本長城川春大東方風高光國海和華建江金京景靜開立林龍明南平青清秋山上生石水松"
            "太天田同文西新星興秀學陽永玉元遠雲正中竹子宇")

# Other scripts: (first code point, last code point, consonant pool, vowel pool
# or None). A letter is replaced by a letter of its own script, so the value
# keeps its byte length in UTF-8, UTF-16 and the script's legacy code pages.
# The pools hold only the basic letters every such code page has.
_SCRIPTS = (
    (0x0430, 0x045F, "бвгджзклмнпрстфхцчш", "аеиоу"),                       # Cyrillic
    (0x0410, 0x042F, "бвгджзклмнпрстфхцчш", "аеиоу"),
    (0x03AC, 0x03CE, "βγδζθκλμνξπρστφχψ", "αεηιου"),                         # Greek
    (0x0391, 0x03A9, "βγδζθκλμνξπρστφχψ", "αεηιου"),
    (0x05D0, 0x05EA, "אבגדהוזחטיכלמנסעפצקרשת", None),                         # Hebrew
    (0x0621, 0x064A, "ابتثجحخدذرزسشصضطظعغفقكلمنهوي", None),                 # Arabic
    (0x3041, 0x3096, "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわ", None),
    (0x30A1, 0x30FA, "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワ", None),
    (0xAC00, 0xD7A3, "가나다라마바사아자차카타파하고노도로모보소오조구누두루무부수우주", None),   # Hangul
    (0x0E01, 0x0E2E, "กขคงจชซดตถทนบปผฝพฟมยรลวสหอ", None),                      # Thai
)


def _script(ch):
    """Index into _SCRIPTS for a non-ASCII letter, or -1."""
    o = ord(ch)
    if o < 0x0391:
        return -1
    for k, (a, b, _c, _v) in enumerate(_SCRIPTS):
        if a <= o <= b:
            return k
    return -1


_SPECIAL = {"ß": "s", "æ": "a", "œ": "o", "ø": "o", "đ": "d", "ł": "l", "ı": "i", "þ": "t", "ð": "d",
            "Æ": "a", "Œ": "o", "Ø": "o", "Đ": "d", "Ł": "l", "Þ": "t", "Ð": "d"}


def canon_char(ch):
    """Canonical form of one character, or '' when it is not a token char."""
    o = ord(ch)
    if o < 128:
        if ch.isalnum():
            return ch.lower()
        return ""
    if ch in _SPECIAL:
        return _SPECIAL[ch]
    if not ch.isalnum():
        return ""
    d = unicodedata.decomposition(ch)
    if d and not d.startswith("<"):
        base = chr(int(d.split()[0], 16))
        if ord(base) < 128 and base.isalpha():
            return base.lower()
    low = ch.lower()
    return low if len(low) == 1 else ch


def tokens_of(surface):
    """Split a surface string into canonical tokens (runs of token chars)."""
    out = []
    cur = []
    for ch in surface:
        c = canon_char(ch)
        if c:
            cur.append(c)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def canon(surface):
    return "".join(canon_char(ch) for ch in surface)


def _is_cjk(ch):
    o = ord(ch)
    return 0x3400 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF


class Pseudonymizer:
    def __init__(self, key, table=None):
        if isinstance(key, str):
            key = key.encode("utf-8")
        self.key = key
        self.table = table or {}
        self._cache = {}

    def _stream(self, token, counter):
        seed = hmac.new(self.key, ("%d\x00%s" % (counter, token)).encode("utf-8"), hashlib.sha256).digest()
        i = 0
        block = seed
        n = 0
        while True:
            if i >= len(block):
                n += 1
                block = hashlib.sha256(seed + n.to_bytes(4, "big")).digest()
                i = 0
            yield block[i]
            i += 1

    def derive(self, token, counter=0):
        """Raw derivation (no collision table)."""
        rnd = self._stream(token, counter)
        out = []
        n = len(token)
        i = 0
        while i < n:
            ch = token[i]
            if "a" <= ch <= "z":
                j = i
                while j < n and "a" <= token[j] <= "z":
                    j += 1
                run = j - i
                if run == 1:
                    out.append("abcdefghijklmnopqrstuvwxyz"[next(rnd) % 26])
                else:
                    cons = (next(rnd) % 10) < 7
                    for _ in range(run):
                        pool = CONS if cons else VOWS
                        out.append(pool[next(rnd) % len(pool)])
                        cons = not cons
                i = j
            elif "0" <= ch <= "9":
                out.append("0123456789"[next(rnd) % 10])
                i += 1
            elif _is_cjk(ch):
                out.append(CJK_POOL[next(rnd) % len(CJK_POOL)])
                i += 1
            else:
                k = _script(ch) if ch >= "\u0391" else -1
                if k < 0:
                    out.append(ch)
                    i += 1
                    continue
                _a, _b, cpool, vpool = _SCRIPTS[k]
                j = i
                while j < n and token[j] >= "\u0391" and _script(token[j]) == k:
                    j += 1
                run = j - i
                if vpool is None or run == 1:
                    pool = cpool + (vpool or "")
                    for _ in range(run):
                        out.append(pool[next(rnd) % len(pool)])
                else:
                    cons = (next(rnd) % 10) < 7
                    for _ in range(run):
                        pool = cpool if cons else vpool
                        out.append(pool[next(rnd) % len(pool)])
                        cons = not cons
                i = j
        return "".join(out)

    def token(self, token):
        """Pseudonym for a canonical token (table first, then derivation)."""
        t = self.table.get(token)
        if t is not None:
            return t
        t = self._cache.get(token)
        if t is None:
            c = 0
            t = self.derive(token, c)
            while t == token and c < 8:
                c += 1
                t = self.derive(token, c)
            self._cache[token] = t
        return t

    def build_table(self, tokens, reserved=()):
        """Assign collision-free pseudonyms to a set of canonical tokens.
        Deterministic: tokens are processed in sorted order."""
        originals = set(tokens) | set(reserved)
        used = set()
        table = {}
        for tok in sorted(set(tokens)):
            c = 0
            while True:
                p = self.derive(tok, c)
                if p != tok and p not in used and p not in originals:
                    break
                c += 1
                if c > 500:
                    # tiny value space (a single letter or digit, all of them
                    # taken): any value other than the token itself will do
                    c = 0
                    p = self.derive(tok, 0)
                    while p == tok and c < 500:
                        c += 1
                        p = self.derive(tok, c)
                    break
            used.add(p)
            table[tok] = p
        self.table = table
        return table


def _shape(orig, pc):
    """Give pseudonym char `pc` the letter case of original char `orig`.
    Accented originals become plain letters, so one identity has one
    pseudonym spelling in every encoding."""
    if pc >= "\u0080":      # non-ASCII pseudonym char (another script's pool, CJK pool, or passthrough)
        if orig.isupper():
            up = pc.upper()
            if len(up) == 1:
                return up
        return pc
    return pc.upper() if orig.isupper() else pc


def project(surface, pseudo):
    """Overlay pseudonym characters on the token characters of `surface`.
    `pseudo` must hold exactly one char per token char of surface."""
    out = []
    k = 0
    for ch in surface:
        if canon_char(ch):
            out.append(_shape(ch, pseudo[k]))
            k += 1
        else:
            out.append(ch)
    if k != len(pseudo):
        raise ValueError("projection length mismatch")
    return "".join(out)
