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
            if not spans:
                continue
            spans.sort()
            last = -1
            direction = line.get("dir", (1.0, 0.0))
            for s, e, rep in spans:
                if s < last:
                    continue
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
                plan.append((pymupdf.Rect(x0, y0, x1, y1), cs[0][1], cs[0][2], cs[0][3], direction,
                             text[s:e], rep))
    return plan


def _rgb(color):
    return ((color >> 16) & 255) / 255.0, ((color >> 8) & 255) / 255.0, (color & 255) / 255.0


def _words(page):
    return [w[4] for w in page.get_text("words")]


def apply_page(page, plan):
    """Apply a page plan. Returns number of replacements, raises ValueError if
    the page lost unrelated text."""
    before = _words(page)
    for rect, _o, _sz, _col, _dir, _old, _new in plan:
        r = pymupdf.Rect(rect)
        # shrink slightly so neighbouring glyphs are not caught
        dx = min(0.6, r.width * 0.02)
        dy = r.height * 0.18
        r = pymupdf.Rect(r.x0 + dx, r.y0 + dy, r.x1 - dx, r.y1 - dy)
        page.add_redact_annot(r, fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    for rect, origin, size, color, direction, old, new in plan:
        font = "helv"
        if any(ord(c) > 0x2E7F for c in new):
            font = "china-s"
        width = pymupdf.get_text_length(new, fontname=font, fontsize=size)
        span = math.hypot(rect.width, rect.height) if abs(direction[0]) < 0.99 and abs(direction[1]) < 0.99 \
            else (rect.width if abs(direction[0]) >= 0.99 else rect.height)
        fs = size
        if width > span * 1.02 and width > 0:
            fs = max(2.0, size * span / width)
        pt = pymupdf.Point(origin)
        morph = None
        if abs(direction[0] - 1.0) > 1e-3 or abs(direction[1]) > 1e-3:
            ang = math.degrees(math.atan2(-direction[1], direction[0]))
            morph = (pt, pymupdf.Matrix(ang))
        page.insert_text(pt, new, fontname=font, fontsize=fs, color=_rgb(color), morph=morph)
    after = _words(page)
    # every word that was not part of a replacement must survive
    import collections
    gone = collections.Counter(before) - collections.Counter(after)
    allowed = collections.Counter()
    for _r, _o, _s, _c, _d, old, _new in plan:
        for w in old.split():
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
