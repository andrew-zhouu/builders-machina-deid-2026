"""Archive extraction helpers.

Members are extracted to numbered files inside a private temp directory, so
member names never touch the filesystem (no traversal, encoding or length
problems). The original name travels alongside as data.
"""
import gzip
import os
import shutil
import subprocess
import tarfile
import zipfile

MAX_MEMBER = 3 << 30


def extract(path, kind, name, dest):
    """Extract archive at `path` of `kind` into directory `dest`.

    Returns a list of (member_name, extracted_path). Raises on corrupt input.
    """
    out = []
    if kind in ("zip", "zipn"):
        with zipfile.ZipFile(path) as zf:
            for idx, zi in enumerate(zf.infolist()):
                if zi.is_dir() or zi.file_size > MAX_MEMBER:
                    continue
                if zi.flag_bits & 0x1:
                    continue  # encrypted member
                tgt = os.path.join(dest, "%06d" % idx)
                with zf.open(zi) as src, open(tgt, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                out.append((zi.filename, tgt))
    elif kind == "tar":
        with tarfile.open(path) as tf:
            for idx, ti in enumerate(tf):
                if not ti.isfile() or ti.size > MAX_MEMBER:
                    continue
                src = tf.extractfile(ti)
                if src is None:
                    continue
                tgt = os.path.join(dest, "%06d" % idx)
                with src, open(tgt, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                out.append((ti.name, tgt))
    elif kind == "gz":
        base = name.rsplit("/", 1)[-1]
        low = base.lower()
        inner = base[:-3] if low.endswith(".gz") else (base[:-4] + ".tar" if low.endswith(".tgz") else base + ".out")
        tgt = os.path.join(dest, "000000")
        with gzip.open(path, "rb") as src, open(tgt, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        out.append((inner, tgt))
    elif kind == "Z":
        base = name.rsplit("/", 1)[-1]
        inner = base[:-2] if base.lower().endswith(".z") else base + ".out"
        tgt = os.path.join(dest, "000000")
        with open(tgt, "wb") as dst:
            subprocess.run(["gzip", "-dc", path], stdout=dst, stderr=subprocess.DEVNULL, check=True,
                           timeout=600)
        out.append((inner, tgt))
    elif kind in ("7z", "rar"):
        sub = os.path.join(dest, "x")
        os.makedirs(sub)
        subprocess.run(["7z", "x", "-y", "-bd", "-o" + sub, path], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True, timeout=1800)
        for dp, _dn, fns in os.walk(sub):
            for fn in sorted(fns):
                full = os.path.join(dp, fn)
                if os.path.islink(full):
                    continue
                out.append((os.path.relpath(full, sub), full))
    else:
        raise ValueError("not an archive kind: %s" % kind)
    return out


ARCHIVE_KINDS = ("zip", "tar", "gz", "Z", "7z", "rar")
