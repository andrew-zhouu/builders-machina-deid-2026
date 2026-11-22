"""Visible-text replacement in PDF pages.

Text that is stored as text (not outlines or scans) is located with its exact
character boxes. A dictionary match is removed with a redaction limited to
those characters (images and line art are not touched) and the pseudonym is
drawn at the same baseline, size, colour and direction with a standard font.
The page is then re-read and every other word must still be present.
"""
import math

import re

import pymupdf

from . import regions as R


# Hook set by handlers.py when document scope is on: contact details
# (e-mail addresses, labelled telephone numbers) without a dictionary entry.
CONTACTS = None


def _line_chars(line):
    """Flatten a rawdict line into (text, chars) where chars[i] describes text[i]."""
    text = []
    chars = []
    for span in line.get("spans", ()):
        for ch in span.get("chars", ()):
            c = ch.get("c", "")
            if not c:
                continue
            text.append(c)
            chars.append((ch["bbox"], ch["origin"], span.get("size", 8.0), span.get("color", 0),
                          span.get("alpha", 255)))
    text = "".join(text)
    return text, _upright(text, chars, line.get("dir", (1.0, 0.0)))


def _upright(text, chars, direction):
    """The characters of a line with boxes as wide as their advances.

    Slanted lettering is drawn with a sheared text matrix, and the box of a
    sheared character is wider than the character: it reaches under its
    left neighbour and over its right one. Positions and widths taken from
    such boxes put a redrawn word too far left and make it too wide, so the
    words of the line run into each other. Here the boxes of such a line are
    cut back to the advance of each character (from the pen position to the
    pen position plus the box width less the lean). Lines of upright text,
    and lines that run at an angle, are returned as they are."""
    dx, dy = direction
    if not chars or (abs(dx) < 0.999 and abs(dy) < 0.999):
        return chars
    try:
        lean = _lean(text, chars, direction)
        size = chars[0][2] or 8.0
        if lean < 0.05 * size:
            return chars
        out = []
        for ch in chars:
            x0, y0, x1, y1 = ch[0]
            a0, a1 = _along(ch[0], direction)
            pen = _pen(ch, direction)
            if not a0 - 0.01 <= pen <= a1 + 0.01:
                return chars                      # not the geometry this correction is for
            n0 = pen
            n1 = pen + max(a1 - a0 - lean, 0.02 * size)
            if abs(dx) >= 0.999:
                box = (n0, y0, n1, y1) if dx > 0 else (-n1, y0, -n0, y1)
            else:
                box = (x0, n0, x1, n1) if dy > 0 else (x0, -n1, x1, -n0)
            out.append((box,) + tuple(ch[1:]))
        return out
    except (IndexError, TypeError, ValueError):
        return chars


def _along(bbox, direction):
    dx, dy = direction
    pts = (bbox[0] * dx + bbox[1] * dy, bbox[2] * dx + bbox[1] * dy,
           bbox[0] * dx + bbox[3] * dy, bbox[2] * dx + bbox[3] * dy)
    return min(pts), max(pts)


def _pen(ch, direction):
    """Where the pen stood when the character was drawn, along the line."""
    return ch[1][0] * direction[0] + ch[1][1] * direction[1]


