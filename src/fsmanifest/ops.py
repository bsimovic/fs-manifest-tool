"""The full-sync, sync and verify operations."""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

from . import manifest
from .manifest import MANIFEST_NAME, Entry, ManifestError


class Reporter:
    """Collects per-file results and prints them according to verbosity."""

    def __init__(self, verbosity: int = 1, out=None, err=None):
        self.verbosity = verbosity
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.counts: Counter[str] = Counter()
        self.errors = 0

    def item(self, status: str, path: Path, *, level: int = 1, detail: str = "") -> None:
        self.counts[status] += 1
        if self.verbosity >= level:
            line = f"{status.upper():<10} {path}"
            print(f"{line}  ({detail})" if detail else line, file=self.out)

    def error(self, message: str) -> None:
        self.errors += 1
        print(f"error: {message}", file=self.err)

    def summary(self) -> None:
        if self.verbosity < 1:
            return
        parts = [f"{n} {status}" for status, n in sorted(self.counts.items())]
        if self.errors:
            parts.append(f"{self.errors} errors")
        print(", ".join(parts) or "nothing to do", file=self.out)


def walk_dirs(root: Path, rep: Reporter):
    """Yield `root` and every non-hidden subdirectory below it (symlinks not followed)."""

    def onerror(exc: OSError) -> None:
        rep.error(f"{exc.filename}: {exc.strerror}")

    for dirpath, dirnames, _ in os.walk(root, onerror=onerror):
        dirnames[:] = sorted(d for d in dirnames if not manifest.is_hidden(d))
        yield Path(dirpath)


def _hash_entry(path: Path, st: os.stat_result) -> Entry:
    # Record the mtime observed *before* hashing: if the file changes while it
    # is being read, the next sync sees a newer mtime and hashes it again.
    return Entry(blake3=manifest.hash_file(path), size=st.st_size, mtime_ns=st.st_mtime_ns)


def _needs_hash(full: bool, old: Entry | None, st: os.stat_result) -> bool:
    # A size change is a sure sign of new content even if the mtime didn't move.
    return (full or old is None or st.st_mtime_ns > old.mtime_ns
            or st.st_size != old.size)


def _update_entry(directory: Path, name: str, st: os.stat_result, old: Entry | None,
                  full: bool, rep: Reporter) -> Entry | None:
    """Return the new entry for a file (hashing it if needed), or None on error."""
    path = directory / name
    if not _needs_hash(full, old, st):
        rep.item("unchanged", path, level=2)
        return old
    try:
        new = _hash_entry(path, st)
    except OSError as exc:
        rep.error(f"{path}: {exc.strerror}")
        return old
    if old is None:
        rep.item("added", path)
    elif old.blake3 != new.blake3:
        rep.item("updated", path)
    else:
        rep.item("unchanged", path, level=2)
    return new


def sync_directory(directory: Path, full: bool, rep: Reporter) -> None:
    """(Re)build the manifest of a single directory."""
    try:
        existing = manifest.load(directory)
    except ManifestError as exc:
        # An unreadable manifest is rebuilt from scratch rather than aborting.
        rep.error(f"{exc}; regenerating")
        existing = None
    try:
        files = manifest.tracked_files(directory)
    except OSError as exc:
        rep.error(f"{directory}: {exc.strerror}")
        return

    old_entries = existing or {}
    entries: dict[str, Entry] = {}
    for name, st in sorted(files.items()):
        entry = _update_entry(directory, name, st, old_entries.get(name), full, rep)
        if entry is not None:
            entries[name] = entry
    for name in sorted(old_entries.keys() - files.keys()):
        rep.item("removed", directory / name)

    if entries != existing and (entries or existing is not None):
        try:
            manifest.save(directory, entries)
        except OSError as exc:
            rep.error(f"{directory / MANIFEST_NAME}: cannot write: {exc.strerror}")


def sync_file(path: Path, full: bool, rep: Reporter) -> None:
    """Add, update or remove the manifest entry for a single file."""
    directory, name = path.parent, path.name
    try:
        existing = manifest.load(directory)
    except ManifestError as exc:
        rep.error(str(exc))
        return
    entries = dict(existing or {})
    old = entries.get(name)

    try:
        st = path.lstat()
    except FileNotFoundError:
        if old is None:
            rep.error(f"{path}: no such file")
            return
        del entries[name]
        rep.item("removed", path)
    else:
        if not path.is_file() or path.is_symlink():
            rep.error(f"{path}: not a regular file")
            return
        entry = _update_entry(directory, name, st, old, full, rep)
        if entry is None:
            return
        entries[name] = entry

    if entries != existing:
        try:
            manifest.save(directory, entries)
        except OSError as exc:
            rep.error(f"{directory / MANIFEST_NAME}: cannot write: {exc.strerror}")


def sync(target: Path, full: bool, rep: Reporter) -> None:
    if target.is_dir():
        for directory in walk_dirs(target, rep):
            sync_directory(directory, full, rep)
    else:
        sync_file(target, full, rep)


