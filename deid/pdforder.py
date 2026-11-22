"""Put redrawn PDF text back where the original text stood in the page content.

pdftext.apply_page removes the old glyphs and draws the new words in extra
content streams at the end of the page. That is right for the eye and for
programs that read text by position, but a program that reads the page in
drawing order then finds a gap where the line was and the new line after
everything else. This module moves each redrawn line into the original
content stream, next to what is left of the line it replaces (or, for a line
that left nothing behind, after its neighbour on the same baseline), so the
drawing order of the page text stays what it was.

Nothing is drawn differently: the same text objects are executed with the
same matrices, only earlier. The caller compares the rendered page before
and after and keeps the appended version when anything differs.
"""
import pikepdf

import collections

WHY = collections.Counter()       # why lines were or were not moved (statistics of the current process)
_SHOW = ("Tj", "TJ", "'", '"')
_POSITION = ("Tm", "Td", "TD", "T*")
_ID = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mul(m, n):
    """m then n (row vectors)."""
    return (m[0] * n[0] + m[1] * n[2], m[0] * n[1] + m[1] * n[3],
            m[2] * n[0] + m[3] * n[2], m[2] * n[1] + m[3] * n[3],
            m[4] * n[0] + m[5] * n[2] + n[4], m[4] * n[1] + m[5] * n[3] + n[5])


def _inv(m):
    det = m[0] * m[3] - m[1] * m[2]
    if abs(det) < 1e-12:
        return None
    return (m[3] / det, -m[1] / det, -m[2] / det, m[0] / det,
            (m[2] * m[5] - m[3] * m[4]) / det, (m[1] * m[4] - m[0] * m[5]) / det)


def _nums(operands, n):
    if len(operands) != n:
        raise ValueError("operand count")
    return tuple(float(x) for x in operands)


def _scan(ins):
    """Walk the instructions. Returns the text-showing operations as dicts:
    i (index), bt (index of the BT), et (index of the ET, filled in later),
    prefix (index after the previous show of the same text object),
    first (first show of its object), positioned (a positioning operator
    stands between prefix and i), tlm (line matrix at prefix), ctm (at i),
    org (device origin of the text matrix when the text is shown)."""
    ctm = _ID
    stack = []
    tm = tlm = _ID
    lead = 0.0
    shows = []
    bt = None
    prefix = None
    tlm_at_prefix = _ID
    first = True
    positioned = False
    open_shows = []
    ctm_bt = ctm_prefix = _ID
    clips = []            # for each open q level: [index of its q, shows drawn under a clip set at this level]
    clipped = []          # per open q level: was a clipping path set at this level
    for i, it in enumerate(ins):
        op = str(it.operator)
        if op == "q":
            stack.append(ctm)
            clips.append([])
            clipped.append(False)
        elif op == "Q":
            if stack:
                ctm = stack.pop()
            if clips:
                waiting = clips.pop()
                was = clipped.pop()
                if was:
                    # everything drawn under this level's clip leaves the clip here
                    for s in waiting:
                        s["unclip"] = i + 1
                        s["ctm_unclip"] = ctm
                elif clips:
                    clips[-1].extend(waiting)
        elif op in ("W", "W*"):
            if clipped:
                clipped[-1] = True
        elif op == "cm":
            ctm = _mul(_nums(it.operands, 6), ctm)
        elif op == "BT":
            bt = i
            tm = tlm = _ID
            prefix = i + 1
            tlm_at_prefix = tlm
            ctm_bt = ctm_prefix = ctm
            first = True
            positioned = False
            open_shows = []
        elif op == "ET":
            for s in open_shows:
                s["et"] = i
                s["ctm_et"] = ctm
            bt = None
            open_shows = []
        elif bt is None:
            continue
        elif op == "Tm":
            tm = tlm = _nums(it.operands, 6)
            positioned = True
        elif op in ("Td", "TD"):
            tx, ty = _nums(it.operands, 2)
            if op == "TD":
                lead = -ty
            tlm = _mul((1.0, 0.0, 0.0, 1.0, tx, ty), tlm)
            tm = tlm
            positioned = True
        elif op == "TL":
            lead = _nums(it.operands, 1)[0]
        elif op == "T*":
            tlm = _mul((1.0, 0.0, 0.0, 1.0, 0.0, -lead), tlm)
            tm = tlm
            positioned = True
        elif op in _SHOW:
            if op in ("'", '"'):
                tlm = _mul((1.0, 0.0, 0.0, 1.0, 0.0, -lead), tlm)
                tm = tlm
                positioned = True
            dev = _mul(tm, ctm)
            s = {"i": i, "bt": bt, "et": None, "prefix": prefix, "first": first, "positioned": positioned,
                 "tlm": tlm_at_prefix, "ctm": ctm, "ctm_bt": ctm_bt, "ctm_prefix": ctm_prefix,
                 "org": (dev[4], dev[5])}
            shows.append(s)
            open_shows.append(s)
            if clips:
                # resolved when an enclosing level that set a clip closes; the
                # outermost such level wins because it is written last
                for level in clips:
                    level.append(s)
            prefix = i + 1
            tlm_at_prefix = tlm
            ctm_prefix = ctm
            first = False
            positioned = False
    return [s for s in shows if s["et"] is not None]


