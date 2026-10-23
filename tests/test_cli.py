import json
import os

import blake3
import pytest

from fsmanifest.cli import main


def write(path, data, mtime_ns=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def read_manifest(directory):
    return json.loads((directory / ".manifest").read_text())["files"]


def digest(data):
    return blake3.blake3(data).hexdigest()


@pytest.fixture
def tree(tmp_path):
    write(tmp_path / "a.txt", b"aaa")
    write(tmp_path / ".hidden", b"secret")
    write(tmp_path / "sub" / "b.txt", b"bbb")
    write(tmp_path / "sub" / "deep" / "c.txt", b"ccc")
    (tmp_path / "empty").mkdir()
    write(tmp_path / "onlyhidden" / ".x", b"x")
    write(tmp_path / ".hiddendir" / "d.txt", b"ddd")
    return tmp_path


@pytest.mark.parametrize("cmd", ["full-sync", "sync"])
def test_directory_creates_manifests(tree, cmd):
    assert main([cmd, str(tree)]) == 0
    files = read_manifest(tree)
    assert set(files) == {"a.txt"}
    assert files["a.txt"]["blake3"] == digest(b"aaa")
    assert files["a.txt"]["mtime_ns"] == (tree / "a.txt").stat().st_mtime_ns
    assert files["a.txt"]["size"] == 3
    assert set(read_manifest(tree / "sub")) == {"b.txt"}
    assert set(read_manifest(tree / "sub" / "deep")) == {"c.txt"}
    assert not (tree / "empty" / ".manifest").exists()
    assert not (tree / "onlyhidden" / ".manifest").exists()
    assert not (tree / ".hiddendir" / ".manifest").exists()


@pytest.mark.parametrize("cmd", ["full-sync", "sync"])
def test_removes_deleted_entries(tree, cmd):
    main(["full-sync", str(tree)])
    write(tree / "new.txt", b"new")
    (tree / "a.txt").unlink()
    assert main([cmd, str(tree)]) == 0
    assert set(read_manifest(tree)) == {"new.txt"}


def test_manifest_removed_when_directory_emptied(tree):
    main(["full-sync", str(tree)])
    (tree / "sub" / "b.txt").unlink()
    main(["sync", str(tree)])
    assert not (tree / "sub" / ".manifest").exists()


def test_sync_skips_unmodified_but_full_sync_rehashes(tree):
    f = write(tree / "a.txt", b"aaa", mtime_ns=1_000_000_000)
    main(["full-sync", str(tree)])
    # Change content but keep the mtime: sync must not notice, full-sync must.
    write(f, b"zzz", mtime_ns=1_000_000_000)
    main(["sync", str(tree)])
    assert read_manifest(tree)["a.txt"]["blake3"] == digest(b"aaa")
    main(["full-sync", str(tree)])
    assert read_manifest(tree)["a.txt"]["blake3"] == digest(b"zzz")


def test_sync_rehashes_newer_files(tree):
    f = write(tree / "a.txt", b"aaa", mtime_ns=1_000_000_000)
    main(["sync", str(tree)])
    write(f, b"zzz", mtime_ns=2_000_000_000)
    main(["sync", str(tree)])
    entry = read_manifest(tree)["a.txt"]
    assert entry == {"blake3": digest(b"zzz"), "size": 3, "mtime_ns": 2_000_000_000}


@pytest.mark.parametrize("cmd", ["full-sync", "sync"])
def test_single_file(tree, cmd):
    assert main([cmd, str(tree / "a.txt")]) == 0
    assert set(read_manifest(tree)) == {"a.txt"}
    write(tree / "other.txt", b"o")
    assert main([cmd, str(tree / "other.txt")]) == 0
    assert set(read_manifest(tree)) == {"a.txt", "other.txt"}


def test_single_file_sync_respects_mtime(tree):
    f = write(tree / "a.txt", b"aaa", mtime_ns=1_000_000_000)
    main(["sync", str(f)])
    write(f, b"zzz", mtime_ns=1_000_000_000)
    main(["sync", str(f)])
    assert read_manifest(tree)["a.txt"]["blake3"] == digest(b"aaa")
    main(["full-sync", str(f)])
    assert read_manifest(tree)["a.txt"]["blake3"] == digest(b"zzz")


def test_single_deleted_file_removes_entry(tree):
    main(["sync", str(tree)])
    write(tree / "b.txt", b"b")
    main(["sync", str(tree / "b.txt")])
    (tree / "a.txt").unlink()
    assert main(["sync", str(tree / "a.txt")]) == 0
    assert set(read_manifest(tree)) == {"b.txt"}


def test_single_file_errors(tree):
    assert main(["sync", str(tree / "nope.txt")]) == 2
    assert main(["sync", str(tree / ".hidden")]) == 2
    assert not (tree / ".manifest").exists()


def test_verify_ok(tree, capsys):
    main(["sync", str(tree)])
    assert main(["verify", str(tree)]) == 0
    assert main(["verify", str(tree / "sub" / "b.txt")]) == 0


def test_verify_detects_problems(tree, capsys):
    write(tree / "a.txt", b"aaa", mtime_ns=1_000_000_000)
    write(tree / "sub" / "b.txt", b"bbb", mtime_ns=1_000_000_000)
    main(["sync", str(tree)])
    write(tree / "a.txt", b"rot", mtime_ns=1_000_000_000)          # corrupt
    write(tree / "sub" / "b.txt", b"edit", mtime_ns=2_000_000_000)  # modified
    (tree / "sub" / "deep" / "c.txt").unlink()                     # missing
    capsys.readouterr()
    assert main(["verify", str(tree)]) == 1
    out = capsys.readouterr().out
    assert "CORRUPT" in out and "a.txt" in out
    assert "MODIFIED" in out and "b.txt" in out
    assert "MISSING" in out and "c.txt" in out
    assert main(["verify", str(tree / "a.txt")]) == 1


def test_verify_single_file_without_entry(tree):
    assert main(["verify", str(tree / "a.txt")]) == 2
    main(["sync", str(tree)])
    write(tree / "untracked.txt", b"u")
    assert main(["verify", str(tree / "untracked.txt")]) == 2
    # Untracked files are reported but don't fail a directory verify.
    assert main(["verify", str(tree)]) == 0


def test_corrupt_manifest_is_regenerated(tree):
    (tree / ".manifest").write_text("not json")
    assert main(["sync", str(tree)]) == 2
    assert set(read_manifest(tree)) == {"a.txt"}
    assert main(["verify", str(tree)]) == 0


def test_size_is_apparent_size_of_sparse_file(tmp_path):
    f = tmp_path / "sparse.bin"
    with open(f, "wb") as fh:
        fh.truncate(10 * 1024 * 1024)
    main(["sync", str(tmp_path)])
    assert read_manifest(tmp_path)["sparse.bin"]["size"] == 10 * 1024 * 1024


def test_sync_rehashes_on_size_change_with_same_mtime(tree):
    f = write(tree / "a.txt", b"aaa", mtime_ns=1_000_000_000)
    main(["sync", str(tree)])
    write(f, b"longer", mtime_ns=1_000_000_000)
    main(["sync", str(tree)])
    assert read_manifest(tree)["a.txt"] == {
        "blake3": digest(b"longer"), "size": 6, "mtime_ns": 1_000_000_000}


def test_verify_reports_size_mismatch(tree, capsys):
    f = write(tree / "a.txt", b"aaa", mtime_ns=1_000_000_000)
    main(["sync", str(tree)])
    write(f, b"truncated-and-longer", mtime_ns=1_000_000_000)
    capsys.readouterr()
    assert main(["verify", str(f)]) == 1
    out = capsys.readouterr().out
    assert "CORRUPT" in out and "size 20 != recorded 3" in out


def test_reads_v1_manifest_and_upgrades(tree):
    f = write(tree / "a.txt", b"aaa", mtime_ns=1_000_000_000)
    (tree / ".manifest").write_text(json.dumps({
        "version": 1, "algorithm": "blake3",
        "files": {"a.txt": {"blake3": digest(b"aaa"), "mtime_ns": 1_000_000_000}},
    }))
    assert main(["verify", str(tree)]) == 0
    assert main(["sync", str(f)]) == 0
    data = json.loads((tree / ".manifest").read_text())
    assert data["version"] == 2
    assert data["files"]["a.txt"]["size"] == 3
