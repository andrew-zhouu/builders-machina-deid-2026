"""Bring the XMP packet of processed PDFs in line with their document information.

A value of the information dictionary that the build replaced (a title carrying a drawing number, a subject,
keywords) usually stands a second time in the XMP metadata packet, and so does the document's own name.
The reading by exclusion changed the information dictionary and the file name only; this step puts the same
new values into the packet: exact copies of an old value (plain or XML-escaped), and every whole word that
the build replaced in the title or in the file name (codes with a digit, names of five letters and more).
Files are rewritten through a temporary file; nothing else in them changes.

Usage: xmp_sync.py OUT_CORPUS JOURNAL [--jobs N] [--dry]     (journal: fields p, o, k, c; last record wins)"""
import collections, html, json, os, sys, multiprocessing as mp
import pikepdf
OUT, JOURNAL = sys.argv[1:3]
DRY = '--dry' in sys.argv
JOBS = 8
if '--jobs' in sys.argv:
    JOBS = int(sys.argv[sys.argv.index('--jobs') + 1])
INP = '/home/ubuntu/machina/input'


def info_of(pdf):
    out = {}
    try:
        info = pdf.trailer.get('/Info')
        if isinstance(info, pikepdf.Dictionary):
            for k in list(info.keys()):
                try:
                    v = info[k]
                except Exception:
                    continue
                if isinstance(v, pikepdf.String):
                    out[str(k)] = str(v)
    except Exception:
        pass
    return out


def _runs(s):
    """(start, end) of the maximal runs of ASCII letters and digits."""
    out = []
    a = None
    for i, ch in enumerate(s):
        if ch.isascii() and ch.isalnum():
            if a is None:
                a = i
        elif a is not None:
            out.append((a, i))
            a = None
    if a is not None:
        out.append((a, len(s)))
    return out


def token_pairs(old, new, into):
    """Words that the build replaced, read off an old and a new value of equal shape: {old word (lower case):
    new word (lower case)}. Only words that tell something: a digit in them, four characters and more, or
    letters only, five and more."""
    if len(old) != len(new) or old == new:
        return
    for a, b in _runs(old):
        x, y = old[a:b], new[a:b]
        if x == y or not (y.isascii() and y.isalnum()):
            continue
        if (any(c.isdigit() for c in x) and len(x) >= 4) or (x.isalpha() and len(x) >= 5):
            into.setdefault(x.lower(), y.lower())


def by_words(text, words):
    """`text` with every whole word of `words` replaced (letter case of the place kept)."""
    out = []
    pos = 0
    hit = False
    for a, b in _runs(text):
        w = text[a:b]
        y = words.get(w.lower())
        if y is not None and len(y) == len(w):
            out.append(text[pos:a])
            out.append(''.join(c.upper() if o.isupper() else c for o, c in zip(w, y)))
            pos = b
            hit = True
    if not hit:
        return text
    out.append(text[pos:])
    return ''.join(out)


def one(item):
    rel, o = item
    src = os.path.join(INP, rel)
    dst = os.path.join(OUT, o)
    try:
        with pikepdf.open(src) as a:
            ia = info_of(a)
    except Exception:
        ia = {}
    try:
        pdf = pikepdf.open(dst)
    except Exception:
        return 'output unreadable', 0
    try:
        ib = info_of(pdf)
        pairs = [(ia[k], ib[k]) for k in ia if k in ib and ia[k] != ib[k] and len(ia[k].strip()) >= 3
                 and k not in ('/CreationDate', '/ModDate')]
        words = {}
        for a_, b_ in pairs:
            token_pairs(a_, b_, words)
        sa = os.path.splitext(os.path.basename(rel))[0]
        sb = os.path.splitext(os.path.basename(o))[0]
        token_pairs(sa, sb, words)
        if not pairs and not words:
            return 'nothing replaced in title or name', 0
        try:
            md = pdf.Root.get('/Metadata')
        except Exception:
            md = None
        if md is None:
            return 'no XMP packet', 0
        try:
            raw = md.read_bytes()
        except Exception:
            return 'XMP packet unreadable', 0
        upd = raw
        pairs.sort(key=lambda t: -len(t[0]))
        for old, new in pairs:
            for esc in (lambda x: x, html.escape, lambda x: html.escape(x, quote=False)):
                a = esc(old).encode('utf-8')
                if a and a in upd:
                    upd = upd.replace(a, esc(new).encode('utf-8'))
        if words:
            try:
                t = upd.decode('utf-8')
                t2 = by_words(t, words)
                if t2 != t:
                    upd = t2.encode('utf-8')
            except UnicodeDecodeError:
                pass
        if upd == raw:
            return 'XMP packet already in line', 0
        if DRY:
            return 'would change', 1
        md.write(upd)
        tmp = dst + '.xmp~'
        pdf.save(tmp, encryption=False, fix_metadata_version=False, deterministic_id=True)
        pdf.close()
        pdf = None
        with pikepdf.open(tmp) as chk:
            if len(chk.pages) < 0:
                raise ValueError('unreadable')
        os.replace(tmp, dst)
        return 'changed', 1
    except Exception as ex:
        try:
            os.unlink(dst + '.xmp~')
        except OSError:
            pass
        return 'error %s' % type(ex).__name__, 0
    finally:
        if pdf is not None:
            pdf.close()


if __name__ == '__main__':
    recs = {}
    for l in open(JOURNAL, encoding='utf-8'):
        try:
            r = json.loads(l)
        except Exception:
            continue
        recs[r['p']] = r
    items = [(p, r.get('o') or p) for p, r in recs.items() if r['k'] == 'pdf' and r.get('c')]
    c = collections.Counter()
    with mp.Pool(JOBS) as pool:
        for what, n in pool.imap_unordered(one, items, chunksize=16):
            c[what] += 1
    print('changed PDFs looked at:', len(items))
    for k, v in c.most_common():
        print('   %-52s %d' % (k, v))