def _op(name, *operands):
    return pikepdf.ContentStreamInstruction(list(operands), pikepdf.Operator(name))


def _num(x):
    return pikepdf.Object.parse(("%.6f" % x).rstrip("0").rstrip(".").encode() or b"0")


def _wrap(holder, ctm, overlay_bytes):
    """Instructions that draw the overlay as it was drawn at the end of the
    page: outside any transformation or text state left by the page."""
    inv = _inv(ctm)
    if inv is None:
        return None
    out = [_op("q"), _op("cm", *[_num(v) for v in inv]),
           _op("Tc", _num(0)), _op("Tw", _num(0)), _op("Tz", _num(100)), _op("Ts", _num(0)), _op("Tr", _num(0))]
    for data in overlay_bytes:
        out.extend(pikepdf.parse_content_stream(holder.make_stream(data)))
    out.append(_op("Q"))
    return out


def _shows_of(doc, page, xrefs):
    """(instructions, text-showing operations with page coordinates "pt")
    of the page content made of these streams."""
    data = b"\n".join(doc.xref_stream(x) or b"" for x in xrefs)
    holder = pikepdf.new()
    ins = list(pikepdf.parse_content_stream(holder.make_stream(data)))
    shows = _scan(ins)
    # PDF space -> the coordinates text extraction and text insertion use
    # (the unrotated page, also when the page has a /Rotate entry)
    tmat = page.transformation_matrix
    for s in shows:
        x, y = s["org"]
        s["pt"] = (x * tmat.a + y * tmat.c + tmat.e, x * tmat.b + y * tmat.d + tmat.f)
    return holder, ins, shows


def _on_line(shows, org, direction, size, length):
    """Show operations that start on the baseline of a line: (along, show)
    for those within the line's own extent, and for those left of it."""
    dx, dy = direction
    tol = max(1.0, 0.35 * (size or 8.0))
    own, left = [], []
    for s in shows:
        px, py = s["pt"][0] - org[0], s["pt"][1] - org[1]
        if abs(px * dy - py * dx) > tol:
            continue
        a = px * dx + py * dy
        if -tol <= a <= length + tol:
            own.append((a, s))
        elif a < -tol:
            left.append((a, s))
    return own, left


def predecessors(doc, page, lines, depth=8):
    """Before anything is removed: for each line (origin, direction, size,
    length) the start points of the text operations that are drawn just
    before the line's own first operation, nearest first. They tell where in
    the drawing order the line stood even when nothing of it is left."""
    if not lines:
        return []
    try:
        _holder, _ins, shows = _shows_of(doc, page, list(page.get_contents()))
    except Exception:
        return [[] for _ in lines]
    out = []
    for org, direction, size, length in lines:
        own, _left = _on_line(shows, org, direction, size, length)
        if not own:
            out.append([])
            continue
        first = min(s["i"] for _a, s in own)
        k = max((n for n, s in enumerate(shows) if s["i"] < first), default=-1)
        out.append([shows[n]["pt"] for n in range(k, max(-1, k - depth), -1)])
    return out


