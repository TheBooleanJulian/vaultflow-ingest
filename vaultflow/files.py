"""File operations: hashing, atomic copy, and the vault's name index for duplicate checks."""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from pathlib import Path
from typing import Callable

from .scan import SourceFile
from .util import CancelToken

CHUNK = 4 * 1024 * 1024
PART_SUFFIX = ".part"

OnBytes = Callable[[int], None]


def sha256_file(path: Path, cancel: CancelToken | None = None, on_bytes: OnBytes | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if cancel:
                cancel.check()
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            if on_bytes:
                on_bytes(len(chunk))
    return h.hexdigest()


def copy_file_atomic(src: Path, dst: Path, cancel: CancelToken | None = None,
                     on_bytes: OnBytes | None = None) -> None:
    """Copy src to dst via dst.part, so a stopped or failed copy never leaves a truncated
    file under the real name (the PowerShell version could). Keeps the source mtime."""
    part = dst.with_name(dst.name + PART_SUFFIX)
    try:
        with open(src, "rb") as fin, open(part, "wb") as fout:
            while True:
                if cancel:
                    cancel.check()
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
                if on_bytes:
                    on_bytes(len(chunk))
            fout.flush()
            os.fsync(fout.fileno())
        copy_times(src, part)
        os.replace(part, dst)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def copy_times(src: Path, dst: Path) -> None:
    st = src.stat()
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))


def unique_path(path: Path) -> Path:
    """name.ext -> name_1.ext, name_2.ext ... for a same-name file that is NOT a duplicate."""
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


class VaultIndex:
    """Every file already in the vault, by lowercase name. Filenames recycle (camera
    counters wrap), so a name match alone never means 'duplicate'."""

    def __init__(self, root: Path | str, cancel: CancelToken | None = None) -> None:
        self._by_name: dict[str, list[tuple[Path, int]]] = defaultdict(list)
        root = str(root)
        if os.path.isdir(root):
            for folder, _dirs, names in os.walk(root):
                if cancel:
                    cancel.check()
                for name in names:
                    if name.endswith(PART_SUFFIX):
                        continue
                    full = os.path.join(folder, name)
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    self._by_name[name.lower()].append((Path(full), size))

    def __len__(self) -> int:
        return len(self._by_name)

    def candidates(self, name: str) -> list[tuple[Path, int]]:
        return list(self._by_name.get(name.lower(), ()))

    def add(self, path: Path, size: int) -> None:
        self._by_name[path.name.lower()].append((path, size))


def find_duplicate(index: VaultIndex, src: SourceFile, cancel: CancelToken | None = None,
                   on_bytes: OnBytes | None = None) -> Path | None:
    """A vault file with the same name, size AND SHA-256 as src, else None.

    Name+size is a cheap pre-filter; the hash is only computed when both match.
    """
    same_size = [p for p, size in index.candidates(src.name) if size == src.size]
    if not same_size:
        return None
    src_hash = sha256_file(src.path, cancel, on_bytes)
    for path in same_size:
        if sha256_file(path, cancel) == src_hash:
            return path
    return None
