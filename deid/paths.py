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


_EXTENSION = re.compile(r"^[A-Za-z0-9_~$]{1,12}$")


def split_name(base):
    """(stem, extension-with-dot) keeping multi-part CAD extensions intact."""
    m = _VERSION.search(base)
    tail = ""
    if m and "." in base[:m.start()]:
        tail = m.group(1)
        base = base[:m.start()]
    if "." in base and not base.startswith("."):
        stem, ext = base.rsplit(".", 1)
        # an extension is short and plain; "GRBL-1.1g-Name-Of-Maker" has a
        # version number in it, not an extension
        if _EXTENSION.match(ext):
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

    level "gitowner": only the account part of sources/github and
    sources/gitlab folders; "owner": the account part of the top-level source
    directory (and web domains, archive names); "top": the whole top-level
    directory name (account, repository, domain, catalogue name); "all":
    every component."""
    parts = rel.split("/")
    if len(parts) < 2:
        return rel
    if level == "gitowner":
        # controlled experiment: only the account part of code-forge source folders
        if parts[0] == "sources" and len(parts) >= 4 and parts[1] in ("github", "gitlab"):
            return "/".join(parts[:2] + [rename_top(eng, parts[1], parts[2], True)] + parts[3:])
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


# ---------------------------------------------------------------------------
# scope: identities named by a file's own location
# ---------------------------------------------------------------------------
_HOST_GENERIC = frozenset(
    "www www2 ftp cdn static media files file docs doc download downloads public wiki blog mail api assets "
    "content contenthub img images shop store support portal web site sites home data repos repo dspace "
    "com org net edu gov ac co io uk de fr es it nl ch at jp cn in ca us au eu info dev app cloud online "
    "github gitlab wordpress blogspot dropbox windows core blob shopify amazonaws s3".split())


def _strong(eng, ids, whole):
    """Drop single short tokens (under four characters) from identities
    matched inside a longer name, unless the short token is the whole name:
    alone they are too ambiguous to be replaced everywhere in a folder."""
    from . import pseudo
    w = pseudo.canon(whole)
    out = set()
    for eid in ids:
        ent = eng.entries[eid]
        if len(ent.tokens) == 1 and len(ent.tokens[0]) < 4 and ent.tokens[0] != w:
            continue
        out.add(eid)
    return out


def _top_ids(eng, top):
    """Identities named by one top-level source folder ("sources/<origin>/<name>...")."""
    ids = set()
    parts = top.split("/")
    if len(parts) < 3 or parts[0] != "sources":
        if parts[0] == "archives" and len(parts) >= 2:
            stem = split_name(parts[1])[0]
            segs = [x for x in stem.split("__") if x]
            if segs:
                ids |= _strong(eng, eng.match_ids(segs[0], "id"), segs[0])
            for sg in segs[1:]:
                if not _HASH.match(sg):
                    ids |= eng.match_ids(sg, "text")
        return ids
    origin, name = parts[1], parts[2]
    if origin in ("github", "gitlab"):
        segs = [x for x in name.split("__") if x]
        if origin == "gitlab" and segs and segs[0] == "gitlab.com":
            segs = segs[1:]
        if segs:
            ids |= _strong(eng, eng.match_ids(segs[0], "id"), segs[0])   # the account, whatever its tier
        for sg in segs[1:]:
            if not _HASH.match(sg):
                ids |= eng.match_ids(sg, "text")
    elif origin == "web":
        ids |= eng.match_ids(name, "text")
        for label in name.lower().split("."):
            if label not in _HOST_GENERIC and len(label) >= 2:
                ids |= _strong(eng, eng.match_ids(label, "id"), label)   # the organisation / account label of the host
    elif origin == "existing":
        for c in parts[2:5]:                              # collection folders
            ids |= eng.match_ids(c, "text")
    else:
        ids |= eng.match_ids(name, "text")
    return ids


def _group_ids(eng, group):
    """Identities named by a source group's own folder and by the folders
    linked to it (cached per engine)."""
    cache = eng.__dict__.setdefault("_gscope", {})
    ids = cache.get(group)
    if ids is None:
        ids = set(_top_ids(eng, group + "/x"))
        for other in eng.links.get(group, ()):
            ids |= _top_ids(eng, other + "/x")
        cache[group] = ids
    return ids


def group_scope(eng, rel, copies=()):
    """Group-level scope of a file: the identities named by its top-level
    source folder, by the folders linked to it (folders that hold copies of
    the same files) and by the folders of the exact copies of this file.
    The same for every file of a group, so file names and the references to
    them change together; and the same for every copy of one document."""
    from . import util
    ids = _group_ids(eng, util.group_of(rel.split("!/", 1)[0]))
    if copies:
        ids = set(ids)
        for c in copies:
            ids |= _group_ids(eng, util.group_of(c))
    return ids


def scope_ids(eng, rel):
    """Document-level additions to the scope: identities found in the path
    components below the top-level folder and in archive member paths (under
    the usual free-text rules). Used for documents that are read by people
    (PDF), not for files whose strings are machine references."""
    ids = set()
    pieces = rel.split("!/")
    parts = pieces[0].split("/")
    rest = parts[3:] if (len(parts) >= 3 and parts[0] == "sources") else parts[1:]
    for c in rest:
        ids |= eng.match_ids(c, "text")
    for member in pieces[1:]:
        for c in member.split("/"):
            ids |= eng.match_ids(c, "text")
    return ids


def map_all(eng, rels, level, scope=False):
    """Yield (rel, new path) for every corpus path, in sorted order. With
    scope, each path is mapped under the group-level scope of its folder,
    exactly the scope its contents are processed with."""
    from . import util
    last = None
    for rel in sorted(rels):
        g = util.group_of(rel)
        if g != last:
            eng.group = g
            eng.extra_groups = frozenset()
            eng.scope_begin()
            if scope:
                eng.scope_promote(group_scope(eng, rel))
            last = g
        yield rel, map_path(eng, rel, level)
