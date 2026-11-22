"""Names by exclusion: the "whitelist" reading of drawing text.

The dictionary names what is known to be an identity. A drawing says much
more about where it comes from than the names in its title block: its
drawing and part numbers, the project and product names in its titles, the
organisations and people it mentions in passing. This module takes the
opposite view for the places where anything out of the ordinary is likely to
identify: the words of a PDF page, of its metadata and of its file name.

A word is kept when it is ordinary vocabulary (English and several other
languages, engineering terms), a number, a dimension, a standard or
material designation, or very short. Every other word gets its token
pseudonym: letters for letters, digits for digits, the same pseudonym
wherever the same word stands, so a drawing number reads the same in the
title block, in the metadata title, in the file name and in the name of the
model the drawing shows.
"""
import re

from . import pseudo

_STATE = {}


def _vocab():
    if not _STATE:
        from .build_dict import Lists, LARGE_WORDS, FOREIGN_WORDS
        L = Lists()
        _STATE["L"] = L
        _STATE["large"] = LARGE_WORDS
        _STATE["foreign"] = FOREIGN_WORDS
        # words of the generic-value list; software and vendor names are not
        # part of it (they are names like any other here)
        from .build_dict import _load_lines
        _STATE["generic"] = set(t for x in _load_lines("generic_values.txt") for t in pseudo.tokens_of(x))
        # trade vocabulary by spread: words that stand in the drawings of many unrelated sources
        _STATE["common4"] = set(x for x in _load_lines("tokens_common4.txt") if not x.startswith("#"))
        _STATE["upper3"] = set(x for x in _load_lines("tokens_upper3.txt") if not x.startswith("#"))
    return _STATE


_SUFFIXES = ("s", "es", "ed", "d", "ing", "er", "ers", "ly", "e", "en", "n", "r", "nen", "em", "te", "ten", "ung",
             "ungen", "a", "o", "os", "as", "ado", "ada", "ados", "adas", "ido", "ida", "cion", "ciones")


def known(w):
    """Ordinary vocabulary in a broad sense."""
    V = _vocab()
    L = V["L"]
    if w in V["large"] or w in V["foreign"] or w in L.words or w in L.eng or w in L.struct or w in L.calendar \
            or w in V["generic"] or w in V["common4"]:
        return True
    return False


_ABBREV = frozenset("""appv apvd appvd chkd dwn drn drwn dsgnd partnumber partname partno filename drawingnumber sheetnumber
dwgno itemno docno lastsaved lastsavedby createdby checkedby approvedby drawnby baseplate endplate backplate faceplate
sideplate topplate handwheel flywheel crankshaft camshaft driveshaft gearbox toolholder workpiece endmill hotend heatsink
heatbed endstop standoffs footprint setscrews leadscrew ballscrew linearrail coldend extruder bowden nozzle idler
tensioner microcontroller breadboard pinout datasheet stepper servo arduino firmware bootloader gcode
shcs bhcs fhcs sshs fhsc bhsc hrpo crs hrs unc unf unef nptf bspp bspt thru cbore csink csk
typ ref max min nom dia rad thk od id pcd bcd tol gdt mmc lmc rfs tir fim rms ptfe pvc hdpe ldpe uhmw uhmwpe pmma petg
epdm viton nbr fkm delrin nylon acetal teflon kapton mylar alodine galv hbw qty assy dwg rev eco ecn bom sht proj appd
chkd dsgn engr mfg matl descr calc misc tbd tba nts sstl stl alum anod matl req reqd spec specs approx equiv dims dim
ctrs ctr centre centres colour deburr deburred chamf rnd sq hex soc hd cap scr washer lockwasher flatwasher locknut
setscrew standoff dowel circlip oring gasket bushing bearing sprocket pulley pinion spur helical keyway spline flange
manifold weldment subassy subassembly weld welded tig mig braze brazed solder knurl knurled tapped reamed bored milled
turned ground lapped honed polished plated coated painted powdercoat anodise anodised passivated hardened tempered
normalized annealed quenched carburized nitrided zinc nickel chrome cadmium phosphate oxide blk nat clr
schraube mutter scheibe welle nabe lager buchse stift feder bohrung gewinde fase radius senkung passung blech platte
profil rohr flansch deckel gehause halter trager winkel leiste bolzen niet keil dichtung
vis ecrou rondelle arbre palier douille goupille ressort alesage filetage chanfrein rayon tole plaque tube bride
tornillo tuerca arandela eje cojinete casquillo pasador muelle taladro rosca chaflan radio chapa placa tubo brida
""".split())


