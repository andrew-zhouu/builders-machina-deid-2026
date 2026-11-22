"""PDF members of zip archives are processed again by the code and options of the build's PDF pass.

The PDF passes that follow the full build rewrite the loose PDF files only; the copies of PDFs inside
archives keep the state of the full build. This step reads every PDF member from the input archive,
processes it like the pass processes a loose PDF (same engine, same scope rules as for an archive member
in the full build) and puts it into the output archive. Every other member keeps the bytes it has.
The archive is rewritten through a temporary file (names, order, dates and compression as before) and
tested before it replaces the old directory entry.

Usage: zip_refresh.py OUT_CORPUS JOURNAL DICT [--jobs N] [--dry] [--metaall] [--inplace]
       code from DEID_CODE (default work/code_attemptx1); the reading options of the pass through the
       environment (DEID_WL_ALLWORDS, DEID_WL_ITEMS), exactly as in the pass.
       (JOURNAL: fields p, o, k, c; last record wins; rewritten with zpdf=<members> for changed archives)"""
import collections, json, os, shutil, sys, tempfile, zipfile, multiprocessing as mp
sys.path.insert(0, os.environ.get('DEID_CODE', '/home/ubuntu/machina/work/code_attemptx1'))
from deid import handlers as H, transform as T

OUT, JOURNAL, DICT = sys.argv[1:4]
DRY = '--dry' in sys.argv
JOBS = int(sys.argv[sys.argv.index('--jobs') + 1]) if '--jobs' in sys.argv else 8
INP = '/home/ubuntu/machina/input'
KEYFILE = '/home/ubuntu/machina/work/secret.key'
TMPROOT = '/home/ubuntu/machina/work/tmp'
OPTS = {"rename": "all", "scope": True, "pdftext": True, "pdfextra": True, "logos": "/home/ubuntu/machina/work/pdflogos.json",
        "previews": True, "creo": True, "zlb": True, "strings": True, "dwg": True, "dwgobj": True,
        "pdforder": '--inplace' in sys.argv, "pdfwl": True, "pdfmeta": '--metaall' in sys.argv, "nameswl": False,
        "nofallback": False}


def one(rec):
    rel, orel = rec
    eng = T._ENGINE
    src_in = os.path.join(INP, rel)
    dst = os.path.join(OUT, orel)
    stat = collections.Counter()
    try:
        zi = zipfile.ZipFile(src_in)
        zo = zipfile.ZipFile(dst)
    except Exception as ex:
        return rel, 0, {'unreadable archive': 1}
    tmpd = tempfile.mkdtemp(prefix='zr', dir=TMPROOT)
    repl = {}
    try:
        with zi, zo:
            mi = [i for i in zi.infolist() if not i.is_dir()]
            mo = [i for i in zo.infolist() if not i.is_dir()]
            if len(mi) != len(mo):
                return rel, 0, {'member count differs from the input': 1}
            k = 0
            for a, b in zip(mi, mo):
                if not a.filename.lower().endswith('.pdf'):
                    continue
                if (a.flag_bits & 1) or a.file_size == 0 or a.file_size > (512 << 20):
                    stat['members skipped (encrypted, empty or very large)'] += 1
                    continue
                k += 1
                src = os.path.join(tmpd, '%d.in' % k)
                out = os.path.join(tmpd, '%d.out' % k)
                try:
                    data = zi.read(a)
                    with open(src, 'wb') as fh:
                        fh.write(data)
                    T.prepare(eng, rel)
                    ch, kind, info = T.process(rel + '!/' + a.filename, src, out, 1)
                    cur = zo.read(b)
                except Exception as ex:
                    stat['member failed: ' + type(ex).__name__] += 1
                    continue
                if info.get('error'):
                    stat['member left as it was (handler error)'] += 1
                    continue
                if ch:
                    with open(out, 'rb') as fh:
                        new = fh.read()
                else:
                    new = data
                stat['PDF members read'] += 1
                if new != cur:
                    repl[b.filename] = out if ch else src
                else:
                    stat['PDF members already in this state'] += 1
                    for f in (src, out):
                        if os.path.exists(f):
                            os.unlink(f)
        if not repl or DRY:
            stat['PDF members to replace'] += len(repl)
            return rel, 0, dict(stat)
        tmp = dst + '.~zr'

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
            return rel, 0, dict(stat)
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
            return rel, 0, dict(stat)
        os.replace(tmp, dst)
        stat['PDF members replaced'] += n
        return rel, n, dict(stat)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


if __name__ == '__main__':
    os.makedirs(TMPROOT, exist_ok=True)
    recs = {}
    order = []
    for l in open(JOURNAL, encoding='utf-8'):
        r = json.loads(l)
        if r['p'] not in recs:
            order.append(r['p'])
        recs[r['p']] = r
    todo = []
    for p in order:
        r = recs[p]
        if r.get('k') != 'zip':
            continue
        try:
            with zipfile.ZipFile(os.path.join(INP, p)) as z:
                if any(i.filename.lower().endswith('.pdf') for i in z.infolist()):
                    todo.append((p, r.get('o') or p))
        except Exception:
            continue
    print('zip archives with PDF members:', len(todo), '| dry run' if DRY else '', '| options:',
          {k: v for k, v in OPTS.items() if k in ('pdfwl', 'pdfmeta', 'pdforder')})
    total = collections.Counter()
    done = {}
    with mp.Pool(JOBS, initializer=T._init, initargs=(DICT, KEYFILE, TMPROOT, OPTS)) as pool:
        for rel, n, stat in pool.imap_unordered(one, todo, chunksize=1):
            total.update(stat)
            if n:
                done[rel] = n
    for k, v in sorted(total.items()):
        print('   %-58s %d' % (k, v))
    print('archives rewritten:', len(done))
    if done and not DRY:
        tmpj = JOURNAL + '.~zr'
        with open(tmpj, 'w', encoding='utf-8') as fh:
            for p in order:
                r = recs[p]
                if p in done:
                    r['c'] = 1
                    r['zpdf'] = done[p]
                fh.write(json.dumps(r, ensure_ascii=False) + '\n')
        os.replace(tmpj, JOURNAL)
        print('journal updated:', JOURNAL)
