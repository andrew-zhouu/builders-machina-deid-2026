"""Shared configuration, file-kind detection and small helpers."""
import os
import re

HOME = os.path.expanduser("~")
INPUT = os.environ.get("DEID_INPUT", os.path.join(HOME, "machina", "input"))
WORK = os.environ.get("DEID_WORK", os.path.join(HOME, "machina", "work"))
OUT = os.environ.get("DEID_OUT", os.path.join(HOME, "machina", "out", "corpus"))
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

EXT_KIND = {
    "step": "step", "stp": "step", "p21": "step",
    "igs": "iges", "iges": "iges",
    "sat": "text", "dxf": "dxf", "svg": "svg", "pdf": "pdf",
    "sldprt": "sw", "sldasm": "sw", "slddrw": "sw", "prtdot": "sw", "asmdot": "sw",
    "drwdot": "sw", "sldlfp": "sw", "slddrt": "sw",
    "ipt": "ole", "iam": "ole", "idw": "ole", "ipn": "ole", "par": "ole", "psm": "ole",
    "dft": "ole",
    "fcstd": "zipn", "3mf": "zipn", "f3d": "zipn", "f3z": "zipn", "odg": "zipn",
    "zip": "zip", "gz": "gz", "tgz": "gz", "z": "Z", "tar": "tar", "7z": "7z", "rar": "rar",
    "png": "img", "jpg": "img", "jpeg": "img", "tif": "img", "tiff": "img", "gif": "img",
    "bmp": "img",
    "stl": "stl",
    "obj": "text", "mtl": "text", "wrl": "text", "x_t": "text", "xmt_txt": "text",
    "brep": "text", "brp": "text", "unv": "text", "neu": "text", "ps": "text", "eps": "text",
    "off": "text", "scad": "text", "ipj": "text", "txt": "text", "md": "text", "csv": "text",
    "json": "text", "xml": "text", "html": "text", "htm": "text", "asm": "text", "prt": "text",
    "drw": "text", "py": "text", "c": "text", "h": "text", "cpp": "text", "ini": "text",
    "cfg": "text", "yml": "text", "yaml": "text", "gbr": "text", "drl": "text", "kicad_pcb": "text",
    "kicad_sch": "text", "kicad_pro": "text", "kicad_prl": "text", "gcode": "text", "nc": "text",
    "prg": "text", "dat": "text", "log": "text", "bat": "text", "sh": "text", "tex": "text",
    "dwg": "dwg", "jt": "jt", "catpart": "catia", "catproduct": "catia", "catdrawing": "catia",
    "x_b": "bin", "xmt_bin": "bin", "u3d": "bin", "3ds": "bin",
}

_TEXT_OK = bytes(range(32, 127)) + b"\t\r\n\x0c\x1b\x08"


def split_ext(name):
    """Return the logical extension (lower case, no dot); Creo version suffixes
    (.prt.12) are stripped so 'a.prt.12' -> 'prt'."""
    base = name.rsplit("/", 1)[-1]
    parts = base.split(".")
    if len(parts) < 2:
        return ""
    ext = parts[-1].lower()
    if ext.isdigit() and len(parts) >= 3:
        ext = parts[-2].lower()
    return ext


def looks_text(head):
    if not head:
        return False
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return True
    if b"\x00" in head:
        return False
    bad = len(head.translate(None, _TEXT_OK))
    high = sum(1 for b in head if b >= 0x80)
    return (bad - high) <= len(head) * 0.02


def kind_of(name, head):
    """Classify a file from its name and first bytes."""
    ext = split_ext(name)
    kind = EXT_KIND.get(ext)
    if not head:
        return "empty"
    if head.startswith(OLE_MAGIC):
        return "ole"
    if head.startswith(b"%PDF") or (ext == "pdf" and b"%PDF" in head[:1024]):
        return "pdf"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zipn" if kind == "zipn" else "zip"
    if head.startswith(b"#UGC:"):
        return "creo"
    if head.startswith(b"\x1f\x8b"):
        return "gz"
    if head.startswith(b"\x1f\x9d"):
        return "Z"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith(b"Rar!"):
        return "rar"
    if head.startswith(b"SPLMSSTR"):
        return "bin"
    if kind == "sw":
        base = name.rsplit("/", 1)[-1]
        if base.startswith("~$") and len(head) <= 256:
            return "swlock"      # SolidWorks lock file: holds the user name of whoever had it open
        return "sw" if not looks_text(head[:512]) else "text"
    if kind == "tar" or (len(head) > 262 and head[257:262] == b"ustar"):
        return "tar"
    if kind == "step":
        return "step" if b"ISO-10303-21" in head[:2048] else ("text" if looks_text(head[:2048]) else "bin")
    if kind == "iges":
        return "iges" if looks_text(head[:2048]) else "bin"
    if kind == "stl":
        return "stl"
    if kind in ("dxf", "svg", "text"):
        return kind if looks_text(head[:4096]) else "bin"
    if kind is None:
        return "text" if looks_text(head[:4096]) else "bin"
    return kind


def group_of(relpath):
    """Top-level source unit of a corpus path (owner__repo, web domain, ...)."""
    parts = relpath.split("/")
    if parts[0] == "sources" and len(parts) > 3:
        if parts[1] == "existing":
            return "/".join(parts[:min(len(parts) - 1, 5)])
        return "/".join(parts[:3])
    return "/".join(parts[:2])


def walk_input(root=None):
    """Yield (relpath, size) for every corpus file under root, sorted."""
    root = root or INPUT
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath == root and ".get-input" in dirnames:
            dirnames.remove(".get-input")
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            out.append((os.path.relpath(full, root), st.st_size))
    out.sort()
    return out


def decode_best(b):
    """Decode bytes of unknown encoding for inspection (never used to write)."""
    if b[:2] == b"\xff\xfe":
        return b[2:].decode("utf-16-le", "replace")
    if b[:2] == b"\xfe\xff":
        return b[2:].decode("utf-16-be", "replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", "replace")


_WS = re.compile(r"\s+")


def squash(s):
    return _WS.sub(" ", s).strip()
