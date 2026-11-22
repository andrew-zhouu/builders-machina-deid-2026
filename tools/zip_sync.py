"""Archive members follow their extracted copies.

Where the corpus holds an archive and, beside it, a folder with its extracted files ("name.zip" and
"name.extracted/"), a member and its extracted copy are the same file in the input. PDF passes that are run
after the full build rewrite the loose copies only; this step puts the processed bytes of the loose copy
into the archive again, so both read the same and nothing stays behind in the archived copy.
Only members whose input bytes equal the input bytes of the loose copy are touched. The archive is rewritten
through a temporary file (names, order, dates and compression as before) and tested before it replaces the
old directory entry.

Usage: zip_sync.py OUT_CORPUS JOURNAL [--dry] [--ext .pdf,.step]     (default: PDF members only)
       (JOURNAL: fields p, o, k, c; last record wins; rewritten with zsync=<members> for changed archives)"""
import collections, json, os, sys, zipfile, zlib
sys.path.insert(0, os.environ.get('DEID_CODE', '/home/ubuntu/machina/work/code_attemptx1'))
from deid import handlers as H

OUT, JOURNAL = sys.argv[1:3]
DRY = '--dry' in sys.argv
EXT = ('.pdf',)
if '--ext' in sys.argv:
    EXT = tuple(x.strip().lower() for x in sys.argv[sys.argv.index('--ext') + 1].split(',') if x.strip())
INP = '/home/ubuntu/machina/input'


def crc_of(path):
    c = 0
    with open(path, 'rb') as fh:
        while True:
            b = fh.read(1 << 20)
            if not b:
                break
            c = zlib.crc32(b, c)
    return c & 0xFFFFFFFF


recs = {}
order = []
for l in open(JOURNAL, encoding='utf-8'):
    r = json.loads(l)
    if r['p'] not in recs:
        order.append(r['p'])
    recs[r['p']] = r
omap = {p: (r.get('o') or p) for p, r in recs.items()}
folders = collections.Counter()
for p in omap:
    i = p.find('.extracted/')
    if i >= 0:
        folders[p[:i + len('.extracted')]] += 1
stat = collections.Counter()
done = {}
for d in sorted(folders):
    stem = d[:-len('.extracted')]
    arch = None
    for c in (stem, stem + '.zip'):
        if c in omap and os.path.isfile(os.path.join(INP, c)):
            arch = c
            break
    if arch is None:
        stat['extracted folders without their archive'] += 1
        continue
    src_in = os.path.join(INP, arch)
    dst = os.path.join(OUT, omap[arch])
    try:
        zi = zipfile.ZipFile(src_in)
        zo = zipfile.ZipFile(dst)
    except Exception:
        stat['archives that are no readable zip'] += 1
        continue
    with zi, zo:
        mi = [i for i in zi.infolist() if not i.is_dir()]
        mo = [i for i in zo.infolist() if not i.is_dir()]
        if len(mi) != len(mo):
            stat['archives whose member count differs from the input'] += 1
            continue
        repl = {}
        for a, b in zip(mi, mo):
            if not a.filename.lower().endswith(EXT):
                continue
            loose = d + '/' + a.filename
            if loose not in omap or (a.flag_bits & 1):
                continue
            lin = os.path.join(INP, loose)
            lout = os.path.join(OUT, omap[loose])
            try:
                if os.path.getsize(lin) != a.file_size or crc_of(lin) != a.CRC:
                    stat['members that differ from the loose copy in the input'] += 1
                    continue
                if os.path.getsize(lout) == b.file_size and crc_of(lout) == b.CRC:
                    stat['members already equal to the loose copy'] += 1
                    continue
            except OSError:
                stat['loose copy unreadable'] += 1
                continue
            repl[b.filename] = lout
    if not repl:
        continue
    stat['members to update'] += len(repl)
    stat['archives to rewrite'] += 1
    if DRY:
        continue
    tmp = dst + '.~zs'

    def member(name, data, _repl=repl):
        path = _repl.get(name)
        if path is None:
            return None
        with open(path, 'rb') as fh:
            new = fh.read()
        return new if new != data else None

    try:
        n = H.rewrite_zip(dst, tmp, member)
    except Exception as ex:
        if os.path.exists(tmp):
            os.unlink(tmp)
        stat['rewrite failed: ' + type(ex).__name__] += 1
        continue
    ok = False
    if n and os.path.exists(tmp):
        try:
            with zipfile.ZipFile(tmp) as chk, zipfile.ZipFile(dst) as old:
                ok = chk.testzip() is None and len(chk.infolist()) == len(old.infolist())
        except Exception:
            ok = False
    if not ok:
        if os.path.exists(tmp):
            os.unlink(tmp)
        stat['rewritten archive failed its check (left as it was)'] += 1
        continue
    os.replace(tmp, dst)
    done[arch] = n
    stat['members updated'] += n
    stat['archives rewritten'] += 1
print('extracted folders:', len(folders), '| dry run' if DRY else '')
for k, v in sorted(stat.items()):
    print('   %-58s %d' % (k, v))
if done and not DRY:
    tmpj = JOURNAL + '.~zs'
    with open(tmpj, 'w', encoding='utf-8') as fh:
        for p in order:
            r = recs[p]
            if p in done:
                r['c'] = 1
                r['zsync'] = done[p]
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(tmpj, JOURNAL)
    print('journal updated:', JOURNAL)
