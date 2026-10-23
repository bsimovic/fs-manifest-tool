"""Command-line interface."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__, manifest, ops

EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_NOT_FOUND = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fsmanifest",
        description="Maintain and verify per-directory .manifest files of BLAKE3 hashes.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true",
                           help="also list unchanged / verified files")
    verbosity.add_argument("-q", "--quiet", action="store_true",
                           help="only print problems")

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, help_text in [
        ("full-sync", "rehash every file and rewrite manifests"),
        ("sync", "hash only new files and files modified since the last sync"),
        ("verify", "check files against their recorded hashes"),
        ("list", "print the names, sizes and hashes recorded in manifests"),
    ]:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument("paths", nargs="+", type=Path, metavar="PATH",
                       help="directory (processed recursively) or single file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rep = ops.Reporter(verbosity=0 if args.quiet else 2 if args.verbose else 1)

    for path in args.paths:
        if not path.is_dir() and manifest.is_hidden(path.name):
            rep.error(f"{path}: hidden files are not tracked")
            continue
        if not path.is_dir() and not path.parent.is_dir():
            rep.error(f"{path}: no such file or directory")
            continue
        if args.command == "verify":
            ops.verify(path, rep)
        elif args.command == "list":
            ops.list_entries(path, rep)
        else:
            ops.sync(path, full=args.command == "full-sync", rep=rep)

    if args.command != "list":
        rep.summary()
    if rep.errors:
        return EXIT_ERROR
    if args.command == "verify" and any(rep.counts[s] for s in ops.VERIFY_FAILURES):
        return EXIT_VERIFY_FAILED
    if args.command == "list" and rep.counts["not found"]:
        return EXIT_NOT_FOUND
    return EXIT_OK
