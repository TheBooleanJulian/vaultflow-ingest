"""Card-vs-vault verification (size check, optionally SHA-256), shared by the Verify
button and the automatic check at the end of a live ingest."""

from __future__ import annotations

import math
import queue
import random
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .exiftool import ExifTool
from .files import READ_AHEAD_MAX, ReadAhead, VaultIndex, sha256_file, sha256_uncached
from .report import Reporter
from .scan import SourceFile
from .util import CancelToken, ProgressTracker, fmt_bytes

OK = "OK"                # same size (and same SHA-256 if hashed)
OK_TAGGED = "OK-TAGGED"  # bytes differ only because metadata was written; image data identical
MISSING = "MISSING"
SIZE = "SIZE"
HASH = "HASH"
PROBLEMS = (MISSING, SIZE, HASH)


def verify_file(src: SourceFile, candidates: list[tuple[Path, int]], exif: ExifTool | None,
                do_hash: bool, cancel: CancelToken | None = None,
                tracker: ProgressTracker | None = None, known_md5: str = "") -> str:
    """Compare one card file with its vault candidates (same name anywhere in the vault)."""
    if not candidates:
        return MISSING

    on_bytes = tracker.add if tracker else None
    same_size = [p for p, size in candidates if size == src.size]
    if same_size:
        if not do_hash:
            return OK
        src_hash = sha256_file(src.path, cancel, on_bytes)
        for path in same_size:
            if sha256_file(path, cancel, on_bytes) == src_hash:
                return OK
        # Same length but different bytes: could still be a same-length metadata rewrite.
        if _image_data_matches(src, same_size, exif, known_md5):
            return OK_TAGGED
        return HASH

    if _image_data_matches(src, [p for p, _ in candidates], exif, known_md5):
        return OK_TAGGED
    return SIZE


MTIME_TOLERANCE_NS = 2_000_000_000  # copies keep the card file's mtime (exFAT rounds to 2s)


def likely_copies(src: SourceFile, paths: list[Path]) -> list[Path]:
    """Same-named vault files are common (cameras reuse counters: 850_5315.NEF exists in many
    shoots) and each one costs a slow ExifTool read. Copies keep the card file's modified time,
    so try those first; if none match (file touched since), fall back to all of them."""
    want = int(src.mtime * 1e9)
    near = []
    for p in paths:
        try:
            if abs(p.stat().st_mtime_ns - want) <= MTIME_TOLERANCE_NS:
                near.append(p)
        except OSError:
            pass
    return near or paths


def _image_data_matches(src: SourceFile, paths: list[Path], exif: ExifTool | None, known_md5: str = "") -> bool:
    if exif is None or not exif.available:
        return False
    want = known_md5 or exif.image_data_md5(src.path)
    return bool(want) and any(exif.image_data_md5(p) == want for p in likely_copies(src, paths))


@dataclass
class VerifyResult:
    total: int = 0
    hash_verified: int = 0
    size_ok: int = 0
    tagged: int = 0
    disk_checked: int = 0   # sample re-read with the OS cache bypassed, all matching
    names: dict[str, list[str]] = field(default_factory=lambda: {s: [] for s in PROBLEMS})

    @property
    def problems(self) -> int:
        return sum(len(v) for v in self.names.values())

    @property
    def ok(self) -> bool:
        return self.problems == 0


def verify_files(files: list[SourceFile], vault_root: Path | str, exif: ExifTool | None,
                 rep: Reporter, cancel: CancelToken, deep: bool,
                 index: VaultIndex | None = None,
                 known_md5: dict[Path, str] | None = None,
                 known_targets: dict[Path, Path] | None = None,
                 trusted: set[Path] | None = None) -> VerifyResult:
    """Phase 1: presence + size for every file. Phase 2 (deep): SHA-256 of both copies of
    every size match. `known_md5` maps card paths to image-data hashes already taken (saves
    re-reading the card); `known_targets` maps card paths to the exact vault file just written,
    so only that one is checked instead of every same-named file in the vault. Raises Cancelled
    if stopped. `trusted` holds card paths a WriteChecker already confirmed; they are counted
    without being read again."""
    result = VerifyResult(total=len(files))
    known_md5 = known_md5 or {}
    known_targets = known_targets or {}
    trusted = trusted or set()
    index = index or VaultIndex(vault_root, cancel)

    rep.log("[INFO]  Phase 1: presence and size...", "warn")
    to_hash: list[SourceFile] = []

    def candidates_for(f: SourceFile) -> list[tuple[Path, int]]:
        target = known_targets.get(f.path)
        if target is not None:
            try:
                return [(target, target.stat().st_size)]
            except OSError:
                pass  # vanished: fall through to the name search, which reports it properly
        return index.candidates(f.name)

    def tagged_copies(f: SourceFile) -> list[Path]:
        """Vault copies whose size differs from the card file: verified by image-data hash via
        ExifTool, which is slow on a cold USB HDD - so these get read ahead."""
        if exif is None or not exif.available or f.size > READ_AHEAD_MAX or f.path in trusted:
            return []
        return likely_copies(f, [p for p, size in candidates_for(f) if size != f.size])

    ahead = ReadAhead(cancel)
    try:
        for i, f in enumerate(files, 1):
            cancel.check()
            for path in tagged_copies(f):
                ahead.start(path)   # no-op if already started during the previous file
                ahead.wait(path)
            if i < len(files):
                for path in tagged_copies(files[i]):
                    ahead.start(path)
            rep.current(f"Checking: {f.name}")
            if f.path in trusted:
                status = OK_TAGGED
            else:
                status = verify_file(f, candidates_for(f), exif, do_hash=False, cancel=cancel,
                                     known_md5=known_md5.get(f.path, ""))
            if status == OK:
                to_hash.append(f)
                result.size_ok += 1
            elif status == OK_TAGGED:
                result.tagged += 1
            else:
                result.names[status].append(f.name)
            if i % 25 == 0 or i == len(files):
                rep.progress(i, len(files), int(i * 100 / max(len(files), 1)), 0.0, 0.0)
    finally:
        ahead.close()
    if not deep:
        return result

    position = [0]
    tracker = ProgressTracker(
        2 * sum(f.size for f in to_hash),
        lambda t: rep.progress(position[0], len(to_hash), t.percent, t.speed(), t.eta()))
    rep.log(f"\n[INFO]  Phase 2: SHA-256 of {len(to_hash)} size-matched files "
            f"({fmt_bytes(tracker.total)} to read, both copies)...", "warn")
    hashed = 0
    for i, f in enumerate(to_hash, 1):
        position[0] = i
        cancel.check()
        rep.current(f"Hashing: {f.name}")
        status = verify_file(f, candidates_for(f), exif, do_hash=True,
                             cancel=cancel, tracker=tracker, known_md5=known_md5.get(f.path, ""))
        if status == OK:
            hashed += 1
        elif status == OK_TAGGED:
            result.tagged += 1
            result.size_ok -= 1
        else:
            result.names[status].append(f.name)
            result.size_ok -= 1
        rep.progress(i, len(to_hash), tracker.percent, tracker.speed(), tracker.eta())
    result.hash_verified = hashed
    return result


