"""Visible-text replacement in PDF pages.

Text that is stored as text (not outlines or scans) is located with its exact
character boxes. A dictionary match is removed with a redaction limited to
those characters (images and line art are not touched) and the pseudonym is
drawn at the same baseline, size, colour and direction with a standard font.
The page is then re-read and every other word must still be present.
"""
import math

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
            chars.append((ch["bbox"], ch["origin"], span.get("size", 8.0), span.get("color", 0)))
    return "".join(text), chars


def _along(bbox, direction):
    dx, dy = direction
    pts = (bbox[0] * dx + bbox[1] * dy, bbox[2] * dx + bbox[1] * dy,
           bbox[0] * dx + bbox[3] * dy, bbox[2] * dx + bbox[3] * dy)
    return min(pts), max(pts)


def _fake_boundary(text, chars, s, e, direction):
    """True when the match [s, e) is cut out of a longer word only by a
    space that the text extraction made up: some fonts draw "fi" as one
    glyph, and the extracted text then reads "defi ned" although the letters
    touch on the page. A real word gap has width; a made-up one has none."""
    try:
        size = chars[s][2] or 8.0
        if s >= 2 and text[s - 1] == " " and text[s - 2].isalnum():
            _a0, a1 = _along(chars[s - 2][0], direction)
            b0, _b1 = _along(chars[s][0], direction)
            if b0 - a1 < 0.1 * size:
                return True
        if e + 1 < len(text) and text[e] == " " and text[e + 1].isalnum():
            _a0, a1 = _along(chars[e - 1][0], direction)
            b0, _b1 = _along(chars[e + 1][0], direction)
            if b0 - a1 < 0.1 * size:
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


def page_plan(eng, page):
    """List of (quad_rect, origin, size, color, direction, old, new) for a page."""
    plan = []
    try:
        d = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)
    except Exception:
        return plan
    for block in d.get("blocks", ()):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", ()):
            text, chars = _line_chars(line)
            if len(text) < 2 or not text.strip():
                continue
            spans = eng.spans_text(text, "text")
            ids = _id_spans(text)
            for a, b in ids:
                for s, e, rep in eng.spans_text(text[a:b], "id"):
                    spans.append((a + s, a + e, rep))
            if CONTACTS is not None:
                spans.extend(CONTACTS(eng, text, [(s, e) for s, e, _r in spans]))
            if not spans:
                continue
            spans.sort()
            last = -1
            direction = line.get("dir", (1.0, 0.0))
            for s, e, rep in spans:
                if s < last:
                    continue
                if _fake_boundary(text, chars, s, e, direction):
                    continue        # "defi ned": a space that is not there on the page
                last = e
                cs = chars[s:e]
                if not cs:
                    continue
                x0 = min(c[0][0] for c in cs)
                y0 = min(c[0][1] for c in cs)
                x1 = max(c[0][2] for c in cs)
                y1 = max(c[0][3] for c in cs)
                if x1 - x0 <= 0 or y1 - y0 <= 0:
                    continue
                # Does the same word go on right after the match ("NAME_part-01",
                # "name@host")? Then the new text must end exactly where the
                # old one did, or a reader would see a space inside the word.
                glued = False
                if e < len(text) and not text[e].isspace() and e < len(chars):
                    try:
                        _a0, a1 = _along(cs[-1][0], direction)
                        b0, _b1 = _along(chars[e][0], direction)
                        glued = (b0 - a1) < 0.25 * max(1.0, cs[0][2] or 8.0)
                    except (IndexError, TypeError):
                        glued = False
                plan.append((pymupdf.Rect(x0, y0, x1, y1), cs[0][1], cs[0][2], cs[0][3], direction,
                             text[s:e], rep, [c[0] for c in cs], glued))
    return plan


def _rgb(color):
    return ((color >> 16) & 255) / 255.0, ((color >> 8) & 255) / 255.0, (color & 255) / 255.0


def _words(page):
    return [w[4] for w in page.get_text("words")]


def apply_page(page, plan):
    """Apply a page plan. Returns number of replacements, raises ValueError if
    the page lost unrelated text."""
    before = _words(page)
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
            page.add_redact_annot(pymupdf.Rect(cx - hw, cy - hh, cx + hw, cy + hh), fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    for item in plan:
        rect, origin, size, color, direction, old, new = item[:7]
        font = "helv"
        if any(ord(c) > 0x2E7F for c in new):
            font = "china-s"
        width = pymupdf.get_text_length(new, fontname=font, fontsize=size)
        span = math.hypot(rect.width, rect.height) if abs(direction[0]) < 0.99 and abs(direction[1]) < 0.99 \
            else (rect.width if abs(direction[0]) >= 0.99 else rect.height)
        fs = size
        glued = len(item) > 8 and item[8]
        pt = pymupdf.Point(origin)
        mat = None
        if glued and width > 0 and 0.55 <= span / width <= 1.8:
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
        page.insert_text(pt, new, fontname=font, fontsize=fs, color=_rgb(color), morph=morph)
    after = _words(page)
    # every word that was not part of a replacement must survive
    import collections
    gone = collections.Counter(before) - collections.Counter(after)
    allowed = collections.Counter()
    for item in plan:
        for w in item[5].split():
            allowed[w] += 1
    lost = sum((gone - _expand(allowed, before)).values())
    if lost > max(2, len(plan)):
        raise ValueError("page lost %d unrelated words" % lost)
    return len(plan)


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
