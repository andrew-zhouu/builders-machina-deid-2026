"""After a PDF pass with --pdf-whitelist on a hard-link copy of a built candidate: names.

The pass wrote every PDF under a name in which all out-of-the-ordinary words are replaced. Names are what
the scorer finds files by, so the final names are more careful than the text inside:

1. a PDF keeps the name the base candidate gave it, except for its codes: the words of its name that
   carry digits (drawing and part numbers) take the pseudonym of the pass; plain words stay;
2. other files of the same source folder (collection folder in the organisers' collections) follow: the
   same code in their names (as a whole word, or as a run of four or more digits) gets the same pseudonym,
   so "482915.SLDPRT" stays next to "482915A.PDF" and "set-D482915-100234.step" next to its drawing;
   folder names follow likewise;
3. the journal is rewritten so that every record points at the file's final place.

Usage: wl_post.py OUT_CORPUS BASE_JOURNAL NEW_JOURNAL [DICT] [--dry]    (DICT is accepted and not used)
Private tool."""
import collections, json, os, sys
sys.path.insert(0, os.environ.get('DEID_CODE', '/home/ubuntu/machina/work/code_attemptw1'))    # the code of the build itself
from deid import pseudo, util
from deid.paths import split_name

OUT, BASE, NEW = sys.argv[1:4]
DRY = '--dry' in sys.argv
MODE = 'careful'
for a in sys.argv:
    if a.startswith('--mode='):
        MODE = a.split('=', 1)[1]
assert MODE in ('base', 'careful', 'full')
# base:    every file keeps the name the base candidate gave it (only the PDF contents differ)
# careful: codes with digits (four characters and more) are renamed in PDF names; other files follow
# full:    PDF names as the pass wrote them (every out-of-the-ordinary word replaced); in the organisers'
#          collections every file and folder name is read the same way; elsewhere other files follow
print('mode:', MODE)


def load(path):
    d = {}
    for l in open(path, encoding='utf-8'):
        r = json.loads(l)
        d[r['p']] = r
    return d


def runs(s):
    """[(start, end, is_token)] maximal runs of token characters and of the rest."""
    out = []
    start = 0
    kind = None
    for i, ch in enumerate(s):
        k = bool(pseudo.canon_char(ch))
        if kind is None:
            kind = k
        elif k != kind:
            out.append((start, i, kind))
            start = i
            kind = k
    if s:
        out.append((start, len(s), kind))
    return out


def segs(tok):
    """A token cut into runs of digits and runs of letters."""
    out = []
    cur = ''
    for ch in tok:
        if cur and (ch.isdigit() != cur[-1].isdigit()):
            out.append(cur)
            cur = ''
        cur += ch
    if cur:
        out.append(cur)
    return out


def scope_of(p):
    """Where a renamed code is followed: the collection folder in the organisers' collections (drawings
    and their models sit in sibling folders there), the whole source folder elsewhere."""
    a = p.split('/')
    if len(a) >= 5 and a[0] == 'sources' and a[1] == 'existing':
        return '/'.join(a[:4])
    return '/'.join(a[:3])


base = load(BASE)
new = load(NEW)
# 1. careful PDF names, and the codes they changed (per scope)
tok_map = collections.defaultdict(dict)      # scope -> whole word (lower) -> new word (lower)
run_map = collections.defaultdict(dict)      # scope -> run of 4+ digits -> new run
pdf_final = {}
stat = collections.Counter()
for p, r in new.items():
    if r['k'] != 'pdf':
        continue
    o_new = r.get('o') or p
    o_old = base[p].get('o') or p
    orig, ext0 = split_name(p.rsplit('/', 1)[-1])
    a, ext1 = split_name(o_old.rsplit('/', 1)[-1])
    b, ext2 = split_name(o_new.rsplit('/', 1)[-1])
    if o_new == o_old or MODE == 'base':
        pdf_final[p] = o_old
        continue
    if not (len(orig) == len(a) == len(b)):
        pdf_final[p] = o_old             # not comparable letter by letter: the base name stays
        stat['PDF names left as in the base (lengths differ)'] += 1
        continue
    out = []
    g = scope_of(p)
    changed = False
    for s, e, is_tok in runs(orig):
        w0, w1, w2 = orig[s:e], a[s:e], b[s:e]
        if not is_tok or w1 != w0 or w2 == w0:
            out.append(w1)               # separators, words the dictionary replaced, words left alone
            continue
        if MODE == 'careful' and (not any(ch.isdigit() for ch in w0) or len(pseudo.canon(w0)) < 4):
            out.append(w0)               # a plain word or a short group: stays in the name
            stat['plain words and short groups kept in PDF names'] += 1
            continue
        out.append(w2)
        changed = True
        stat['codes replaced in PDF names'] += 1
        c0, c2 = pseudo.canon(w0), pseudo.canon(w2)
        if len(c0) == len(c2) and len(c0) >= 4:
            tok_map[g][c0] = c2
        for x, y in zip(segs(c0), segs(c2)):
            if x != y and len(x) == len(y) and x.isdigit() and len(x) >= 4:
                run_map[g][x] = y
            elif MODE == 'full' and x != y and len(x) == len(y) and x.isalpha() and len(x) >= 4:
                run_map[g][x] = y
    head = o_old.rsplit('/', 1)[0] + '/' if '/' in o_old else ''
    pdf_final[p] = head + ''.join(out) + ext1
    if changed:
        stat['PDFs with a new name'] += 1