_CACHE = {}


def _known_form(w):
    """A known word, an inflected form of one, or two known words written
    as one (the usual way of German and Dutch technical vocabulary)."""
    r = _CACHE.get(w)
    if r is None:
        r = _known_form_slow(w)
        if len(_CACHE) < 400000:
            _CACHE[w] = r
    return r


def _known_form_slow(w):
    if known(w) or w in _ABBREV:
        return True
    n = len(w)
    for suf in _SUFFIXES:
        k = len(suf)
        if n - k >= 4 and w.endswith(suf) and known(w[:n - k]):
            return True
    if n >= 8:
        for i in range(4, n - 3):
            head = w[:i]
            if not known(head):
                continue
            tail = w[i:]
            for cut in ("", "s", "n", "en", "es", "e"):
                if cut and not tail.startswith(cut):
                    continue
                rest = tail[len(cut):]
                if len(rest) >= 4 and (known(rest) or any(rest.endswith(sf) and len(rest) - len(sf) >= 4
                                                           and known(rest[:len(rest) - len(sf)]) for sf in _SUFFIXES)):
                    return True
    return False


_CAMEL = re.compile(r"[A-ZÀ-Þ]+(?![a-zß-ÿ])|[A-ZÀ-Þ]?[a-zß-ÿ]+")

_UNITS = ("mm|cm|dm|km|in|inch|ft|deg|kg|mg|lb|lbs|oz|nm|ncm|kn|mpa|gpa|kpa|psi|bar|mbar|pa|hz|khz|mhz|ghz|rpm|"
          "ma|mah|ah|kw|mw|kwh|vdc|vac|va|ohm|kohm|uf|nf|pf|mh|uh|ms|ns|us|sec|min|hr|hrs|pcs|pc|ea|x|th|st|nd|rd|"
          "d|v|a|w|k|c|f|g|n|m|s|h|l|t|u|p|um|ga|awg|mil|ml|dpi|fps|bit|gb|mb|kb|tpi|hp|cc|z|zz|rs")
_CODE_KEEP = re.compile(
    r"^(?:"
    r"\d{1,4}(?:" + _UNITS + r")"                  # 10mm, 24v, 23t, 3d, 2x, 608zz
    r"|[a-z]\d{1,3}"                                # m6, r20, h7, t6, a4, g54
    r"|[a-z]{2}\d{1,2}"                             # sp5, js6, ra63, hr55, pa12
    r"|\d{1,2}[a-z]{1,2}\d{0,2}"                    # 2d, 4k7, 3rd
    r"|\d+x\d+(?:x\d+)*[a-z]{0,2}"                  # 20x20, 156x156x76, 3x5mm
    r"|m\d+x\d+(?:x\d+)?"                           # m3x10
    r"|\d+x[a-z]\d+"                                # 4xm3
    r"|(?:iso|din|en|bs|jis|astm|ansi|asme|sae|aisi|nema|awg|ral|ip|ul|mil|iec|ieee|usb|rs|hdmi|rj|cat|gb|gost|"
    r"uni|nf|nfe|vdi|sus|ss|aa|aw|pa|pla|abs|petg|pom|hss|hrc|hb|hv|ra|rz|it|gt|htd|mxl|xl|ansi|unc|unf|npt|bsp|"
    r"led|pcb|cnc|pwm|adc|dac|gpio|uart|spi|rgb|lcd|nfc|rfid|utf)\d+[a-z]{0,2}\d{0,2}"
    r"|\d{4}t\d{1,4}"                               # 6061t6
    r"|(?:[a-z]?\d{1,3})?(?:cr|ni|mo|mn|si|al|mg|cu|zn|ti|pb|sn|nb|co|v|w){2,}\d{0,4}[a-z]{0,2}\d{0,2}"   # 42crmo4
    r"|[cse]\d{2,3}[a-z]{0,2}\d?"                   # c45e, s235jr
    r"|(?:v|ver|rev|r|sp|sr|mk|no|nr|fig|pos|typ|opt)\d+[a-z]?"
    r"|x+\d*|\d*x+"                                 # X.XXX placeholders
    r"|(?:19|20)\d\d[a-z]?"
    r")$")