class WriteChecker:
    """Checks each Copyright-tagged file right after it is written, on its own thread and
    ExifTool process, so the work hides behind the slow SD card instead of adding a long pass
    at the end. A file is `trusted` when its image data matches the card's. It also records the
    SHA-256 of every file as written, for `recheck_from_disk`.

    It reads from the OS cache (the file was just written), so it cannot see a bad write to the
    drive itself - the end-of-run disk re-read covers that. Any failure here just leaves the
    file untrusted, and the normal verify then checks it the slow way."""

    def __init__(self, exiftool_path: str, cancel: CancelToken) -> None:
        self._exif = ExifTool(exiftool_path, cancel)
        self._cancel = cancel
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self.trusted: set[Path] = set()                    # card paths confirmed intact
        self.written: dict[Path, tuple[Path, str]] = {}    # card path -> (vault file, SHA-256 as written)

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def submit(self, src: Path, target: Path, want_md5: str) -> None:
        """Queue a tagged copy for checking against the card file's image-data hash."""
        if not want_md5 or not self._exif.available:
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._work, name="write-checker", daemon=True)
            self._thread.start()
        self._queue.put((src, target, want_md5))

    def note_copy(self, src: Path, target: Path, sha: str) -> None:
        """Record a plain (untagged) copy: `sha` is the SHA-256 of the bytes read from the card."""
        self.written[src] = (target, sha)

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                src, target, want = item
                if self._cancel.cancelled:
                    continue
                if self._exif.image_data_md5(target) == want:
                    self.written[src] = (target, sha256_file(target, self._cancel))
                    self.trusted.add(src)
            except Exception:  # noqa: BLE001 - never kill the worker; the file just stays unchecked
                pass
            finally:
                self._queue.task_done()

    def drain(self) -> None:
        """Wait for every queued check to finish."""
        self._queue.join()

    def close(self) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=10)
        self._exif.close()


RECHECK_FRACTION = 0.10
RECHECK_MIN_FILES = 5
RECHECK_MAX_BYTES = 3 * 1024 ** 3   # keeps the re-read to about a minute on a USB HDD


def recheck_from_disk(written: dict[Path, tuple[Path, str]], result: VerifyResult, rep: Reporter,
                      cancel: CancelToken, rng: random.Random | None = None) -> None:
    """Re-read a random sample (~10%, capped) of the files just written, straight from the drive
    with the OS cache bypassed, and compare with the SHA-256 taken when each was written. This
    is what catches a write that never reached the disk. A mismatch is reported as a hash
    problem, so the card is not declared safe to format."""
    if not written:
        return
    items = list(written.items())
    (rng or random).shuffle(items)
    count = min(len(items), max(RECHECK_MIN_FILES, math.ceil(len(items) * RECHECK_FRACTION)))
    picked: list[tuple[Path, str]] = []
    total = 0
    for _, (target, sha) in items[:count]:
        try:
            size = target.stat().st_size
        except OSError:
            size = 0
        if picked and total + size > RECHECK_MAX_BYTES:
            break
        picked.append((target, sha))
        total += size

    rep.status("Re-reading sample from disk...")
    rep.log(f"\n[INFO]  Re-reading {len(picked)} of {len(items)} file(s) ({fmt_bytes(total)}) straight "
            f"from the vault drive, bypassing the OS cache...", "warn")
    bad = 0
    for target, sha in picked:
        cancel.check()
        rep.current(f"Re-reading from disk: {target.name}")
        try:
            ok = sha256_uncached(target, cancel) == sha
        except OSError:
            ok = False
        if ok:
            result.disk_checked += 1
        else:
            bad += 1
            result.names[HASH].append(target.name)
    rep.current("")
    if bad:
        rep.log(f"[ERROR] {bad} file(s) read back from the drive differ from what was written.", "error")
    else:
        rep.log(f"[OK]    {len(picked)} file(s) read back from the drive match what was written.", "ok")