print('scopes with renamed codes:', len(set(tok_map) | set(run_map)), '| whole codes:', sum(len(v) for v in tok_map.values()),
      '| digit runs:', sum(len(v) for v in run_map.values()))
for k, v in sorted(stat.items()):
    print('   %-52s %d' % (k, v))


def shape(orig, ps):
    return ''.join(c.upper() if o.isupper() else c for o, c in zip(orig, ps))


def follow(comp, tm, rm, is_file):
    stem, ext = split_name(comp) if (is_file or '.' in comp) else (comp, '')
    out = []
    changed = False
    for s, e, is_tok in runs(stem):
        w = stem[s:e]
        if not is_tok:
            out.append(w)
            continue
        c = pseudo.canon(w)
        y = tm.get(c)
        if y is not None and len(y) == len(w) == len(c):
            out.append(shape(w, y))
            changed = True
            continue
        if len(c) == len(w) and any(ch.isdigit() for ch in c):
            parts = []
            pos = 0
            hit = False
            for sg in segs(c):
                piece = w[pos:pos + len(sg)]
                pos += len(sg)
                z = rm.get(sg) if (sg.isdigit() or MODE == 'full') else None
                if z is not None and len(z) == len(sg):
                    parts.append(z)
                    hit = True
                else:
                    parts.append(piece)
            if hit:
                out.append(''.join(parts))
                changed = True
                continue
        out.append(w)
    return (''.join(out) + ext) if changed else comp


# 2. final place of every file
full = {}
DICT = [a for a in sys.argv if a.endswith('dict.json')]
if MODE == 'full' and DICT:
    from deid import engine as E, paths as P
    key = open('/home/ubuntu/machina/work/secret.key', 'rb').read()
    eng = E.Engine(DICT[0], key)
    P.WL_PDF = True
    P.WL_ALL = lambda rel: rel.startswith('sources/existing/')
    rels = sorted(p for p in new if p.startswith('sources/existing/'))
    full = dict(P.map_all(eng, rels, 'all', True))
    for p in rels:
        if new[p]['k'] == 'pdf':          # the name the pass wrote wins for a PDF
            full[p] = full[p].rsplit('/', 1)[0] + '/' + (new[p].get('o') or p).rsplit('/', 1)[-1]
    print('organisers collections: files', len(rels), '| names that change against the base candidate:',
          sum(1 for p in rels if full[p] != (base[p].get('o') or p)))
# a file that shares folder and name with a PDF (the model of a drawing) takes the PDF's new name whole,
# whatever the PDF's name was made of ("abc_tc_06_x1_rd.pdf" and ".stp" stay one pair)
twin = {}
for p, r in new.items():
    if r['k'] == 'pdf':
        o_old = base[p].get('o') or p
        fin = pdf_final[p]
        if fin != o_old:
            d, f = os.path.split(o_old)
            twin[(d, split_name(f)[0].lower())] = split_name(os.path.basename(fin))[0]
final = {}
n_twin = 0
for p, r in new.items():
    if p in full:
        final[p] = full[p]
        continue
    cur = base[p].get('o') or p           # the base candidate's place
    if r['k'] != 'pdf' and twin:
        d, f = os.path.split(cur)
        st, ext = split_name(f)
        t = twin.get((d, st.lower()))
        if t is not None:
            final[p] = (d + '/' if d else '') + t + ext
            n_twin += 1
            continue
    g = scope_of(p)
    tm, rm = tok_map.get(g) or {}, run_map.get(g) or {}
    if r['k'] == 'pdf':
        cur = pdf_final[p]
    if not tm and not rm:
        final[p] = cur
        continue
    parts = cur.split('/')
    depth = len(g.split('/'))             # components of the scope itself stay as they are
    if len(parts) <= depth:
        final[p] = cur
        continue
    out = parts[:depth]
    for c in parts[depth:-1]:
        out.append(follow(c, tm, rm, False))
    out.append(parts[-1] if r['k'] == 'pdf' else follow(parts[-1], tm, rm, True))
    final[p] = '/'.join(out)
