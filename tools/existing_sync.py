"""The organisers' collections: a source's own name, the same everywhere inside the source.

The scope-terms reading replaces the words of a source's name in its PDFs, and the source folder takes the
same pseudonyms. This step carries them across the other surfaces of the source:

  STEP files   the strings of the naming entities (product, product definition, assembly usage, shape
               representation, layer, document) and the name / author / organisation / authorisation of the
               header: each scope word is replaced where its letters stand, same length, so nothing outside
               the string literals changes. Strings that the STEP schemas prescribe ('design', 'part
               definition', 'mechanical' ...) and escape sequences are left alone.
  names        files and folders inside the source: a scope word in a name gets the same pseudonym.
               Sources that hold native CAD files (SolidWorks, Inventor, Creo) keep their inner names,
               because those files point at each other by name.

The pseudonym of a word is the one the renamed source folder shows in its place.
Files are rewritten through a temporary file; a hard-linked copy in another tree is not touched.

Usage: existing_sync.py OUT_CORPUS JOURNAL DICT [--dry] [--no-names] [--no-step]"""
import collections, json, os, re, sys
sys.path.insert(0, os.environ.get('DEID_CODE', '/home/ubuntu/machina/work/code_attempts2'))
from deid import engine as E, pseudo, scopeterms as S

OUT, JOURNAL, DICT = sys.argv[1:4]
DRY = '--dry' in sys.argv
DO_NAMES = '--no-names' not in sys.argv
DO_STEP = '--no-step' not in sys.argv
ALL = '--all-sources' in sys.argv       # every source of the corpus, not only the organisers' collections
eng = E.Engine(DICT, open('/home/ubuntu/machina/work/secret.key', 'rb').read())

recs = {}
order = []
for l in open(JOURNAL, encoding='utf-8'):
    r = json.loads(l)
    if r['p'] not in recs:
        order.append(r['p'])
    recs[r['p']] = r

# ---- sources of the collections, their words and the pseudonyms their folders show -----------------
sources = {}            # input folder path -> dict(depth, rx, map)
files_of = collections.defaultdict(list)
native = set()
for p in order:
    a = p.split('/')
    if a[0] != 'sources' or len(a) < 4:
        continue
    if a[1] == 'existing':
        d = S.source_depth(a)
    elif ALL and a[1] in ('github', 'gitlab', 'catalog', 'web'):
        d = 2
    else:
        d = None
    if d is None:
        continue
    src = '/'.join(a[:d + 1])
    files_of[src].append(p)
    if recs[p].get('k') in ('sw', 'ole', 'creo', 'swlock', 'catia', 'jt'):
        native.add(src)
    if src in sources:
        continue
    o = (recs[p].get('o') or p).split('/')
    longs, shorts = S.terms_of(p)
    if not (longs or shorts) or len(o) != len(a):
        sources[src] = None
        continue
    alts = []
    if longs:
        alts.append('(?:%s)' % '|'.join(sorted((re.escape(t) for t in longs), key=len, reverse=True)))
    if shorts:
        alts.append('(?<![A-Za-z])(?:%s)(?![A-Za-z])' % '|'.join(sorted(re.escape(t) for t in shorts)))
    rx = re.compile('|'.join(alts), re.I)
    mp = {}
    x, y = a[d], o[d]
    if a[1] == 'web':
        # top-level domains and "www" are no words of the name (as in scopeterms.terms_of)
        pass
    if len(x) == len(y):
        for m in rx.finditer(x):
            s, e = m.span()
            t = x[s:e].lower()
            cand = y[s:e].lower()
            if t not in mp and cand != t and cand.isalpha() and cand.isascii():
                mp[t] = cand
    sources[src] = {'d': d, 'rx': rx, 'map': mp, 'words': sorted(longs | shorts)}


def rep_of(info, word):
    c = word.lower()
    ps = info['map'].get(c) or eng.pz.token(c)
    if len(ps) != len(c):
        return word
    return ''.join(pseudo._shape(ch, p) for ch, p in zip(word, ps))


def sub_text(info, text):
    """`text` with the scope words replaced (same length); count of replacements."""
    n = [0]

    def one(m):
        new = rep_of(info, m.group(0))
        if new != m.group(0):
            n[0] += 1
        return new

    return info['rx'].sub(one, text), n[0]