def reorder(doc, page, groups, base_xrefs, hints=None, own_only=False):
    """groups: [(origin (x, y) in page coordinates, direction (dx, dy),
    size, length along the line, [xref of each overlay stream])], one per
    redrawn line, in drawing order. base_xrefs: the content streams that
    were there before the overlays. hints: per group, the result of
    predecessors() taken before the old text was removed. Returns the number
    of groups moved. The page content is rewritten only when at least one
    group moves. own_only: move only lines of which something is left on the
    page (the safest kind of move: same place, same clip)."""
    if not groups or not base_xrefs:
        return 0
    holder, ins, shows = _shows_of(doc, page, base_xrefs)
    if not shows:
        WHY["no text operations left in the page content"] += len(groups)
        return 0
    grid = {}
    for s in shows:
        grid.setdefault((round(s["pt"][0]), round(s["pt"][1])), []).append(s)

    def find(pt):
        best = None
        for gx in (round(pt[0]) - 1, round(pt[0]), round(pt[0]) + 1):
            for gy in (round(pt[1]) - 1, round(pt[1]), round(pt[1]) + 1):
                for s in grid.get((gx, gy), ()):
                    d = abs(s["pt"][0] - pt[0]) + abs(s["pt"][1] - pt[1])
                    if d < 0.3 and (best is None or s["i"] > best["i"]):
                        best = s
        return best

    def after(s):
        """Insertion point right after show operation s, or None. When s
        is drawn inside a clipping path (a table cell, say), the point is
        where that clip ends: the moved line belongs to another place on
        the page and would be cut away inside it."""
        if s.get("unclip") is not None:
            return (s["unclip"], None, s["ctm_unclip"])
        nxt = [t for t in by_bt.get(s["bt"], ()) if t["i"] > s["i"]]
        if not nxt:
            return (s["et"] + 1, None, s["ctm_et"])
        if nxt[0]["positioned"]:
            return (nxt[0]["prefix"], nxt[0]["tlm"], nxt[0]["ctm_prefix"])
        return None

    by_bt = {}
    for s in shows:
        by_bt.setdefault(s["bt"], []).append(s)
    inserts = {}           # instruction index -> [instructions], inserted before that index
    moved = []
    for g, (org, direction, size, length, xrefs) in enumerate(groups):
        own, left = _on_line(shows, org, direction, size, length)
        data = [doc.xref_stream(x) or b"" for x in xrefs]
        target = None
        only_own_failed = False
        if own:
            # what is left of this very line: the new words go right before it
            s = min(own, key=lambda t: t[1]["i"])[1]
            if s["first"]:
                target = (s["bt"], None, s["ctm_bt"])
            elif s["positioned"]:
                target = (s["prefix"], s["tlm"], s["ctm_prefix"])
            only_own_failed = target is None
        if target is None and own_only:
            WHY["not moved: second try moves only lines with something left"] += 1
            continue
        if target is None and hints is not None and g < len(hints):
            # nothing is left of the line: after the text that was drawn
            # just before it, as far as that text is still there
            for pt in hints[g]:
                s = find(pt)
                if s is not None:
                    target = after(s)
                    if target is not None:
                        break
        how = None
        if target is not None:
            how = "before what is left of the line" if (own and not only_own_failed) else "after the preceding text"
        if target is None and left:
            # otherwise after its neighbour to the left on the same baseline
            s = max(left, key=lambda t: (t[0], t[1]["i"]))[1]
            target = after(s)
            if target is not None:
                how = "after the neighbour to the left"
        if target is None:
            if own:
                WHY["not moved: rest of the line continues an unpositioned run"] += 1
            elif hints is not None and g < len(hints) and hints[g]:
                WHY["not moved: preceding text not found again"] += 1
            elif hints is not None and g < len(hints):
                WHY["not moved: line not found in the page content (form or unusual layout)"] += 1
            else:
                WHY["not moved: no anchor"] += 1
            continue
        WHY["moved: " + (how or "after the preceding text")] += 1
        idx, tlm, ctm = target
        block = _wrap(holder, ctm, data)
        if block is None:
            continue
        if tlm is not None:
            # inside a text object: close it, draw, open it again where it was
            block = [_op("ET")] + block + [_op("BT"), _op("Tm", *[_num(v) for v in tlm])]
        inserts.setdefault(idx, []).append((g, block))
        moved.append(g)
    if not moved:
        return 0
    out = []
    for i, it in enumerate(ins):
        for _g, block in inserts.get(i, ()):
            out.extend(block)
        out.append(it)
    for _g, block in inserts.get(len(ins), ()):
        out.extend(block)
    new = pikepdf.unparse_content_stream(out)
    doc.update_stream(base_xrefs[0], new)
    for x in base_xrefs[1:]:
        doc.update_stream(x, b" ")
    for g in moved:
        for x in groups[g][4]:
            doc.update_stream(x, b" ")
    return len(moved)
