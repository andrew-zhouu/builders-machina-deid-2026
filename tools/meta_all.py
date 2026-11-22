"""Every descriptive value of a PDF's metadata is treated as identifying as a whole.

The build replaces identities inside the metadata (author, company, last-saved-by; names and codes inside
a title) and leaves the other words. This step gives word-by-word pseudonyms to all descriptive values:
title, subject, keywords, custom text fields and the name of the producing software, in the information
dictionary and in the XMP packet. The new value is computed from the input's value, so one value gets one
new value in every document. Identity fields and dates stay as the build wrote them. A file is rewritten
through a temporary file and then put in place of the old directory entry (a hard-linked copy of the same
file in another tree is not touched).

Usage: meta_all.py OUT_CORPUS JOURNAL DICT [--jobs N] [--dry] [--paths FILE]
       (JOURNAL: fields p, o, k, c; last record wins; rewritten with c=1 and metaall=<values> for changed files)"""
import html, json, os, re, subprocess, sys, multiprocessing as mp
sys.path.insert(0, os.environ.get('DEID_CODE', '/home/ubuntu/machina/work/code_attemptx1'))
import pikepdf
from deid import engine as E, handlers as H, pseudo

OUT, JOURNAL, DICT = sys.argv[1:4]
DRY = '--dry' in sys.argv
JOBS = int(sys.argv[sys.argv.index('--jobs') + 1]) if '--jobs' in sys.argv else 8
ONLY = None
if '--paths' in sys.argv:
    ONLY = set(l.rstrip('\n') for l in open(sys.argv[sys.argv.index('--paths') + 1], encoding='utf-8') if l.strip())
INP = '/home/ubuntu/machina/input'
KEYFILE = '/home/ubuntu/machina/work/secret.key'
DATES = ('/CreationDate', '/ModDate', '/Trapped', '/SourceModified')
SHIFT = '--dates' in sys.argv          # also move the dates of creation and modification (keyed, per value)
ENG = None
_PDFDATE = re.compile(r"^(D:)?(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?(.*)$", re.S)
_ISODATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?")


def _offset(text):
    """Keyed shift for one calendar day: 30 to 400 days and some seconds back. Every date of the same day
    moves by the same amount, so the dates of one working day keep their order and their distance."""
    import datetime, hashlib, hmac
    h = hmac.new(ENG.pz.key, b"pdf date " + text.encode("utf-8", "replace"), hashlib.sha256).digest()
    return datetime.timedelta(days=30 + int.from_bytes(h[:4], "big") % 371, seconds=int.from_bytes(h[4:8], "big") % 86400)


def _day_key(y, mo, d):
    return "%04d%02d%02d" % (y, mo, d)


def shift_pdf_date(v):
    """A date of the information dictionary (D:YYYYMMDDHHmmSS...) moved back; None when it is not such a date."""
    import datetime
    m = _PDFDATE.match(v.strip())
    if not m:
        return None
    try:
        y, mo, d = int(m.group(2)), int(m.group(3)), int(m.group(4))
        hh = int(m.group(5) or 0); mi = int(m.group(6) or 0); ss = int(m.group(7) or 0)
        t = datetime.datetime(y, mo, d, min(hh, 23), min(mi, 59), min(ss, 59))
    except ValueError:
        return None
    t2 = t - _offset(_day_key(y, mo, d))
    out = (m.group(1) or "") + "%04d%02d%02d" % (t2.year, t2.month, t2.day)
    if m.group(5) is not None:
        out += "%02d" % t2.hour
    if m.group(6) is not None:
        out += "%02d" % t2.minute
    if m.group(7) is not None:
        out += "%02d" % t2.second
    return out + m.group(8)


def shift_iso_dates(raw):
    """The ISO dates of an XMP packet (bytes) moved back by the same rule."""
    import datetime

    def one(m):
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            t = datetime.datetime(y, mo, d, int(m.group(4)), int(m.group(5)), int(m.group(6) or 0))
        except ValueError:
            return m.group(0)
        t2 = t - _offset(_day_key(y, mo, d))
        s = "%04d-%02d-%02dT%02d:%02d" % (t2.year, t2.month, t2.day, t2.hour, t2.minute)
        if m.group(6) is not None:
            s += ":%02d" % t2.second
        return s

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    new = _ISODATE.sub(one, text)
    return new.encode("utf-8") if new != text else raw


def _init():
    global ENG
    ENG = E.Engine(DICT, open(KEYFILE, 'rb').read())


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