# ---- STEP -----------------------------------------------------------------------------------------
NAMING = (b'PRODUCT|PRODUCT_DEFINITION|PRODUCT_DEFINITION_FORMATION|PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE|'
          b'NEXT_ASSEMBLY_USAGE_OCCURRENCE|SHAPE_REPRESENTATION|ADVANCED_BREP_SHAPE_REPRESENTATION|'
          b'MANIFOLD_SURFACE_SHAPE_REPRESENTATION|GEOMETRICALLY_BOUNDED_SURFACE_SHAPE_REPRESENTATION|'
          b'GEOMETRICALLY_BOUNDED_WIREFRAME_SHAPE_REPRESENTATION|MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS|'
          b'SHELL_BASED_SURFACE_MODEL|PRESENTATION_LAYER_ASSIGNMENT|DOCUMENT|DOCUMENT_FILE|'
          b'PRODUCT_DEFINITION_SHAPE|DESCRIPTIVE_REPRESENTATION_ITEM')
STMT = re.compile(rb"=\s*(?:" + NAMING + rb")\s*\(((?:[^';]|'(?:[^']|'')*')*)\)\s*;")
HEAD = re.compile(rb"FILE_NAME\s*\(((?:[^';]|'(?:[^']|'')*')*)\)\s*;")
STRING = re.compile(rb"'((?:[^']|'')*)'")
ESC = re.compile(r"\\X2\\[0-9A-Fa-f]*\\X0\\|\\X4\\[0-9A-Fa-f]*\\X0\\|\\X\\[0-9A-Fa-f]{2}|\\S\\.|\\[PN]\\?[A-Z]?\\?")
SCHEMA = frozenset(x.lower() for x in (
    'design', 'part definition', 'mechanical', 'mechanical design', 'automotive design', 'detail', 'part', 'assembly',
    'none', 'not specified', 'unknown', 'unspecified', 'international standard', 'draughting', 'presentation',
    'configuration controlled 3d designs of mechanical parts and assemblies', 'config_control_design',
    'core data for automotive mechanical design processes', 'automotive_design', 'managed model based 3d engineering',
    'ap242_managed_model_based_3d_engineering', 'product definition', 'user defined attributes', 'material property',
    'material name', 'density', 'isotropic material', 'geometric validation property', 'volume measure',
    'centre point', 'surface area measure', 'area measure', 'pmi validation property'))


def step_string(info, raw):
    """One string literal (bytes between the quotes)."""
    try:
        text = raw.decode('latin-1')
    except Exception:
        return raw, 0
    if text.strip().lower() in SCHEMA or not info['rx'].search(text):
        return raw, 0
    out = []
    pos = 0
    n = 0
    for m in ESC.finditer(text):
        seg, k = sub_text(info, text[pos:m.start()])
        out.append(seg)
        n += k
        out.append(m.group(0))
        pos = m.end()
    seg, k = sub_text(info, text[pos:])
    out.append(seg)
    n += k
    new = ''.join(out)
    if len(new) != len(text):
        return raw, 0
    return new.encode('latin-1'), n


def step_file(info, path):
    """New bytes of a STEP file, or None when nothing changes."""
    if os.path.getsize(path) > (700 << 20):
        return None, 0
    with open(path, 'rb') as fh:
        data = fh.read()
    low = data.lower()
    if not any(w.encode('ascii') in low for w in info['words']):
        return None, 0
    total = [0]

    def strings(body, only=None):
        idx = [0]

        def one(m):
            idx[0] += 1
            if only is not None and idx[0] not in only:
                return m.group(0)
            new, k = step_string(info, m.group(1))
            total[0] += k
            return b"'" + new + b"'"

        return STRING.sub(one, body)

    def stmt(m):
        body = m.group(1)
        new = strings(body)
        if new == body:
            return m.group(0)
        return m.group(0).replace(body, new, 1)

    def head(m):
        body = m.group(1)
        # arguments: name, time stamp, (authors), (organisations), preprocessor, originating system, authorisation
        depth = 0
        arg = 1
        quote = False
        spans = []          # (start, end, argument number) of the string literals
        i = 0
        n = len(body)
        while i < n:
            ch = body[i:i + 1]
            if quote:
                if ch == b"'":
                    if body[i + 1:i + 2] == b"'":
                        i += 1
                    else:
                        quote = False
                        spans[-1][1] = i + 1
            elif ch == b"'":
                quote = True
                spans.append([i, None, arg])
            elif ch == b'(':
                depth += 1
            elif ch == b')':
                depth -= 1
            elif ch == b',' and depth == 0:
                arg += 1
            i += 1
        out = []
        pos = 0
        for s, e, a in spans:
            if e is None:
                break
            out.append(body[pos:s])
            lit = body[s + 1:e - 1]
            if a in (1, 3, 4, 7):
                new, k = step_string(info, lit)
                total[0] += k
                out.append(b"'" + new + b"'")
            else:
                out.append(body[s:e])
            pos = e
        out.append(body[pos:])
        new = b''.join(out)
        return m.group(0).replace(body, new, 1) if new != body else m.group(0)

    cut = data.find(b'ENDSEC;')
    cut = cut if 0 < cut < (1 << 20) else min(len(data), 1 << 16)
    new_head = HEAD.sub(head, data[:cut], count=1)
    new_rest = STMT.sub(stmt, data[cut:])
    if not total[0]:
        return None, 0
    new = new_head + new_rest
    if len(new) != len(data) or new.count(b';') != data.count(b';'):
        return None, 0
    return new, total[0]