_HEX_ID = re.compile(r"^[0-9a-f]{10,40}$")


def is_name(c, word, before, after):
    """True when the word (canonical form c, as written `word`) is to be
    replaced. `before` and `after` are the characters around it."""
    n = len(c)
    if n <= 2:
        return False
    if any(ord(ch) > 0x24F for ch in word):
        return False                      # other scripts are not read by exclusion
    digits = sum(1 for ch in c if "0" <= ch <= "9")
    if digits == n:
        if n < 5:
            return False
        # decimal numbers, digit groups and times are values
        if before[-1:] in ".,:" and before[-2:-1].isdigit():
            return False
        if after[:1] in ".,:" and after[1:2].isdigit():
            return False
        if n == 8 and c[:2] in ("19", "20") and "01" <= c[4:6] <= "12" and "01" <= c[6:8] <= "31":
            return False                  # a date
        if n == 6 and ("01" <= c[2:4] <= "12" and "01" <= c[4:6] <= "31" and c[:2] in ("19", "20", "21", "22", "23",
                                                                                         "24", "25", "26")):
            return False                  # a short date at the head of a file name (220527)
        return True
    if digits:
        if n <= 3:
            return False
        if _CODE_KEEP.match(c):
            return False
        if _HEX_ID.match(c) and digits >= 2 and n - digits >= 2:
            return False                  # a content hash put in front of a file name
        base = c.strip("0123456789")
        if base and base.isalpha() and len(base) >= 3 and digits <= 2 and _known_form(base):
            return False                  # "sheet1", "axis3", "module12"
        return True
    if n == 3:
        # three capitals: initials or the short name of an organisation,
        # unless the word is common (REV, DWG, ISO: in the drawings of many
        # unrelated sources) or an ordinary word written in capitals
        if not word.isupper() or not word.isascii():
            return False
        V = _vocab()
        return not (c in V["upper3"] or c in _ABBREV or c in _STD or known(c))
    if n < 3:
        return False
    if _known_form(c):
        return False
    # a compound written in camel case stays when all its parts are ordinary
    parts = _CAMEL.findall(word)
    if len(parts) >= 2 and "".join(parts) == word:
        long = [pseudo.canon(p) for p in parts if len(p) >= 4]
        if long and all(_known_form(p) for p in long):
            return False
    return True


_PERM = {}


def _digits(eng, run):
    """Pseudonym of a run of digits. A short run goes through a keyed
    permutation of all numbers of its length: two different numbers never
    share a pseudonym and no number keeps its own (the token pseudonym of a
    two-digit number would collide with one in a hundred others). Longer
    runs, and runs the dictionary knows, take their token pseudonym."""
    n = len(run)
    if n > 5 or run in eng.pz.table:
        return eng.pz.token(run)
    perm = _PERM.get(n)
    if perm is None:
        import hashlib
        import hmac
        import random
        seed = hmac.new(eng.pz.key, b"digit runs of %d" % n, hashlib.sha256).digest()
        rnd = random.Random(seed)
        size = 10 ** n
        perm = list(range(size))
        rnd.shuffle(perm)
        for i in range(size):
            if perm[i] == i:
                j = (i + 1) % size
                perm[i], perm[j] = perm[j], perm[i]
        _PERM[n] = perm
    return "%0*d" % (n, perm[int(run)])