def packets(pdf):
    """The XMP packets of a document: the catalog's first, then those attached to other objects."""
    out = []
    seen = set()
    try:
        md = pdf.Root.get('/Metadata')
        if isinstance(md, pikepdf.Stream):
            out.append(md)
            seen.add(md.objgen)
    except Exception:
        pass
    try:
        count = 0
        for obj in pdf.objects:
            count += 1
            if count > 400000:
                break
            try:
                if (isinstance(obj, pikepdf.Stream) and obj.objgen not in seen
                        and str(obj.stream_dict.get('/Type')) == '/Metadata'):
                    out.append(obj)
                    seen.add(obj.objgen)
            except Exception:
                continue
    except Exception:
        pass
    return out


def desc_nodes(raw):
    """Text nodes of the descriptive elements of a packet."""
    out = []
    for m in H._XMP_DESC.finditer(raw):
        inner = m.group(3)
        if b'<' in inner:
            for t in H._XMP_TEXT.finditer(b'>' + inner + b'<'):
                out.append(t.group(1))
        elif inner.strip():
            out.append(inner)
    return out


_NUMERIC = re.compile(r'[\d\sT:.+\-Z/]*')


def new_value(old, cur):
    """Every word of the input's value gets a pseudonym: the one the build gave it where the build replaced
    it (a code then reads the same in the title and on the sheet), its token pseudonym elsewhere.
    `old` is the input's value, `cur` what the build made of it."""
    if old == cur:
        return ENG.words_pseudonym(old)
    if len(old) != len(cur) or len(old) > 2000:
        return cur                          # replaced as a whole (an identity value)
    res = list(cur)
    runs = []
    s = None
    for j, ch in enumerate(old):
        if pseudo.canon_char(ch):
            if s is None:
                s = j
        elif s is not None:
            runs.append((s, j))
            s = None
    if s is not None:
        runs.append((s, len(old)))
    for s, e in runs:
        if old[s:e] != cur[s:e]:
            continue                        # the build's pseudonym stands
        word = old[s:e]
        c = pseudo.canon(word)
        if len(c) < 2 or not any(ch.isalpha() for ch in c):
            continue
        ps = ENG.pz.token(c)
        k = 0
        w = []
        for ch in word:
            if pseudo.canon_char(ch) and k < len(ps):
                w.append(pseudo._shape(ch, ps[k]))
                k += 1
            else:
                w.append(ch)
        res[s:e] = w
    return ''.join(res)


