"""Source folders take the pseudonyms of their own words.

The scope-terms reading replaces the words of a source's name (account, project, site, collection) in the
text and metadata of its PDFs. This step gives the source folder itself the same pseudonyms, so that the
name of the folder and the documents inside agree: every scope term that still stands in the folder's name
(the dictionary already renamed accounts and known names) is replaced by its token pseudonym.
Only folders are renamed (top-level source folders; inside the organisers' collections the folder that
names the source); file names stay as the build wrote them, so references between files are untouched.

Usage: scope_rename.py OUT_CORPUS JOURNAL DICT [--dry]
       (JOURNAL: fields p, o ...; last record wins; rewritten with the new output paths)"""
import collections, json, os, re, sys
sys.path.insert(0, os.environ.get('DEID_CODE', '/home/ubuntu/machina/work/code_attempts1'))
from deid import engine as E, pseudo, scopeterms as S

OUT, JOURNAL, DICT = sys.argv[1:4]
DRY = '--dry' in sys.argv
eng = E.Engine(DICT, open('/home/ubuntu/machina/work/secret.key', 'rb').read())

recs = {}
order = []
for l in open(JOURNAL, encoding='utf-8'):
    r = json.loads(l)
    if r['p'] not in recs:
        order.append(r['p'])
    recs[r['p']] = r


def matcher(names, skip):
    longs = set()
    shorts = set()
    for name in names:
        name = S._ARCH.sub('', name)
        for part in re.split(r'[^A-Za-z0-9]+', name):
            if not part or (S._HEX.match(part.lower()) and any(ch.isdigit() for ch in part)):
                continue
            for c in set(S._CAMEL.findall(part)) | set(S._CAMEL2.findall(part)):
                c = c.lower()
                if not c.isalpha() or not c.isascii() or c in skip or c in S._JOIN:
                    continue
                if len(c) >= 4:
                    longs.add(c)
                elif len(c) == 3:
                    shorts.add(c)
    alts = []
    if longs:
        alts.append('(?:%s)' % '|'.join(sorted((re.escape(t) for t in longs), key=len, reverse=True)))
    if shorts:
        alts.append('(?<![A-Za-z])(?:%s)(?![A-Za-z])' % '|'.join(sorted(re.escape(t) for t in shorts)))
    return re.compile('|'.join(alts), re.I) if alts else None


def new_name(orig, cur, skip):
    """Name of a folder: `orig` in the input, `cur` in the build. Scope terms of `orig` that the build left
    standing get their pseudonym; what the build already replaced stays."""
    if len(orig) != len(cur):
        return cur                      # the build changed the shape of the name: left alone
    rx = matcher([orig], skip)
    if rx is None:
        return cur
    out = list(cur)
    for m in rx.finditer(orig):
        s, e = m.span()
        if cur[s:e] != orig[s:e]:
            continue
        c = pseudo.canon(orig[s:e])
        if len(c) != e - s:
            continue
        ps = eng.pz.token(c)
        if len(ps) != len(c):
            continue
        out[s:e] = [pseudo._shape(ch, p) for ch, p in zip(orig[s:e], ps)]
    return ''.join(out)


# folder (input path) -> folder (output path), from the journal
dirs = {}
for p in order:
    o = recs[p].get('o') or p
    a = p.split('/')
    b = o.split('/')
    if len(a) != len(b):
        continue
    if a[0] == 'sources' and len(a) > 3:
        if a[1] == 'existing':
            d = S.source_depth(a)                 # the folder that names the source
            if d is not None:
                dirs.setdefault('/'.join(a[:d + 1]), '/'.join(b[:d + 1]))
        else:
            dirs.setdefault('/'.join(a[:3]), '/'.join(b[:3]))

plan = {}        # output folder path (current) -> new last component
stat = collections.Counter()
for din, dout in dirs.items():
    a = din.split('/')
    if a[1] == 'existing':
        skip = S._STRUCT
    elif a[1] == 'web':
        skip = S._SITE
    else:
        skip = frozenset()
    oname = a[-1]
    cname = dout.split('/')[-1]
    if a[1] in ('github', 'gitlab'):
        # account__repository[__hash]: read part by part
        po = oname.split('__')
        pc = cname.split('__')
        if len(po) != len(pc):
            stat['folders whose shape changed (left alone)'] += 1
            continue
        nn = '__'.join(new_name(x, y, skip) for x, y in zip(po, pc))
    else:
        nn = new_name(oname, cname, skip)
    stat['folders read'] += 1
    if nn != cname:
        plan[dout] = nn
        stat['folders to rename'] += 1

# new full output paths: apply the renames level by level (a parent's rename moves its children)
def remap(o):
    b = o.split('/')
    out = []
    cur = []
    for comp in b[:-1]:
        cur.append(comp)
        key = '/'.join(cur)
        out.append(plan.get(key, comp))
    out.append(b[-1])
    return '/'.join(out)


new_o = {}
for p in order:
    o = recs[p].get('o') or p
    n = remap(o)
    new_o[p] = n
targets = collections.Counter(new_o.values())
clash = [p for p, n in new_o.items() if targets[n] > 1]
print('folders read:', stat['folders read'], '| to rename:', stat['folders to rename'], '| files whose path changes:',
      sum(1 for p in order if new_o[p] != (recs[p].get('o') or p)), '| path clashes:', len(clash), dict(stat))
shown = 0
for k in sorted(plan):
    if shown < 14 and (hash(k) % 23 == 0 or shown < 4):
        shown += 1
        print('   %-70s -> %s' % (k[-70:], plan[k]))
if clash:
    print('clashes, nothing done:', clash[:5])
    sys.exit(1)
if DRY:
    sys.exit(0)
# rename deepest folders first, so that a parent's old path is still valid for its children
done = 0
for k in sorted(plan, key=lambda x: -x.count('/')):
    src = os.path.join(OUT, k)
    dst = os.path.join(OUT, os.path.dirname(k), plan[k])
    if not os.path.isdir(src):
        print('missing folder:', k)
        sys.exit(1)
    if os.path.exists(dst):
        print('target exists:', dst)
        sys.exit(1)
    os.rename(src, dst)
    done += 1
missing = 0
for p in order:
    if not os.path.exists(os.path.join(OUT, new_o[p])):
        missing += 1
        if missing <= 5:
            print('not where the journal says:', new_o[p])
if missing:
    print('files missing after the renames:', missing)
    sys.exit(1)
tmpj = JOURNAL + '.~sr'
with open(tmpj, 'w', encoding='utf-8') as fh:
    for p in order:
        r = recs[p]
        if new_o[p] != p:
            r['o'] = new_o[p]
        fh.write(json.dumps(r, ensure_ascii=False) + '\n')
os.replace(tmpj, JOURNAL)
print('folders renamed:', done, '| journal updated:', JOURNAL)