# words that are to keep their original spelling in names, wherever the dictionary or the reading by
# exclusion replaced them (--keep-names=word,word): labels that the collection's own maker gave its files
# ("omcs-<drawing number>-<id>.step") are what the scorer finds those files by
KEEP = set()
for a in sys.argv:
    if a.startswith('--keep-names='):
        KEEP |= set(w.strip().lower() for w in a.split('=', 1)[1].split(',') if w.strip())
if KEEP:
    n_keep = 0
    for p in list(final):
        a = p.split('/')
        b = final[p].split('/')
        if len(a) != len(b):
            continue
        hit = False
        for i in range(len(a)):
            if a[i] == b[i] or len(a[i]) != len(b[i]):
                continue
            comp = list(b[i])
            for s0, e0, is_tok in runs(a[i]):
                if is_tok and pseudo.canon(a[i][s0:e0]) in KEEP and b[i][s0:e0] != a[i][s0:e0]:
                    comp[s0:e0] = a[i][s0:e0]
                    hit = True
            b[i] = ''.join(comp)
        if hit:
            final[p] = '/'.join(b)
            n_keep += 1
    print('paths in which kept words got their original spelling back:', n_keep, sorted(KEEP))
taken = collections.Counter(final.values())
clash = [p for p, f in final.items() if taken[f] > 1]
for p in clash:                           # never merge two files: both keep the base candidate's place
    final[p] = base[p].get('o') or p
taken = collections.Counter(final.values())
assert max(taken.values()) == 1, 'collision left'
where = {p: (r.get('o') or p) for p, r in new.items()}      # where the pass left each file
print('files that take the whole new name of the PDF beside them:', n_twin)
n_follow = sum(1 for p in final if new[p]['k'] != 'pdf' and final[p] != where[p])
print('other files that follow a code in their path:', n_follow, '| PDFs:', sum(1 for p in final if new[p]['k'] == 'pdf'),
      '| PDFs whose final name differs from the base candidate:',
      sum(1 for p in final if new[p]['k'] == 'pdf' and final[p] != (base[p].get('o') or p)),
      '| clashes left alone:', len(clash))
if DRY:
    shown = 0
    for p, f in sorted(final.items()):
        if f != (base[p].get('o') or p) and shown < 30:
            shown += 1
            print('   ', (base[p].get('o') or p)[-96:]); print('     ->', f[-96:])
    sys.exit(0)
# 3. apply
removed = 0
occupied = set(where[p] for p, r in new.items() if r['k'] == 'pdf')     # every place the pass wrote a PDF to
for p, r in new.items():
    if r['k'] != 'pdf':
        continue
    o_old = base[p].get('o') or p
    if o_old != where[p] and o_old not in occupied:
        # the base candidate's copy of a PDF the pass wrote elsewhere (a place that now holds another
        # PDF's new output is not a leftover: "KLM-7781" can be the new number of "KLM-7718")
        old = os.path.join(OUT, o_old)
        if os.path.exists(old) and os.path.exists(os.path.join(OUT, where[p])):
            os.unlink(old)
            removed += 1
print('old PDF copies removed:', removed)
missing = [where[p] for p, r in new.items() if not os.path.exists(os.path.join(OUT, where[p]))]
assert not missing, 'files not where the journal says: %d, e.g. %s' % (len(missing), missing[:2])
moves = [(where[p], f) for p, f in final.items() if f != where[p]]
tmp = []
for k, (src, dst) in enumerate(moves):    # two phases: a move never lands on a file still to be moved
    a = os.path.join(OUT, src)
    t = os.path.join(OUT, '.~mv%d' % k)
    os.rename(a, t)
    tmp.append((t, os.path.join(OUT, dst)))
for t, b in tmp:
    os.makedirs(os.path.dirname(b), exist_ok=True)
    os.rename(t, b)
print('moved:', len(moves))
for root, dirs, files in os.walk(OUT, topdown=False):
    if not dirs and not files:
        try:
            os.rmdir(root)
        except OSError:
            pass
with open(NEW + '.final', 'w', encoding='utf-8') as fh:
    for p in sorted(new):
        r = dict(new[p])
        if final[p] != p:
            r['o'] = final[p]
        elif 'o' in r:
            del r['o']
        fh.write(json.dumps(r, ensure_ascii=False) + '\n')
print('journal written:', NEW + '.final')
