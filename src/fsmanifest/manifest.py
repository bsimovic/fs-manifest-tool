"""Reading, writing and updating `.manifest` files.

A manifest lives in a directory and describes the regular, non-hidden files
directly inside it (not in subdirectories). It is a JSON document:

    {
      "version": 1,
      "algorithm": "blake3",
      "files": {
        "photo.jpg": {"blake3": "<64 hex chars>", "mtime_ns": 1729700000000000000}
      }
    }

`mtime_ns` is the file's modification time in nanoseconds since the epoch,
as reported by the filesystem when the hash was computed.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import blake3

MANIFEST_NAME = ".manifest"
FORMAT_VERSION = 1
CHUNK_SIZE = 1 << 20


class ManifestError(Exception):
    pass


@dataclass
class Entry:
    blake3: str
    mtime_ns: int


def is_hidden(name: str) -> bool:
    return name.startswith(".")


def hash_file(path: Path) -> str:
    hasher = blake3.blake3(max_threads=blake3.blake3.AUTO)
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def tracked_files(directory: Path) -> dict[str, os.stat_result]:
    """Regular, non-hidden files directly inside `directory`, with their stats."""
    files = {}
    with os.scandir(directory) as it:
        for e in it:
            if not is_hidden(e.name) and e.is_file(follow_symlinks=False):
                files[e.name] = e.stat(follow_symlinks=False)
    return files


def load(directory: Path) -> dict[str, Entry] | None:
    """Load the manifest in `directory`, or return None if there is none."""
    path = directory / MANIFEST_NAME
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ManifestError(f"{path}: cannot read manifest: {exc}") from exc

    if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
        raise ManifestError(f"{path}: unsupported manifest format")
    try:
        return {
            name: Entry(blake3=str(e["blake3"]), mtime_ns=int(e["mtime_ns"]))
            for name, e in data["files"].items()
        }
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ManifestError(f"{path}: malformed manifest entry: {exc}") from exc


def save(directory: Path, entries: dict[str, Entry]) -> None:
    """Atomically write the manifest, or remove it if there are no entries."""
    path = directory / MANIFEST_NAME
    if not entries:
        path.unlink(missing_ok=True)
        return
    data = {
        "version": FORMAT_VERSION,
        "algorithm": "blake3",
        "files": {
            name: {"blake3": e.blake3, "mtime_ns": e.mtime_ns}
            for name, e in sorted(entries.items())
        },
    }
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=MANIFEST_NAME + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
