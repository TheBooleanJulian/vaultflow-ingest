"""Finding card files, grouping them into shoots, and detecting SD cards."""

from __future__ import annotations

import os
import string
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .util import CancelToken, parse_ext_list

SHOOT_GAP_SECONDS = 30 * 60

# Order matters: an extension listed under two kinds is ingested once, as the first.
KINDS = ("raw", "video", "audio", "aux")
# Destination subfolder under <day>/RAW for each kind.
KIND_FOLDER = {"raw": "CAM_RAW", "jpg": "JPG", "video": "VIDEO", "audio": "AUDIO", "aux": "MISC"}
JPG_EXTS = ("jpg", "jpeg")


@dataclass(frozen=True)
class SourceFile:
    path: Path
    size: int
    mtime: float
    kind: str  # raw | video | audio | aux | jpg

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def ext(self) -> str:
        return self.path.suffix.lstrip(".").lower()


@dataclass
class CardScan:
    files: dict[str, list[SourceFile]] = field(default_factory=lambda: {k: [] for k in (*KINDS, "jpg")})
    other_count: int = 0   # files on the card that match no configured type
    other_bytes: int = 0

    def of(self, *kinds: str) -> list[SourceFile]:
        return [f for k in kinds for f in self.files[k]]

    def bytes_of(self, *kinds: str) -> int:
        return sum(f.size for f in self.of(*kinds))

    def orphan_jpgs(self) -> list[SourceFile]:
        """JPGs with no RAW of the same name - the only JPGs that get ingested."""
        raw_stems = {f.path.stem.lower() for f in self.files["raw"]}
        return [f for f in self.files["jpg"] if f.path.stem.lower() not in raw_stems]

    def paired_jpgs(self) -> list[SourceFile]:
        orphans = {f.path for f in self.orphan_jpgs()}
        return [f for f in self.files["jpg"] if f.path not in orphans]

    def ingest_files(self) -> list[SourceFile]:
        """Everything a live ingest copies (RAW, video, audio, aux, orphan JPGs)."""
        return self.of(*KINDS) + self.orphan_jpgs()


def scan_card(root: Path | str, cfg: Config, cancel: CancelToken | None = None) -> CardScan:
    """Walk the card once and bucket files by the configured extensions."""
    ext_kind: dict[str, str] = {}
    for kind, raw in (("raw", cfg.raw_ext), ("video", cfg.video_ext),
                      ("audio", cfg.audio_ext), ("aux", cfg.aux_ext)):
        for ext in parse_ext_list(raw):
            ext_kind.setdefault(ext, kind)
    for ext in JPG_EXTS:
        ext_kind.setdefault(ext, "jpg")

    scan = CardScan()
    stack = [str(root)]
    while stack:
        if cancel:
            cancel.check()
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name.lower())
        except OSError:
            continue
        subdirs = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.path)
                    continue
                st = entry.stat()
            except OSError:
                continue
            ext = os.path.splitext(entry.name)[1].lstrip(".").lower()
            kind = ext_kind.get(ext)
            if kind:
                scan.files[kind].append(SourceFile(Path(entry.path), st.st_size, st.st_mtime, kind))
            else:
                scan.other_count += 1
                scan.other_bytes += st.st_size
        stack.extend(reversed(subdirs))
    return scan


@dataclass
class Shoot:
    index: int
    files: list[SourceFile]

    @property
    def start(self) -> float:
        return self.files[0].mtime

    @property
    def end(self) -> float:
        return self.files[-1].mtime


def group_shoots(files: list[SourceFile], gap: float = SHOOT_GAP_SECONDS) -> list[Shoot]:
    """Split files (by modified time, which cameras set to capture time) at gaps > `gap` seconds."""
    shoots: list[Shoot] = []
    for f in sorted(files, key=lambda f: f.mtime):
        if shoots and f.mtime - shoots[-1].end <= gap:
            shoots[-1].files.append(f)
        else:
            shoots.append(Shoot(len(shoots), [f]))
    return shoots


def assign_shoots(shoots: list[Shoot], files: list[SourceFile]) -> dict[Path, int]:
    """Map every file to a shoot index: grouped files directly, others to the nearest shoot in time."""
    mapping: dict[Path, int] = {}
    for shoot in shoots:
        for f in shoot.files:
            mapping[f.path] = shoot.index
    if shoots:
        for f in files:
            if f.path not in mapping:
                nearest = min(shoots, key=lambda s: 0 if s.start <= f.mtime <= s.end
                              else min(abs(f.mtime - s.start), abs(f.mtime - s.end)))
                mapping[f.path] = nearest.index
    return mapping


# -- SD card detection (Windows) -------------------------------------------
# Run this off the UI thread: touching an empty card reader can block for ~15s.

def list_drive_letters() -> list[str]:
    """Drive roots like 'C:\\' that exist. Cheap - does not touch the media."""
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except (AttributeError, OSError):
        return []
    return [f"{letter}:\\" for i, letter in enumerate(string.ascii_uppercase) if mask & (1 << i)]


def detect_sd_cards() -> list[tuple[str, str]]:
    """[(root, volume label)] for removable drives holding a DCIM folder."""
    found: list[tuple[str, str]] = []
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetErrorMode(1)  # SEM_FAILCRITICALERRORS: no "insert disk" popups
    except (AttributeError, OSError):
        return found
    DRIVE_REMOVABLE = 2
    for root in list_drive_letters():
        if kernel32.GetDriveTypeW(root) != DRIVE_REMOVABLE:
            continue
        if not os.path.isdir(os.path.join(root, "DCIM")):
            continue
        label = ctypes.create_unicode_buffer(261)
        if not kernel32.GetVolumeInformationW(root, label, 261, None, None, None, None, 0):
            label.value = ""
        found.append((root, label.value or "(no label)"))
    return found