def one(rec):
    rel, orel = rec
    src = os.path.join(INP, rel)
    dst = os.path.join(OUT, orel)
    try:
        with pikepdf.open(src) as a:
            in_info = info_of(a)
            in_lists = []                 # per packet: the text nodes of its descriptive elements, in order
            for obj in packets(a):
                try:
                    in_lists.append(desc_nodes(obj.read_bytes()))
                except Exception:
                    in_lists.append(None)
    except Exception as ex:
        return rel, 0, 'input ' + type(ex).__name__, None
    try:
        pdf = pikepdf.open(dst)
    except Exception as ex:
        return rel, 0, 'output ' + type(ex).__name__, None
    n = nx = 0
    example = None
    tmp = dst + '.~ma'
    with pdf:
        pairs = {}
        info = pdf.trailer.get('/Info')
        if isinstance(info, pikepdf.Dictionary):
            for k in list(info.keys()):
                try:
                    v = info[k]
                except Exception:
                    continue
                if not isinstance(v, pikepdf.String):
                    continue
                ks = str(k)
                if ks in DATES:
                    if SHIFT and ks in ('/CreationDate', '/ModDate'):
                        moved = shift_pdf_date(str(v))
                        if moved and moved != str(v):
                            info[k] = pikepdf.String(moved)
                            n += 1
                    continue
                if ks not in ('/Creator', '/Producer') and H.label_class('pdf.info.' + ks.lstrip('/').lower()):
                    continue                      # an identity field: the build's value stands
                cur = str(v)
                old = in_info.get(ks, cur)
                new = new_value(old, cur)
                if new == old:
                    continue
                if new != cur:
                    info[k] = pikepdf.String(new)
                    n += 1
                    if example is None or ks == '/Title':
                        example = (ks, old[:50], new[:50])
                for o in (old, cur):
                    if len(o.strip()) >= 3 and o != new:
                        pairs[o] = new

        state = {}

        def one_node(m):
            node = m.group(1)
            k = state['k']
            state['k'] = k + 1
            src_nodes = state['in']
            if src_nodes is not None:
                before = src_nodes[k]             # the same node of the input's packet
            elif node in state['set']:
                before = node
            else:
                return m.group(0)
            try:
                t_old = html.unescape(before.decode('utf-8'))
                t_cur = html.unescape(node.decode('utf-8'))
            except UnicodeDecodeError:
                return m.group(0)
            if not t_old.strip() or _NUMERIC.fullmatch(t_old):
                return m.group(0)
            new = new_value(t_old, t_cur)
            if new == t_cur:
                return m.group(0)
            return b'>' + html.escape(new, quote=False).encode('utf-8') + b'<'

        def one_el(m):
            inner = m.group(3)
            if b'<' in inner:
                body = H._XMP_TEXT.sub(one_node, b'>' + inner + b'<')[1:-1]
            elif inner.strip():
                body = one_node(re.match(rb'>(.*)<', b'>' + inner + b'<', re.S))[1:-1]
            else:
                body = inner
            return b'<' + m.group(1) + m.group(2) + b'>' + body + b'</' + m.group(1) + b'>'

        allin = set()
        for lst in in_lists:
            if lst:
                allin.update(lst)
        for idx, obj in enumerate(packets(pdf)):
            try:
                raw = obj.read_bytes()
            except Exception:
                continue
            upd = raw
            for o in sorted(pairs, key=len, reverse=True):
                for esc in (lambda x: x, html.escape, lambda x: html.escape(x, quote=False)):
                    a = esc(o).encode('utf-8')
                    if a in upd:
                        upd = upd.replace(a, esc(pairs[o]).encode('utf-8'))
            # the descriptive elements, node by node against the input's packet (same order) where the
            # two packets have the same shape; otherwise only nodes that still read as in the input
            mine = desc_nodes(raw)
            theirs = in_lists[idx] if idx < len(in_lists) else None
            state['in'] = theirs if (theirs is not None and len(theirs) == len(mine)) else None
            state['set'] = allin
            state['k'] = 0
            try:
                # node order must be counted on the packet as it was read (pairs above keep the shape)
                upd2 = H._XMP_DESC.sub(one_el, upd)
                if state['in'] is None or state['k'] == len(mine):
                    upd = upd2
            except Exception:
                pass
            if SHIFT:
                upd = shift_iso_dates(upd)
            if upd != raw:
                obj.write(upd)
                nx += 1
        if not (n or nx) or DRY:
            return rel, n + nx, None, example
        npages = len(pdf.pages)
        try:
            if pdf.is_encrypted:
                pdf.save(tmp, encryption=True, fix_metadata_version=False)
            else:
                pdf.save(tmp, encryption=False, fix_metadata_version=False, deterministic_id=True)
        except Exception as ex:
            if os.path.exists(tmp):
                os.unlink(tmp)
            return rel, 0, 'save ' + type(ex).__name__, None
    try:
        with pikepdf.open(tmp) as chk:
            ok = len(chk.pages) == npages
    except Exception:
        ok = False
    if ok:
        # a damaged file can lose pages when it is written again: the rewritten copy must show as many
        # pages as the file it replaces, counted by an independent reader
        a = subprocess.run(['qpdf', '--show-npages', dst], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=600)
        b = subprocess.run(['qpdf', '--show-npages', tmp], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=600)
        if a.stdout.strip() != b.stdout.strip() or not b.stdout.strip():
            os.unlink(tmp)
            return rel, 0, 'page count would change', None
    if not ok:
        os.unlink(tmp)
        return rel, 0, 'check failed', None
    os.replace(tmp, dst)
    return rel, n + nx, None, example


if __name__ == '__main__':
    recs = {}
    order = []
    for l in open(JOURNAL, encoding='utf-8'):
        r = json.loads(l)
        if r['p'] not in recs:
            order.append(r['p'])
        recs[r['p']] = r
    todo = [(p, recs[p].get('o') or p) for p in order
            if recs[p].get('k') == 'pdf' and (ONLY is None or p in ONLY)]
    print('PDFs:', len(todo), '| dry run' if DRY else '')
    changed = {}
    errors = {}
    shown = 0
    with mp.Pool(JOBS, initializer=_init) as pool:
        for rel, n, err, ex in pool.imap_unordered(one, todo, chunksize=16):
            if err:
                errors[err] = errors.get(err, 0) + 1
            elif n:
                changed[rel] = n
                if ex and shown < 40 and (len(changed) % 97 == 1):
                    shown += 1
                    print('   %-10s %-52s -> %s' % ex)
    print('PDFs with metadata values rewritten:', len(changed), '| values and packets:', sum(changed.values()),
          '| not done:', errors)
    if changed and not DRY:
        tmpj = JOURNAL + '.~ma'
        with open(tmpj, 'w', encoding='utf-8') as fh:
            for p in order:
                r = recs[p]
                if p in changed:
                    r['c'] = 1
                    r['metaall'] = changed[p]
                fh.write(json.dumps(r, ensure_ascii=False) + '\n')
        os.replace(tmpj, JOURNAL)
        print('journal updated:', JOURNAL)
