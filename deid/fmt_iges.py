"""IGES Global-section field location.

IGES is fixed format: 80-column records, section letter in column 73. Global
parameters are Hollerith strings (nHtext) that may wrap across records, so a
field is returned as a Region with an explicit offset map. Because every
replacement is the same length, Hollerith counts and record layout stay valid.
"""
from .regions import Region

# 1-based global parameter index -> (label, ctx)
_GLOBAL = {
    3: ("iges.product_id", "text"), 4: ("iges.file_name", "text"),
    12: ("iges.product_id_rx", "text"), 21: ("iges.author", "id"), 22: ("iges.org", "id"),
}


def _records(buf, n, limit=2000):
    """Yield (line_start, data_len, section_letter) for leading records."""
    pos = 0
    count = 0
    while pos < n and count < limit:
        nl = buf.find(b"\n", pos, min(n, pos + 200))
        end = n if nl < 0 else nl
        line_end = end
        if line_end > pos and buf[line_end - 1] == 0x0D:
            line_end -= 1
        ln = line_end - pos
        if ln >= 73:
            yield pos, 72, chr(buf[pos + 72])
        else:
            yield pos, min(ln, 72), "?"
        if nl < 0:
            break
        pos = nl + 1
        count += 1


def iges_regions(buf, n=None):
    n = len(buf) if n is None else n
    omap = []
    started = False
    for pos, dlen, sec in _records(buf, n):
        if sec == "G":
            started = True
            omap.extend(range(pos, pos + dlen))
        elif started:
            break
        elif sec not in ("S", "B", "C", "?"):
            break
    if not omap:
        return []
    data = bytes(buf[o] for o in omap)
    regs = []
    # parameter delimiter / record delimiter (params 1 and 2)
    pd, rd = b",", b";"
    i = 0
    if data[:2] == b"1H":
        pd = data[2:3]
        i = 3
    idx = 1
    L = len(data)
    # first param consumed specially above; now parse generally
    i = 0
    idx = 1
    while i < L and idx <= 26:
        # skip blanks
        j = i
        while j < L and data[j] == 0x20:
            j += 1
        # Hollerith?
        k = j
        while k < L and 0x30 <= data[k] <= 0x39:
            k += 1
        if k > j and k < L and data[k] == 0x48:  # 'H'
            cnt = int(data[j:k])
            s = k + 1
            e = s + cnt
            if e > L:
                break
            lab = _GLOBAL.get(idx)
            if lab and cnt > 0:
                regs.append(Region(omap[s], omap[e - 1] + 1, lab[0], lab[1], omap=omap[s:e]))
            if idx == 1 and cnt == 1:
                pd = data[s:e]
            if idx == 2 and cnt == 1:
                rd = data[s:e]
            i = e
        else:
            i = j
        # advance to delimiter
        while i < L and data[i:i + 1] not in (pd, rd):
            i += 1
        if i >= L or data[i:i + 1] == rd:
            break
        i += 1
        idx += 1
    return regs
