"""Card-vs-vault verification (size check, optionally SHA-256), shared by the Verify
button and the automatic check at the end of a live ingest."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .exiftool import ExifTool
from .files import READ_AHEAD_MAX, ReadAhead, VaultIndex, sha256_file
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
        if _image_data_matches(src.path, same_size, exif, known_md5):
            return OK_TAGGED
        return HASH

    if _image_data_matches(src.path, [p for p, _ in candidates], exif, known_md5):
        return OK_TAGGED
    return SIZE


def _image_data_matches(src: Path, paths: list[Path], exif: ExifTool | None, known_md5: str = "") -> bool:
    if exif is None or not exif.available:
        return False
    want = known_md5 or exif.image_data_md5(src)
    return bool(want) and any(exif.image_data_md5(p) == want for p in paths)


@dataclass
class VerifyResult:
    total: int = 0
    hash_verified: int = 0
    size_ok: int = 0
    tagged: int = 0
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
                 known_md5: dict[Path, str] | None = None) -> VerifyResult:
    """Phase 1: presence + size for every file. Phase 2 (deep): SHA-256 of both copies of
    every size match. `known_md5` maps card paths to image-data hashes already taken (saves
    re-reading the card). Raises Cancelled if stopped."""
    result = VerifyResult(total=len(files))
    known_md5 = known_md5 or {}
    index = index or VaultIndex(vault_root, cancel)

    rep.log("[INFO]  Phase 1: presence and size...", "warn")
    to_hash: list[SourceFile] = []

    def tagged_copies(f: SourceFile) -> list[Path]:
        """Vault copies whose size differs from the card file: verified by image-data hash via
        ExifTool, which is slow on a cold USB HDD - so these get read ahead."""
        if exif is None or not exif.available or f.size > READ_AHEAD_MAX:
            return []
        return [p for p, size in index.candidates(f.name) if size != f.size]

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
            status = verify_file(f, index.candidates(f.name), exif, do_hash=False, cancel=cancel,
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
        status = verify_file(f, index.candidates(f.name), exif, do_hash=True,
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