def _lean(text, chars, direction):
    """By how much the character boxes of a line are wider than the
    characters' advances. Upright text: nothing. Slanted lettering (the usual
    style on engineering drawings, drawn with a sheared text matrix): every
    box leans over its neighbours by the same amount, which is found here
    from pairs of adjacent letters."""
    v = []
    for k in range(len(text) - 1):
        if text[k] != " " and text[k + 1] != " ":
            step = _pen(chars[k + 1], direction) - _pen(chars[k], direction)
            if step > 0:
                a0, a1 = _along(chars[k][0], direction)
                v.append((a1 - a0) - step)
    if not v:
        return 0.0
    v.sort()
    return max(0.0, v[len(v) // 2])


def _fake_boundary(text, chars, s, e, direction):
    """True when the match [s, e) is cut out of a longer word only by a
    space that the text extraction made up: some fonts draw "fi" as one
    glyph, and the extracted text then reads "defi ned" although the letters
    touch on the page. A real word gap has width; a made-up one has none.
    The gap is measured between character boxes less their lean: on a line
    of slanted lettering the boxes overlap across every real word gap."""
    try:
        size = chars[s][2] or 8.0
        lean = None
        if s >= 2 and text[s - 1] == " " and text[s - 2].isalnum():
            lean = _lean(text, chars, direction)
            _a0, a1 = _along(chars[s - 2][0], direction)
            b0, _b1 = _along(chars[s][0], direction)
            if b0 - a1 + lean < 0.1 * size:
                return True
        if e + 1 < len(text) and text[e] == " " and text[e + 1].isalnum():
            if lean is None:
                lean = _lean(text, chars, direction)
            _a0, a1 = _along(chars[e - 1][0], direction)
            b0, _b1 = _along(chars[e + 1][0], direction)
            if b0 - a1 + lean < 0.1 * size:
                return True
    except (IndexError, TypeError):
        return False
    return False


def _id_spans(text):
    """Identity-context spans inside a line: user names in paths, e-mail local parts."""
    raw = text.encode("utf-8", "replace")
    if len(raw) != len(text):
        return []
    out = [(r.start, r.end) for r in R.scan_userpaths(raw, len(raw))]
    out += [(r.start, r.end) for r in R.scan_emails(raw, len(raw), limit=50) if r.label == "email.local"]
    return sorted(out)


WRAPPED = True      # also read two consecutive lines of a text block as one (a name that wraps)

# what a value on the next line has to look like: an organisation with its
# legal form, initials with or without a surname, two or three capitalised
# words, or a telephone number
_TOLD = re.compile(
    r"(?:.{2,70}\b(?i:inc|ltd|llc|l\.l\.c|gmbh|corp|co|s\.l|s\.a|b\.v|a\.g|ag|plc|pty|oy|ab|srl|s\.r\.l|sas|kg|limited|incorporated|corporation)\b\.?"
    r"|(?:[A-Z\u00c0-\u00de]\.\s?){1,3}(?:[A-Z\u00c0-\u00de][\w'\u2019-]+)?"
    r"|[A-Z\u00c0-\u00de][a-z\u00df-\u00ff'\u2019-]+(?:\s(?:[A-Z\u00c0-\u00de]\.|[a-z]{2,3}))?\s[A-Z\u00c0-\u00de][a-z\u00df-\u00ff'\u2019-]+(?:\s[A-Z\u00c0-\u00de][a-z\u00df-\u00ff'\u2019-]+)?"
    r"|[A-Z\u00c0-\u00de]{2,}(?:\s[A-Z\u00c0-\u00de]\.)?\s[A-Z\u00c0-\u00de]{2,}"
    r"|[+(]?\d[\d\s()./-]{6,}\d)$")


def _wrapped(eng, first, second):
    """Replacements that only show when two consecutive lines of a block are
    read as one: a name of several words that wraps at the end of a line
    ("... Northfield" / "Harrow ..."), and a name whose telling context ends
    the line before ("... IS THE SOLE PROPERTY OF" / "<organisation>; ANY
    ..."). What is found is added to the two lines' own lists; a name that
    straddles the break is cut at the break, each line getting its part of
    the replacement."""
    line1, t1, _c1, spans1 = first
    line2, t2, _c2, spans2 = second
    d1 = line1.get("dir", (1.0, 0.0))
    d2 = line2.get("dir", (1.0, 0.0))
    if abs(d1[0] - d2[0]) > 0.01 or abs(d1[1] - d2[1]) > 0.01:
        return
    a = t1.rstrip()
    b = t2.lstrip()
    lead = len(t2) - len(b)
    if len(a) < 2 or len(b) < 2 or len(a) + len(b) > 600:
        return
    joined = a + " " + b
    cut = len(a)                      # index of the joining space
    found = eng.spans_text(joined, "text")
    if CONTACTS is not None:
        # a value that is told by the line before it (a label, "property
        # of") must look the part: the next line of a block is as often
        # another label or a heading
        for s, e, rep in CONTACTS(eng, joined, [(s, e) for s, e, _r in found]):
            if e <= cut or _TOLD.match(joined[max(s, cut + 1):e].strip()):
                found.append((s, e, rep))
    if not found:
        return

    def free(spans, s, e):
        return all(e <= x or s >= y for x, y, _r in spans)

    for s, e, rep in found:
        if e <= cut:
            if free(spans1, s, e):
                spans1.append((s, e, rep))
        elif s > cut:
            s2, e2 = s - cut - 1 + lead, e - cut - 1 + lead
            if free(spans2, s2, e2):
                spans2.append((s2, e2, rep))
        else:
            old = joined[s:e]
            k = cut - s               # characters of the match on the first line
            if len(rep) != len(old) or rep[k:k + 1] != " ":
                continue
            r1, r2 = rep[:k], rep[k + 1:]
            if r1 and r1 != old[:k] and free(spans1, s, cut):
                spans1.append((s, cut, r1))
            if r2 and r2 != old[k + 1:] and free(spans2, lead, lead + e - cut - 1):
                spans2.append((lead, lead + e - cut - 1, r2))


def page_plan(eng, page, lines=True):
    """List of (quad_rect, origin, size, color, direction, old, new, ...) for
    a page. With lines=False only the matched characters are redrawn (the
    fallback for pages whose lines cannot be drawn again word by word)."""
    plan = []
    lid = 0
    try:
        d = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)
    except Exception:
        return plan
    for block in d.get("blocks", ()):
        if block.get("type") != 0:
            continue
        rows = []
        for line in block.get("lines", ()):
            text, chars = _line_chars(line)
            if len(text) < 2 or not text.strip():
                rows.append(None)
                continue
            spans = eng.spans_text(text, "text")
            ids = _id_spans(text)
            for a, b in ids:
                for s, e, rep in eng.spans_text(text[a:b], "id"):
                    spans.append((a + s, a + e, rep))
            if CONTACTS is not None:
                spans.extend(CONTACTS(eng, text, [(s, e) for s, e, _r in spans]))
            rows.append([line, text, chars, spans])
        if WRAPPED:
            for k in range(len(rows) - 1):
                if rows[k] is not None and rows[k + 1] is not None:
                    _wrapped(eng, rows[k], rows[k + 1])
        for row in rows:
            if row is None:
                continue
            line, text, chars, spans = row
            if not spans:
                continue
            spans.sort()
            last = -1
            direction = line.get("dir", (1.0, 0.0))
            keep = []
            for s, e, rep in spans:
                if s < last:
                    continue
                if _fake_boundary(text, chars, s, e, direction):
                    continue        # "defi ned": a space that is not there on the page
                last = e
                keep.append((s, e, rep))
            if keep and lines:
                items = _line_items(text, chars, keep, direction)
            elif keep:
                items = [it for it in (_span_item(text, chars, s, e, rep, direction) for s, e, rep in keep) if it]
            else:
                items = []
            if items:
                lid += 1
                plan.extend(it[:11] + (lid,) for it in items)     # field 11: which line the item belongs to
    return plan


_HELV = []


def _text_width(text, font, size):
    """Width the text will have when drawn. The library's quick length
    function undercounts accented letters of the standard font, so that font
    is measured through its font object; the fallback font is drawn with
    fixed advances, which the quick function reports correctly."""
    if font == "helv":
        if not _HELV:
            _HELV.append(pymupdf.Font("helv"))
        return _HELV[0].text_length(text, fontsize=size)
    return pymupdf.get_text_length(text, fontname=font, fontsize=size)


_FALLBACK_PUNCT = frozenset("\u2018\u2019\u201c\u201d\u2013\u2014\u2122\u20ac")


def _font_of(ch):
    """Which of the two fonts draws this character: "h" (standard font,
    Latin-1), "c" (fallback font: Greek, Cyrillic, kana, CJK, typographic
    quotes and dashes), or None."""
    o = ord(ch)
    if 0x20 < o <= 0x7E or 0xA1 <= o <= 0xFF:
        return "h"
    if (ch in _FALLBACK_PUNCT or 0x0370 <= o <= 0x052F or 0x2E80 <= o <= 0x9FFF or 0xFF01 <= o <= 0xFF5E):
        return "c"
    return None


def _runs(word):
    """Split a word into runs drawn by one font each: [(start, end, font)],
    or None when a character can be drawn by neither font."""
    out = []
    for i, ch in enumerate(word):
        f = _font_of(ch)
        if f is None:
            return None
        if out and out[-1][2] == f:
            out[-1][1] = i + 1
        else:
            out.append([i, i + 1, f])
    return out


def _item(cs, direction, old, new, glued):
    x0 = min(c[0][0] for c in cs)
    y0 = min(c[0][1] for c in cs)
    x1 = max(c[0][2] for c in cs)
    y1 = max(c[0][3] for c in cs)
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return None
    # fields: rect, origin, size, colour, direction, old, new, character boxes,
    # fill the old width exactly, draw a space after it, opacity (0: the text
    # is an invisible layer, as over a scan or over text drawn as outlines)
    alpha = cs[0][4] if len(cs[0]) > 4 else 255
    return (pymupdf.Rect(x0, y0, x1, y1), cs[0][1], cs[0][2], cs[0][3], direction, old, new,
            [c[0] for c in cs], glued, False, alpha)


def _span_item(text, chars, s, e, rep, direction):
    """One replacement on its own: only the matched characters are redrawn."""
    cs = chars[s:e]
    if not cs:
        return None
    # Does the same word go on right after the match ("NAME_part-01",
    # "name@host")? Then the new text must end exactly where the old one
    # did, or a reader would see a space inside the word.
    glued = False
    if e < len(text) and not text[e].isspace() and e < len(chars):
        try:
            _a0, a1 = _along(cs[-1][0], direction)
            b0, _b1 = _along(chars[e][0], direction)
            glued = (b0 - a1) < 0.25 * max(1.0, cs[0][2] or 8.0)
        except (IndexError, TypeError):
            glued = False
    return _item(cs, direction, text[s:e], rep, glued)


def _line_items(text, chars, keep, direction):
    """Plan items for one text line with replacements `keep` [(s, e, new)].

    The whole line is drawn again, word by word, each word at the place and
    width of the original word, in reading order. A program that reads the
    page text in drawing order then still finds "label: value" together and
    a part number in one piece; replacing only the matched characters would
    leave a hole in the old line and the new text at the end of the page.
    A word the standard font cannot draw is left as it is (when unchanged)
    or gets only its matched characters replaced."""
    n = len(text)
    new = list(text)
    for s, e, rep in keep:
        if len(rep) != e - s:
            return [it for it in (_span_item(text, chars, a, b, r, direction) for a, b, r in keep) if it]
        new[s:e] = rep
    new = "".join(new)
    owner = [-1] * n
    for k, (s, e, _rep) in enumerate(keep):
        for i in range(s, e):
            owner[i] = k
    items = []
    i = 0
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        j = i
        while j < n and not text[j].isspace():
            j += 1
        hit = sorted(set(o for o in owner[i:j] if o >= 0))
        old_w, new_w = text[i:j], new[i:j]
        word_items = None
        runs = _runs(new_w) if len(chars) >= j else None
        if runs:
            # one text object per font run, each at the place of its own
            # characters; a space is drawn after the word when the line goes
            # on, so that two words set close together are still read as two
            word_items = []
            for a, b, _f in runs:
                it = _item(chars[i + a:i + b], direction, old_w[a:b], new_w[a:b], True)
                if it is None:
                    word_items = None
                    break
                word_items.append(it[:9] + (b == len(new_w) and j < n and text[j:].strip() != "",) + it[10:])
        if word_items:
            items.extend(word_items)
        elif hit:
            for k in hit:
                s, e, rep = keep[k]
                a, b = max(s, i), min(e, j)
                it = _span_item(text, chars, a, b, rep[a - s:b - s], direction)
                if it:
                    items.append(it)
        i = j
    if not any(it[5] != it[6] for it in items):
        return []
    return items


def _rgb(color):
    return ((color >> 16) & 255) / 255.0, ((color >> 8) & 255) / 255.0, (color & 255) / 255.0


def _words(page):
    return [w[4] for w in page.get_text("words")]


def _redact(page, marks):
    """Remove the characters under the marks (text only; images and line
    art stay). The marks are set and applied through the library's low-level
    calls: its own functions give every mark a unique name and a fresh
    appearance and look every mark up again when applying them, which costs
    time quadratic in the number of marks on a page (minutes for a page with
    thousands of them)."""
    if not marks:
        return
    fast = False
    rotation = 0
    try:
        mu = pymupdf.mupdf
        # like the library: marks are set on the unrotated page (their
        # coordinates are those of the unrotated page), then the rotation
        # is put back
        rotation = page.rotation
        if rotation:
            page.set_rotation(0)
        pp = page._pdf_page()
        for x0, y0, x1, y1 in marks:
            annot = mu.pdf_create_annot(pp, mu.PDF_ANNOT_REDACT)
            mu.pdf_set_annot_rect(annot, mu.fz_make_rect(x0, y0, x1, y1))
        fast = True
    except Exception:
        fast = False
    finally:
        if rotation:
            page.set_rotation(rotation)
    if fast:
        ok = page._apply_redactions(pymupdf.PDF_REDACT_TEXT_REMOVE, pymupdf.PDF_REDACT_IMAGE_NONE,
                                    pymupdf.PDF_REDACT_LINE_ART_NONE)
        if not ok:
            raise ValueError("redaction not applied")
        return
    for x0, y0, x1, y1 in marks:
        page.add_redact_annot(pymupdf.Rect(x0, y0, x1, y1), fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)


def apply_page(page, plan):
    """Apply a page plan. Returns number of replacements, raises ValueError if
    the page lost unrelated text."""
    before = _words(page)
    hints = None
    if INPLACE:
        try:
            from . import pdforder
            hints = pdforder.predecessors(page.parent, page, [g[:4] for g in _groups(plan, None)])
        except Exception:
            hints = None
    marks = []
    for item in plan:
        boxes = item[7] if len(item) > 7 else [item[0]]
        for bb in boxes:
            # A mark in the middle of each character box: it removes that
            # character whatever the direction of the text, and it cannot
            # reach a neighbouring character or line.
            x0, y0, x1, y1 = bb[0], bb[1], bb[2], bb[3]
            w, h = x1 - x0, y1 - y0
            if w <= 0 or h <= 0:
                continue
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            hw, hh = max(0.05, w * 0.2), max(0.05, h * 0.2)
            marks.append((cx - hw, cy - hh, cx + hw, cy + hh))
    _redact(page, marks)
    base_xrefs = None
    if INPLACE:
        try:
            if not page.is_wrapped:
                page.wrap_contents()      # what the first inserted text would do anyway
            base_xrefs = list(page.get_contents())
        except Exception:
            base_xrefs = None
    for item in plan:
        rect, origin, size, color, direction, old, new = item[:7]
        # the standard font draws Latin-1 only; Greek, Cyrillic, kana, CJK and
        # typographic punctuation need the fallback font (which in turn lacks
        # some accented letters)
        font = "china-s" if any(ord(c) > 0xFF for c in new) else "helv"
        width = _text_width(new, font, size)
        span = math.hypot(rect.width, rect.height) if abs(direction[0]) < 0.99 and abs(direction[1]) < 0.99 \
            else (rect.width if abs(direction[0]) >= 0.99 else rect.height)
        fs = size
        glued = len(item) > 8 and item[8]
        pt = pymupdf.Point(origin)
        mat = None
        if glued and width > 0 and (0.2 if font == "china-s" else 0.4) <= span / width <= 1.8:
            # the word goes on behind the match: stretch or squeeze the new
            # text sideways so that it fills the old place exactly
            mat = pymupdf.Matrix(span / width, 1.0)
        elif width > span * 0.97 and width > 0:
            # keep a little air after the new word: text drawn right up to the
            # next word would be read together with it
            fs = max(2.0, size * span * 0.97 / width)
        if abs(direction[0] - 1.0) > 1e-3 or abs(direction[1]) > 1e-3:
            ang = math.degrees(math.atan2(-direction[1], direction[0]))
            rot = pymupdf.Matrix(ang)
            mat = rot if mat is None else mat * rot
        morph = (pt, mat) if mat is not None else None
        shown = new + " " if (len(item) > 9 and item[9]) else new
        alpha = item[10] if len(item) > 10 else 255
        if alpha == 0:
            # the original is an invisible text layer: so is its replacement
            page.insert_text(pt, shown, fontname=font, fontsize=fs, color=_rgb(color), morph=morph, render_mode=3)
        elif alpha < 250:
            page.insert_text(pt, shown, fontname=font, fontsize=fs, color=_rgb(color), morph=morph,
                             fill_opacity=alpha / 255.0)
        else:
            page.insert_text(pt, shown, fontname=font, fontsize=fs, color=_rgb(color), morph=morph)
    after = _words(page)
    # every word that was not part of a replacement must survive
    import collections
    gone = collections.Counter(before) - collections.Counter(after)
    fresh = collections.Counter(after) - collections.Counter(before)
    allowed = collections.Counter()
    for item in plan:
        if item[5] != item[6]:
            for w in item[5].split():
                allowed[w] += 1
    rest = gone - _expand(allowed, before)
    # A word that only merged with its neighbour ("1." and "CAD" now read as
    # "1.CAD") or split in two is still on the page; a word whose letters
    # are gone is not.
    news = [w for w in fresh if len(w) >= 2]
    newset = set(fresh)
    lost = 0
    for w, c in rest.items():
        if any(w in x for x in news):
            continue
        if any(w.startswith(a) and w[len(a):] in newset for a in news if len(a) < len(w)):
            continue
        lost += c
    changed = sum(1 for item in plan if item[5] != item[6])
    if lost > max(2, changed):
        raise ValueError("page lost %d unrelated words" % lost)
    if INPLACE and base_xrefs:
        try:
            _in_place(page, plan, base_xrefs, after, hints)
        except Exception:
            pass
    return changed


INPLACE = False          # set by the PDF handler: move redrawn lines to where the old text stood
MOVED = [0, 0]           # lines moved / lines redrawn (statistics of the current process)


def _groups(plan, over):
    """The redrawn lines of a plan: [(origin, direction, size, length along
    the line, overlay streams)], consecutive items of one line together."""
    groups = []
    for k, item in enumerate(plan):
        lid = item[11] if len(item) > 11 else -k - 1
        x = [over[k]] if over is not None else []
        if groups and groups[-1][5] == lid:
            groups[-1][4].extend(x)
            groups[-1][6] = item[0]
            continue
        groups.append([tuple(item[1]), item[4], item[2], 0.0, x, lid, item[0]])
    out = []
    for org, direction, size, _l, xs, _lid, last_rect in groups:
        lo, hi = _along((last_rect.x0, last_rect.y0, last_rect.x1, last_rect.y1), direction)
        start = org[0] * direction[0] + org[1] * direction[1]
        out.append((org, direction, size, max(0.0, max(lo, hi) - start), xs))
    return out


def _in_place(page, plan, base_xrefs, words_after, hints=None):
    """Move the redrawn lines from the end of the page content to the place
    of the text they replace (pdforder.reorder). The page must look and read
    exactly as before the move, otherwise the move is undone."""
    import collections
    from . import pdforder
    doc = page.parent
    xrefs = list(page.get_contents())
    over = xrefs[len(base_xrefs):]
    if xrefs[:len(base_xrefs)] != base_xrefs or len(over) != len(plan):
        pdforder.WHY["skipped: unexpected content stream list"] += 1
        return
    out = _groups(plan, over)
    MOVED[1] += len(out)
    keep = dict((x, doc.xref_stream(x)) for x in xrefs)
    before = page.get_pixmap(dpi=40, colorspace=pymupdf.csGRAY).samples
    want = collections.Counter(words_after)

    def same():
        try:
            now = page.get_pixmap(dpi=40, colorspace=pymupdf.csGRAY).samples
            if now != before:
                # a handful of edge pixels may change when text and a line swap
                # their drawing order; a hidden or clipped word changes far more
                if len(now) != len(before) or sum(1 for u, v in zip(now, before) if abs(u - v) > 40) > 12:
                    return False
            return collections.Counter(_words(page)) == want
        except Exception:
            return False

    moved = 0
    for own_only in (False, True):
        moved = pdforder.reorder(doc, page, out, base_xrefs, hints, own_only)
        if not moved:
            break
        if same():
            break
        for x, data in keep.items():
            doc.update_stream(x, data if data is not None else b" ")
        pdforder.WHY["undone: the page looked or read differently" + (" (second try)" if own_only else "")] += moved
        moved = -1
    if moved < 0 and 2 <= len(out) <= 400:
        # all lines at once did not work: in halves, down to single lines,
        # keeping every part whose move leaves the page as it is
        budget = [48]

        def attempt(idx):
            snap = dict((x, doc.xref_stream(x)) for x in xrefs)
            sub = [out[i] for i in idx]
            sh = [hints[i] if i < len(hints) else None for i in idx] if hints is not None else None
            try:
                m = pdforder.reorder(doc, page, sub, base_xrefs, sh, False)
            except Exception:
                m = -1
            if m > 0 and same():
                return m
            if m != 0:
                for x, data in snap.items():
                    doc.update_stream(x, data if data is not None else b" ")
            return 0 if m == 0 else -1

        def solve(idx):
            if not idx or budget[0] <= 0:
                return 0
            budget[0] -= 1
            r = attempt(idx)
            if r >= 0 or len(idx) == 1:
                return max(r, 0)
            h = len(idx) // 2
            return solve(idx[:h]) + solve(idx[h:])

        h = len(out) // 2
        moved = solve(list(range(h))) + solve(list(range(h, len(out))))
        if moved:
            pdforder.WHY["moved in parts after the whole page failed"] += moved
    if moved <= 0:
        return
    MOVED[0] += moved


def _expand(allowed, before):
    """Words of the page that contain a replaced fragment are allowed to change."""
    import collections
    out = collections.Counter()
    frags = list(allowed)
    for w in set(before):
        for f in frags:
            if f and f in w:
                out[w] += before.count(w)
                break
    return out
