"""File operations: hashing, atomic copy, and the vault's name index for duplicate checks."""

from __future__ import annotations

import hashlib
import mmap
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Callable

from .scan import SourceFile
from .util import CancelToken

CHUNK = 4 * 1024 * 1024
PART_SUFFIX = ".part"
READ_AHEAD_MAX = 256 * 1024 * 1024  # bigger files (video) aren't pulled into the OS cache ahead of time

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


def sha256_uncached(path: Path, cancel: CancelToken | None = None) -> str:
    """SHA-256 of a file read straight from the disk, bypassing the OS file cache, so it proves
    what is physically stored rather than what is still sitting in RAM after a write.
    Windows only (FILE_FLAG_NO_BUFFERING); elsewhere this is an ordinary read."""
    if os.name != "nt":
        return sha256_file(path, cancel)
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.ReadFile.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    GENERIC_READ, SHARE_READ_WRITE, OPEN_EXISTING = 0x80000000, 3, 3
    NO_BUFFERING, SEQUENTIAL_SCAN = 0x20000000, 0x08000000
    handle = k32.CreateFileW(str(path), GENERIC_READ, SHARE_READ_WRITE, None, OPEN_EXISTING,
                             NO_BUFFERING | SEQUENTIAL_SCAN, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = mmap.mmap(-1, CHUNK)   # page-aligned, as unbuffered reads require
    view = (ctypes.c_char * CHUNK).from_buffer(buffer)
    h = hashlib.sha256()
    try:
        got = wintypes.DWORD(0)
        while True:
            if cancel:
                cancel.check()
            if not k32.ReadFile(handle, ctypes.addressof(view), CHUNK, ctypes.byref(got), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if got.value == 0:
                break
            h.update(buffer[:got.value])
    finally:
        k32.CloseHandle(handle)
        del view
        buffer.close()
    return h.hexdigest()


def warm_cache(path: Path, cancel: CancelToken | None = None) -> None:
    """Read a file front to back so the OS cache holds it. ExifTool reads in small scattered
    pieces and crawls on a cold USB card/HDD; after this it reads from RAM instead."""
    try:
        with open(path, "rb", buffering=0) as fh:
            while fh.read(CHUNK):
                if cancel:
                    cancel.check()
    except OSError:
        pass  # best effort - the real read will report any genuine problem


class ReadAhead:
    """Warms the cache for the NEXT file in a background thread while the current one is
    processed, so slow-device reads overlap with ExifTool work and writes."""

    def __init__(self, cancel: CancelToken | None = None) -> None:
        self._cancel = cancel
        self._pool = ThreadPoolExecutor(max_workers=1)  # one reader: parallel reads thrash a card/HDD
        self._pending: dict[Path, Future] = {}

    def start(self, path: Path) -> None:
        if path not in self._pending:
            self._pending[path] = self._pool.submit(warm_cache, path, self._cancel)

    def wait(self, path: Path) -> None:
        """Block until `path` is cached (no-op if it was never started). Re-raises Cancelled."""
        future = self._pending.pop(path, None)
        if future is not None:
            future.result()

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)


def copy_file_atomic(src: Path, dst: Path, cancel: CancelToken | None = None,
                     on_bytes: OnBytes | None = None) -> str:
    """Copy src to dst via dst.part, so a stopped or failed copy never leaves a truncated
    file under the real name (the PowerShell version could). Keeps the source mtime.
    Returns the SHA-256 of the bytes read, for a later check of what landed on disk."""
    part = dst.with_name(dst.name + PART_SUFFIX)
    digest = hashlib.sha256()
    try:
        with open(src, "rb") as fin, open(part, "wb") as fout:
            while True:
                if cancel:
                    cancel.check()
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                fout.write(chunk)
                if on_bytes:
                    on_bytes(len(chunk))
            fout.flush()
            os.fsync(fout.fileno())
        copy_times(src, part)
        os.replace(part, dst)
        return digest.hexdigest()
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
