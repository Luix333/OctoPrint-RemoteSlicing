"""On-demand Dropbox folder listing and file resolution."""

import os
import stat

ALLOWED = (".stl", ".sldprt")
MAX_ENTRIES = 2000


def normalize(rel):
    """Normalize a relative path from the Dropbox root.

    Returns a forward-slash joined path string (empty string for root).
    Raises ValueError for UNC paths, drive letters, or ``..`` components.
    """
    if rel is None:
        raise ValueError("bad path")

    # Check for UNC / two leading separators
    if len(rel) >= 2 and rel[0] in ("\\", "/") and rel[1] in ("\\", "/"):
        raise ValueError("bad path")

    # Strip a single leading separator (root-relative)
    if rel and rel[0] in ("\\", "/"):
        rel = rel[1:]

    # Split on both forward and back slashes
    parts = []
    for part in rel.replace("\\", "/").split("/"):
        if part == "" or part == ".":
            continue
        if ":" in part:
            raise ValueError("bad path")
        if part == "..":
            raise ValueError("bad path")
        parts.append(part)

    return "/".join(parts)


def _resolve(root, rel):
    """Resolve *rel* under *root* and ensure the result stays inside root."""
    norm = normalize(rel)
    if norm:
        parts = norm.split("/")
    else:
        parts = []
    target = os.path.realpath(os.path.join(root, *parts))
    root_real = os.path.realpath(root)
    norm_target = os.path.normcase(target)
    norm_root = os.path.normcase(root_real)
    if norm_target != norm_root and not norm_target.startswith(norm_root + os.sep):
        raise ValueError("bad path")
    return target


def list_folder(root, rel):
    """List a folder inside the Dropbox root.

    Returns a dict with ``path``, ``dirs``, ``files``, and ``truncated``.
    """
    target = _resolve(root, rel)

    if not os.path.isdir(target):
        raise ValueError("not a folder")

    try:
        iterator = os.scandir(target)
    except OSError:
        raise ValueError("not a folder")

    entries = []
    with iterator:
        _collect(iterator, entries)
    return _result(rel, entries)


def _collect(iterator, entries):
    """Append ("d", name) / ("f", name, size, mtime) tuples for the visible entries."""
    for entry in iterator:
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue  # skip entries whose stat fails

        # Skip hidden / temp names
        if entry.name.startswith(".") or entry.name.startswith("~$"):
            continue

        # Skip Windows hidden/system attributes (when available)
        try:
            attr = entry.stat().st_file_attributes
            if attr & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM):
                continue
        except AttributeError:
            pass  # st_file_attributes not available on non-Windows
        except OSError:
            continue

        if is_dir:
            entries.append(("d", entry.name))
        else:
            ext = os.path.splitext(entry.name)[1].lower()
            if ext in ALLOWED:
                try:
                    info = entry.stat()
                except OSError:
                    continue
                entries.append(("f", entry.name, info.st_size, info.st_mtime))


def _result(rel, entries):
    """Sort, cap at MAX_ENTRIES and shape the listing."""
    truncated = False
    # Apply MAX_ENTRIES limit
    if len(entries) > MAX_ENTRIES:
        entries = entries[:MAX_ENTRIES]
        truncated = True

    # Sort case-insensitively
    dir_entries = sorted([e for e in entries if e[0] == "d"], key=lambda e: e[1].lower())
    file_entries = sorted([e for e in entries if e[0] == "f"], key=lambda e: e[1].lower())

    return {
        "path": normalize(rel),
        "dirs": [{"name": d[1]} for d in dir_entries],
        "files": [{"name": f[1], "size": f[2], "mtime": f[3]} for f in file_entries],
        "truncated": truncated,
    }


def resolve_file(root, rel):
    """Resolve a model file under the Dropbox root.

    Returns the absolute path of an existing regular file with an allowed
    extension, or raises ``ValueError("not a model file")``.
    """
    target = _resolve(root, rel)
    if not os.path.isfile(target):
        raise ValueError("not a model file")
    ext = os.path.splitext(target)[1].lower()
    if ext not in ALLOWED:
        raise ValueError("not a model file")
    return target