stat = collections.Counter()
changed = {}
if DO_STEP:
    for src, info in sorted(sources.items()):
        if not info:
            continue
        for p in files_of[src]:
            if recs[p].get('k') != 'step':
                continue
            path = os.path.join(OUT, recs[p].get('o') or p)
            try:
                new, n = step_file(info, path)
            except Exception as ex:
                stat['STEP files that could not be read (%s)' % type(ex).__name__] += 1
                continue
            stat['STEP files read'] += 1
            if new is None:
                continue
            stat['STEP files with scope words in naming strings'] += 1
            stat['strings replaced in STEP files'] += n
            if DRY:
                continue
            tmp = path + '.~es'
            with open(tmp, 'wb') as fh:
                fh.write(new)
            os.replace(tmp, path)
            changed[p] = n

# ---- names inside the source -----------------------------------------------------------------------
new_o = {}
if DO_NAMES:
    for src, info in sorted(sources.items()):
        if not info:
            continue
        if src in native:
            stat['sources with native CAD files (inner names kept)'] += 1
            continue
        d = info['d']
        for p in files_of[src]:
            a = p.split('/')
            b = (recs[p].get('o') or p).split('/')
            if len(a) != len(b):
                continue
            nb = list(b)
            for i in range(d + 1, len(a)):
                x, y = a[i], b[i]
                if len(x) != len(y) or not info['rx'].search(x):
                    continue
                comp = list(y)
                for m in info['rx'].finditer(x):
                    s, e = m.span()
                    if y[s:e] != x[s:e]:
                        continue                      # the build already replaced these letters
                    comp[s:e] = rep_of(info, x[s:e])
                nb[i] = ''.join(comp)
            if nb != b:
                new_o[p] = '/'.join(nb)
    final = {p: new_o.get(p, recs[p].get('o') or p) for p in order}
    count = collections.Counter(final.values())
    clash = [p for p in new_o if count[new_o[p]] > 1]
    for p in clash:
        del new_o[p]
    stat['files renamed inside their source'] = len(new_o)
    stat['renames dropped (two files would share a path)'] = len(clash)
print('sources of the collections:', len(sources), '| with words:', sum(1 for v in sources.values() if v), '| dry run' if DRY else '')
for k, v in sorted(stat.items()):
    print('   %-58s %d' % (k, v))
shown = 0
for p in sorted(new_o):
    if shown < 10 and hash(p) % 37 == 0:
        shown += 1
        print('   %s\n     -> %s' % ((recs[p].get('o') or p)[-100:], new_o[p][-100:]))
if DRY:
    sys.exit(0)
if new_o:
    # two-phase move, then empty folders are removed
    moves = []
    for i, p in enumerate(sorted(new_o)):
        cur = os.path.join(OUT, recs[p].get('o') or p)
        tmp = os.path.join(OUT, '.~es%d' % i)
        os.rename(cur, tmp)
        moves.append((tmp, os.path.join(OUT, new_o[p]), cur))
    olddirs = set()
    for tmp, dst, cur in moves:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.lexists(dst):
            print('target exists:', dst)
            sys.exit(1)
        os.rename(tmp, dst)
        olddirs.add(os.path.dirname(cur))
    for dname in sorted(olddirs, key=lambda x: -x.count('/')):
        cur = dname
        while cur.startswith(OUT) and cur != OUT:
            try:
                os.rmdir(cur)
            except OSError:
                break
            cur = os.path.dirname(cur)
if changed or new_o:
    tmpj = JOURNAL + '.~es'
    with open(tmpj, 'w', encoding='utf-8') as fh:
        for p in order:
            r = recs[p]
            if p in changed:
                r['c'] = 1
                r['esync'] = changed[p]
            if p in new_o:
                r['o'] = new_o[p]
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(tmpj, JOURNAL)
    missing = sum(1 for p in order if (p in new_o or p in changed) and not os.path.exists(os.path.join(OUT, recs[p].get('o') or p)))
    print('journal updated:', JOURNAL, '| files of the collections missing on disk:', missing)
    if missing:
        sys.exit(1)
