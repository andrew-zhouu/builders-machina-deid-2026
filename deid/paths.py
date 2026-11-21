"""Path renaming (used only with transform.py --rename).

A path is renamed by passing each component through the same engine that
rewrites file contents, with the same source-group scoping. Because one
identity always maps to one pseudonym of the same length, a file name and
every reference to it inside other files change in exactly the same way, so
assemblies, drawings and their components keep pointing at each other.

Never changed: the two leading components (sources/<origin>), file
extensions (including Creo version suffixes), commit hashes, and any
component that carries no dictionary identity.
"""
import re

from . import util

_HASH = re.compile(r"^[0-9a-f]{7,40}$")
_VERSION = re.compile(r"(\.\d+)$")


def split_name(base):
    """(stem, extension-with-dot) keeping multi-part CAD extensions intact."""
    m = _VERSION.search(base)
    tail = ""
    if m and "." in base[:m.start()]:
        tail = m.group(1)
        base = base[:m.start()]
    if "." in base and not base.startswith("."):
        stem, ext = base.rsplit(".", 1)
        return stem, "." + ext + tail
    return base, tail


def _text(eng, s, ctx="text"):
    return eng.replace_text(s, ctx)[0] if s else s


def rename_file_component(eng, comp):
    stem, ext = split_name(comp)
    if stem.startswith("~$"):
        return comp
    return _text(eng, stem) + ext


def rename_dir_component(eng, comp):
    # "<name>.extracted" and "<archive>.zip" siblings must stay aligned
    for suf in (".extracted",):
        if comp.endswith(suf):
            return rename_file_component(eng, comp[:-len(suf)]) + suf
    return rename_file_component(eng, comp) if "." in comp else _text(eng, comp)


def rename_top(eng, origin, top, owner_only=False):
    """Top-level source directory (or archive file) name. With owner_only
    just the account part of "<account>__<repository>" (or the web domain)
    is renamed."""
    if origin in ("github", "gitlab", "archives"):
        stem, ext = (top, "")
        if origin == "archives":
            stem, ext = split_name(top)
        parts = stem.split("__")
        out = []
        i = 0
        if origin == "gitlab" and parts and parts[0] == "gitlab.com":
            out.append(parts[0])
            i = 1
        owner_done = False
        owner_tokens = None
        for j in range(i, len(parts)):
            p = parts[j]
            if not p:
                out.append(p)
            elif _HASH.match(p) and j == len(parts) - 1 and j > i:
                out.append(p)
            elif not owner_done:
                out.append(_text(eng, p, "id"))
                owner_done = True
                owner_tokens = p
            elif owner_only:
                out.append(p)
            else:
                q = _text(eng, p)
                if owner_tokens and owner_tokens.lower() in q.lower():
                    # the owner's own name inside the repository name
                    k = q.lower().find(owner_tokens.lower())
                    q = q[:k] + _text(eng, q[k:k + len(owner_tokens)], "id") + q[k + len(owner_tokens):]
                out.append(q)
        return "__".join(out) + ext
    if owner_only and origin != "web":
        return top
    # web domains and descriptive catalogue names: the whole name is text
    return _text(eng, top)


def map_path(eng, rel, level="all"):
    """New relative path for corpus path `rel` (engine group must be set).

    level "owner": only the account part of the top-level source directory
    (and web domains); "top": the whole top-level directory name (account,
    repository, domain, catalogue name); "all": every component."""
    parts = rel.split("/")
    if len(parts) < 2:
        return rel
    if parts[0] == "archives":
        return "/".join([parts[0], rename_top(eng, "archives", parts[1], level == "owner")] + parts[2:])
    if parts[0] != "sources" or len(parts) < 3:
        return rel
    out = parts[:2]
    if len(parts) == 3:
        out.append(rename_file_component(eng, parts[2]) if level == "all" else parts[2])
        return "/".join(out)
    out.append(rename_top(eng, parts[1], parts[2], level == "owner"))
    if level != "all":
        return "/".join(out + parts[3:])
    for c in parts[3:-1]:
        out.append(rename_dir_component(eng, c))
    out.append(rename_file_component(eng, parts[-1]))
    return "/".join(out)


def map_member(eng, name):
    """New name for an archive member path (forward slashes)."""
    parts = name.split("/")
    out = []
    for i, c in enumerate(parts):
        if not c:
            out.append(c)
        elif i == len(parts) - 1:
            out.append(rename_file_component(eng, c))
        else:
            out.append(rename_dir_component(eng, c))
    return "/".join(out)