def _mapped(eng, word, c):
    """The word with its pseudonym. A word of letters only is replaced as a
    whole. A code of letters and digits is replaced run by run (every run of
    two or more digits, every run of two or more letters), so that a number
    keeps its pseudonym whatever is written around it: "482915" in a title
    block, "482915A" as a file name and "482915.SLDPRT" as the model all
    show the same new number."""
    chars = []
    for ch in word:
        cc = pseudo.canon_char(ch)
        if not cc:
            chars.append((ch, None))
        elif len(cc) != 1:
            return None
        else:
            chars.append((ch, cc))
    if c.isalpha() or c.isdigit():
        ps = _digits(eng, c) if c.isdigit() and c.isascii() else eng.pz.token(c)
        if len(ps) != len(c):
            return None
        out = []
        q = 0
        for ch, cc in chars:
            if cc is None:
                out.append(ch)
            else:
                out.append(pseudo._shape(ch, ps[q]))
                q += 1
        return "".join(out)
    out = []
    i = 0
    n = len(chars)
    while i < n:
        ch, cc = chars[i]
        if cc is None:
            out.append(ch)
            i += 1
            continue
        kind = cc.isdigit()
        j = i
        while j < n and chars[j][1] is not None and chars[j][1].isdigit() == kind:
            j += 1
        run = "".join(x[1] for x in chars[i:j])
        if len(run) >= 2 and (kind or not (run in _STD or (len(run) >= 3 and known(run)))):
            ps = _digits(eng, run) if kind and run.isascii() else eng.pz.token(run)
            if len(ps) != len(run):
                return None
            for q in range(i, j):
                out.append(pseudo._shape(chars[q][0], ps[q - i]))
        else:
            out.extend(x[0] for x in chars[i:j])
        i = j
    return "".join(out)


def _runs(text):
    """(start, end) of every maximal run of token characters."""
    out = []
    s = None
    for i, ch in enumerate(text):
        if pseudo.canon_char(ch):
            if s is None:
                s = i
        elif s is not None:
            out.append((s, i))
            s = None
    if s is not None:
        out.append((s, len(text)))
    return out


_STD = frozenset("""iso din en bs jis astm ansi asme sae aisi nema awg ral ip ul mil iec ieee usb rs hdmi rj cat gb
gost uni nf nfe vdi sus ss aa aw pa pla abs petg pom hss hrc hb hv ra rz it gt htd mxl xl unc unf npt bsp led pcb cnc
pwm rev ver sheet page fig no nr pos item typ type size step rgb lcd utf pt dc ac""".split())
_NORMS = frozenset("iso din en bs jis astm ansi asme sae aisi nema ral ul mil iec ieee gb gost uni nf nfe vdi ams nas "
                   "csa awwa api ipc jedec vde onorm sn sis une abnt nbr is ks cns".split())
_DATE = re.compile(r"^(?:(?:19|20)\d\d[-_](?:0?[1-9]|1[0-2])[-_](?:0?[1-9]|[12]\d|3[01])"
                   r"|(?:0?[1-9]|[12]\d|3[01])[-_](?:0?[1-9]|[12]\d|3[01])[-_](?:(?:19|20)\d\d|\d\d))$")