def verify_entry(path: Path, entry: Entry, rep: Reporter) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        rep.item("missing", path, level=0)
        return
    if not path.is_file() or path.is_symlink():
        rep.item("missing", path, level=0, detail="not a regular file")
        return
    if entry.size is not None and st.st_size != entry.size:
        # Different length means different content; no need to hash.
        mismatch = f"size {st.st_size} != recorded {entry.size}"
    else:
        try:
            digest = manifest.hash_file(path)
        except OSError as exc:
            rep.error(f"{path}: {exc.strerror}")
            return
        if digest == entry.blake3:
            rep.item("ok", path, level=2)
            return
        mismatch = "hash mismatch"
    if st.st_mtime_ns > entry.mtime_ns:
        # Content changed and so did the mtime: most likely a legitimate edit.
        rep.item("modified", path, level=0, detail=f"{mismatch}, changed since last sync")
    else:
        # Content changed but the mtime did not: silent corruption.
        rep.item("corrupt", path, level=0, detail=f"{mismatch}, mtime unchanged")


def verify_directory(directory: Path, rep: Reporter) -> None:
    try:
        entries = manifest.load(directory)
    except ManifestError as exc:
        rep.error(str(exc))
        return
    if entries is None:
        return
    for name, entry in sorted(entries.items()):
        verify_entry(directory / name, entry, rep)
    try:
        untracked = manifest.tracked_files(directory).keys() - entries.keys()
    except OSError:
        return
    for name in sorted(untracked):
        rep.item("untracked", directory / name, level=1)


def verify(target: Path, rep: Reporter) -> None:
    if target.is_dir():
        for directory in walk_dirs(target, rep):
            verify_directory(directory, rep)
        return
    try:
        entries = manifest.load(target.parent)
    except ManifestError as exc:
        rep.error(str(exc))
        return
    if entries is None:
        rep.error(f"{target.parent / MANIFEST_NAME}: no manifest")
    elif target.name not in entries:
        rep.error(f"{target}: no entry in manifest")
    else:
        verify_entry(target, entries[target.name], rep)


VERIFY_FAILURES = ("missing", "modified", "corrupt")


def _format_size(size: int | None) -> str:
    return "?" if size is None else f"{size:,}"


def _manifest_tree(directory: Path, rep: Reporter):
    """Return (entries, children) for `directory`, or None if no manifest is at or below it.

    `children` is a list of (name, subtree) for non-hidden subdirectories that
    themselves contain a manifest somewhere below them.
    """
    try:
        entries = manifest.load(directory)
    except ManifestError as exc:
        rep.error(str(exc))
        entries = None
    try:
        with os.scandir(directory) as it:
            subdirs = sorted(e.name for e in it
                             if not manifest.is_hidden(e.name) and e.is_dir(follow_symlinks=False))
    except OSError as exc:
        rep.error(f"{directory}: {exc.strerror}")
        subdirs = []
    children = []
    for name in subdirs:
        subtree = _manifest_tree(directory / name, rep)
        if subtree is not None:
            children.append((name, subtree))
    if entries is None and not children:
        return None
    return entries or {}, children


def _tree_rows(tree, prefix: str, rows: list) -> None:
    entries, children = tree
    items = [(name, entry) for name, entry in sorted(entries.items())]
    items += [(name + "/", subtree) for name, subtree in children]
    for i, (name, item) in enumerate(items):
        last = i == len(items) - 1
        label = prefix + ("└── " if last else "├── ") + name
        if isinstance(item, Entry):
            rows.append((label, _format_size(item.size), item.blake3))
        else:
            rows.append((label, "", ""))
            _tree_rows(item, prefix + ("    " if last else "│   "), rows)


def _print_rows(rows: list, rep: Reporter) -> None:
    # Directory rows have no size/hash and don't count towards column widths.
    files = [r for r in rows if r[2]]
    name_w = max((len(r[0]) for r in files), default=0)
    size_w = max((len(r[1]) for r in files), default=0)
    for label, size, digest in rows:
        line = f"{label:<{name_w}}  {size:>{size_w}}  {digest}" if digest else label
        print(line.rstrip(), file=rep.out)


def list_entries(target: Path, rep: Reporter) -> None:
    """Print what the manifests record; never touches or creates manifests."""
    if target.is_dir():
        tree = _manifest_tree(target, rep)
        if tree is None:
            rep.item("not found", target, level=0, detail="no manifests")
            return
        rows = [(str(target).rstrip(os.sep) + os.sep, "", "")]
        _tree_rows(tree, "", rows)
        _print_rows(rows, rep)
        return
    try:
        entries = manifest.load(target.parent) or {}
    except ManifestError as exc:
        rep.error(str(exc))
        return
    entry = entries.get(target.name)
    if entry is None:
        rep.item("not found", target, level=0, detail="no manifest entry")
    else:
        _print_rows([(str(target), _format_size(entry.size), entry.blake3)], rep)