def spans(eng, text, taken=()):
    """(start, end, replacement) for the words of `text` that are replaced
    by exclusion and that do not overlap the spans in `taken`."""
    if not text or len(text) > 20000:
        return []
    runs = _runs(text)
    if not runs:
        return []
    want = set()
    # digit groups joined by hyphens ("555-0100-321") form one number
    k = 0
    while k < len(runs):
        j = k
        while (j + 1 < len(runs) and runs[j + 1][0] == runs[j][1] + 1 and text[runs[j][1]] in "-_"
               and text[runs[j][0]:runs[j][1]].isdigit() and text[runs[j + 1][0]:runs[j + 1][1]].isdigit()):
            j += 1
        if j > k and text[runs[k][0]:runs[k][1]].isdigit():
            whole = text[runs[k][0]:runs[j][1]]
            nd = sum(1 for ch in whole if ch.isdigit())
            before = text[max(0, runs[k][0] - 2):runs[k][0]]
            after = text[runs[j][1]:runs[j][1] + 2]
            if (nd >= 7 and not _DATE.match(whole) and not (before[-1:] in ".," and before[-2:-1].isdigit())
                    and not (after[:1] in ".," and after[1:2].isdigit())):
                for m in range(k, j + 1):
                    want.add(m)
        k = j + 1
    # part codes written as groups ("ABC-0012-A", "XY-QRS-003", "KL-MN-00"):
    # groups joined by hyphens or underscores, with digits in one of them
    # and a letter group that is no word and no standard in another
    k = 0
    while k < len(runs):
        j = k
        while j + 1 < len(runs) and runs[j + 1][0] == runs[j][1] + 1 and text[runs[j][1]] in "-_":
            j += 1
        if j > k:
            groups = [pseudo.canon(text[a:b]) for a, b in runs[k:j + 1]]
            has_digits = any(g.isdigit() and len(g) >= 2 for g in groups) or any(
                any(ch.isdigit() for ch in g) and not g.isdigit() and len(g) >= 4 for g in groups)
            letters = [g for g in groups if g.isalpha()]
            code = []
            for m in range(k, j + 1):
                g = groups[m - k]
                if not g.isalpha() or g in _STD:
                    continue
                word = text[runs[m][0]:runs[m][1]]
                if len(g) >= 4:
                    if is_name(g, word, "", ""):
                        code.append(g)
                elif not known(g) and (len(g) == 3 or (len(g) == 2 and len(groups) >= 3)):
                    # whatever its letter case: "ab_cde_003_p_001" as a file
                    # name and "AB_CDE_003_P_001" as a label are one code
                    code.append(g)
            if has_digits and code and not any(g in _STD for g in letters):
                for m in range(k, j + 1):
                    g = groups[m - k]
                    if (g.isdigit() and len(g) >= 2) or (g in code) or (not g.isdigit() and not g.isalpha() and len(g) >= 4
                                                                         and not _CODE_KEEP.match(g)):
                        want.add(m)
        k = j + 1
    out = []
    tk = sorted(taken)
    prev = ""
    for idx, (s, e) in enumerate(runs):
        last, prev = prev, pseudo.canon(text[s:e])
        if any(s < y and e > x for x, y, *_r in tk):
            continue
        word = text[s:e]
        c = prev
        if not c:
            continue
        if c.isdigit() and last in _NORMS and e - s <= 6 and idx not in want and text[runs[idx - 1][1]:s].strip(" -:/.") == "":
            continue                      # the number of a standard ("EN 22768", "ISO 13920")
        if idx in want:
            if len(c) < 2:
                continue
        elif not is_name(c, word, text[max(0, s - 2):s], text[e:e + 2]):
            continue
        rep = _mapped(eng, word, c)
        if rep is None:
            continue
        if rep != word:
            out.append((s, e, rep))
    return out


def apply(eng, text, taken=()):
    """`text` with its out-of-the-ordinary words replaced."""
    sp = spans(eng, text, taken)
    if not sp:
        return text
    parts = []
    pos = 0
    for s, e, rep in sp:
        parts.append(text[pos:s])
        parts.append(rep)
        pos = e
    parts.append(text[pos:])
    return "".join(parts)
