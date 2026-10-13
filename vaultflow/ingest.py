"""The ingest and verify runs. No GUI code: everything user-facing goes through a
Reporter (progress/log) and Hooks (the few questions that need a person)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config
from .exiftool import ExifTool
from .files import (READ_AHEAD_MAX, VaultIndex, ReadAhead, copy_file_atomic, copy_times, find_duplicate,
                    unique_path, PART_SUFFIX)
from .report import Reporter, TeeReporter
from .scan import (KIND_FOLDER, CardScan, Shoot, SourceFile, assign_shoots, group_shoots,
                   scan_card)
from .util import (Cancelled, CancelToken, ProgressTracker, fmt_bytes, fmt_speed,
                   sanitize_description, NO_WINDOW)
from .verify import HASH, MISSING, SIZE, VerifyResult, verify_files

CLIENT_FOLDERS = ("RAW", "PX01", "LR01")  # per day folder; PX01/LR01 are empty export targets
RAW_TYPE_FOLDERS = ("CAM_RAW", "JPG", "VIDEO", "AUDIO", "MISC")
FREE_SPACE_BUFFER = 500 * 1024 * 1024
THUMB_CAP = 50
THUMB_WORKERS = 4
# Copyright is written into stills only. Video/audio/proxy files are copied byte-for-byte:
# ExifTool rewrites the whole file to tag it, and it cannot tag Insta360 clips at all.
TAGGED_KINDS = ("raw", "jpg")


class Hooks:
    """Questions that need a person. Defaults are non-interactive (used by tests/dry runs)."""

    def choose_shoot_folders(self, shoots: list[Shoot]) -> bool:
        """True = one folder per shoot, False = merge everything into one day folder."""
        return False

    def describe_jobs(self, count: int, multiple: bool, shoots: list[Shoot] | None = None) -> list[str] | None:
        """Job description(s) for the folder name(s); None = user cancelled.
        `shoots` is passed when naming several, so a UI can show what each one contains."""
        return [""] * (count if multiple else 1)

    def confirm_low_space(self, needed: int, available: int) -> bool:
        return True


@dataclass
class IngestResult:
    dry_run: bool = True
    cancelled: bool = False
    aborted: str = ""             # reason, if it stopped before doing anything
    files_total: int = 0
    bytes_total: int = 0
    copied: int = 0
    duplicates: int = 0
    failed: list[str] = field(default_factory=list)
    verify: VerifyResult | None = None
    elapsed: float = 0.0
    log_path: Path | None = None
    detected_copyright: str = ""
    ejected: bool = False

    @property
    def ok(self) -> bool:
        return not (self.cancelled or self.aborted or self.failed
                    or (self.verify is not None and not self.verify.ok))


def run_ingest(cfg: Config, drive_root: str, dry_run: bool, rep: Reporter, hooks: Hooks,
               cancel: CancelToken, eject: bool = False) -> IngestResult:
    result = IngestResult(dry_run=dry_run)
    started = time.monotonic()
    if not dry_run and cfg.log_dir.strip():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            result.log_path = Path(cfg.log_dir.strip()) / f"ingest_{stamp}.log"
            rep = TeeReporter(rep, result.log_path)
        except OSError:
            result.log_path = None
    exif = ExifTool(cfg.exiftool, cancel)
    ahead = ReadAhead(cancel)
    try:
        _ingest(cfg, drive_root, rep, hooks, cancel, eject, result, exif, ahead)
    except Cancelled:
        result.cancelled = True
        rep.log("[WARN]  Stopped by user.", "error")
    finally:
        ahead.close()
        exif.close()
    result.elapsed = time.monotonic() - started
    return result


def _abort(result: IngestResult, rep: Reporter, message: str) -> None:
    result.aborted = message
    rep.log(f"[ERROR] {message}", "error")


def _ingest(cfg: Config, drive_root: str, rep: Reporter, hooks: Hooks, cancel: CancelToken,
            eject: bool, result: IngestResult, exif: ExifTool, ahead: ReadAhead) -> None:
    dry_run = result.dry_run
    if not cfg.dest.strip():
        return _abort(result, rep, "Vault destination path is empty.")
    dest_root = Path(cfg.dest.strip())
    card_root = Path(drive_root) / "DCIM"
    if not card_root.is_dir():
        return _abort(result, rep, f"SD card not found at {card_root} - check the drive letter.")

    copyright_text = cfg.copyright.strip()
    meta_args = [f"-Copyright={copyright_text}"] if copyright_text else []
    if meta_args and not exif.available and not dry_run:
        return _abort(result, rep, f"Copyright is set but exiftool.exe was not found at: {cfg.exiftool}\n"
                                   "        Update the ExifTool path, or clear Copyright.")
    if not exif.available:
        rep.log("[WARN]  ExifTool not found - no previews or copyright tagging.", "warn")

    # -- scan -------------------------------------------------------------
    rep.status("Scanning files...")
    rep.log("[INFO]  Scanning source files...", "warn")
    scan = scan_card(card_root, cfg, cancel)
    steps = [("RAW files", scan.files["raw"], "CAM_RAW"),
             ("video files", scan.files["video"], "VIDEO"),
             ("audio files", scan.files["audio"], "AUDIO"),
             ("aux files", scan.files["aux"], "MISC"),
             ("JPGs" if cfg.copy_paired_jpgs else "orphan JPGs", scan.jpgs_to_ingest(cfg.copy_paired_jpgs), "JPG")]
    for label, files, _ in steps:
        rep.log(f"[INFO]  Found {len(files)} {label}   ({fmt_bytes(sum(f.size for f in files))})")
    to_ingest = scan.ingest_files(cfg.copy_paired_jpgs)
    result.files_total = len(to_ingest)
    result.bytes_total = sum(f.size for f in to_ingest)
    rep.log(f"[INFO]  Total: {fmt_bytes(result.bytes_total)}")
    _log_not_ingested(scan, rep, cfg.copy_paired_jpgs)
    if not to_ingest:
        rep.log("[WARN]  Nothing to ingest - no files match the configured file types.", "warn")
        return

    _preview(scan, exif, rep, cancel, cfg.copy_paired_jpgs)

    # -- shoots & naming ------------------------------------------------------
    rep.status("Detecting multiple shoots...")
    rep.log("\n[INFO]  Checking for multiple shoots...", "warn")
    shoots = group_shoots(scan.of("raw", "video") + scan.jpgs_to_ingest(cfg.copy_paired_jpgs))
    multiple = False
    mapping: dict[Path, int] = {}
    if len(shoots) > 1:
        rep.log(f"[INFO]  Detected {len(shoots)} separate shoots (gaps over 30 min).", "accent")
        multiple = hooks.choose_shoot_folders(shoots)
        cancel.check()
        if multiple:
            mapping = assign_shoots(shoots, to_ingest)
            rep.log("[INFO]  Multiple shoot folders will be created.", "accent")
        else:
            rep.log("[INFO]  All files will be merged into single folder.", "dim")
    else:
        rep.log("[INFO]  Single shoot detected.", "dim")

    if dry_run:
        descriptions = [""] * (len(shoots) if multiple else 1)  # names only matter for real folders
    else:
        descriptions = hooks.describe_jobs(len(shoots) if multiple else 1, multiple,
                                            shoots if multiple else None)
        if descriptions is None:
            rep.log("[INFO]  Ingest cancelled.", "dim")
            result.cancelled = True
            return
    descriptions = [sanitize_description(d) for d in descriptions]

    def suffix_for(f: SourceFile) -> str:
        desc = descriptions[mapping.get(f.path, 0)] if multiple else descriptions[0]
        return f" {desc}" if desc else ""

    # -- vault index & storage ---------------------------------------------
    rep.status("Indexing vault...")
    rep.log("[INFO]  Indexing vault for duplicates...", "warn")
    index = VaultIndex(dest_root, cancel)
    rep.log(f"[INFO]  {len(index)} distinct file names already in vault.", "dim")

    if not dry_run:
        anchor = dest_root
        while not anchor.exists() and anchor != anchor.parent:
            anchor = anchor.parent
        free = shutil.disk_usage(anchor).free
        needed = result.bytes_total + FREE_SPACE_BUFFER
        rep.log(f"[INFO]  Target drive free: {fmt_bytes(free)}")
        if needed > free:
            rep.log(f"[WARN]  Insufficient storage! Need {fmt_bytes(needed)}, have {fmt_bytes(free)}.", "error")
            if not hooks.confirm_low_space(needed, free):
                rep.log("[INFO]  Cancelled (storage check).", "error")
                result.cancelled = True
                return
            rep.log("[WARN]  Continuing despite storage warning.", "warn")
        else:
            rep.log("[OK]    Sufficient storage available.", "ok")
        dest_root.mkdir(parents=True, exist_ok=True)

    mode = "DRY RUN" if dry_run else "LIVE"
    rep.log("\n============================================", "accent")
    rep.log(f" VAULTFLOW  [{mode}]  -  {datetime.now():%Y-%m-%d %H:%M}", "accent")
    rep.log(f" Source : {card_root}")
    rep.log(f" Dest   : {dest_root}\\{{YYYY_MM}}\\{{YYYY_MM_DD}}\\RAW\\{{CAM_RAW|JPG|VIDEO|AUDIO|MISC}}")
    rep.log("============================================", "accent")

    # -- copy ----------------------------------------------------------------
    files_done = [0]
    tracker = ProgressTracker(
        result.bytes_total,
        lambda t: rep.progress(files_done[0], result.files_total, t.percent, t.speed(), t.eta()))
    reserved: set[Path] = set()
    written: dict[Path, Path] = {}  # card file -> the vault file it became, for the verify
    src_md5: dict[Path, str] = {}  # image-data hash of each tagged card file, taken during the copy
    touched_days: set[Path] = set()

    for label, files, folder in steps:
        if not files:
            rep.log(f"\n[INFO]  No {label} found - skipping.", "dim")
            continue
        rep.status(f"{'Simulating' if dry_run else 'Copying'} {label}...")
        rep.log(f"\n[INFO]  {'Simulating' if dry_run else 'Copying'} {len(files)} {label} "
                f"({fmt_bytes(sum(f.size for f in files))})...", "warn")
        for i, f in enumerate(files):
            cancel.check()
            if not dry_run:
                # Card reads are the slow part: pull the next file in while this one is tagged/written.
                ahead.wait(f.path)
                if i + 1 < len(files) and files[i + 1].size <= READ_AHEAD_MAX:
                    ahead.start(files[i + 1].path)
            rep.highlight(f.name)
            rep.current(f"{'Simulating' if dry_run else 'Copying'}: {f.name}  ({fmt_bytes(f.size)})")
            dt = datetime.fromtimestamp(f.mtime)
            day = dest_root / f"{dt:%Y_%m}" / f"{dt:%Y_%m_%d}{suffix_for(f)}"
            target_dir = day / "RAW" / folder

            dup = find_duplicate(index, f, cancel)
            if dup is None:
                target = target_dir / f.name
                if target in reserved or target.exists():
                    # Same-name file is already at this exact spot: if it's our own earlier,
                    # tagged copy of this file it's a duplicate, otherwise keep both.
                    if (meta_args and f.kind in TAGGED_KINDS and target not in reserved
                            and exif.available
                            and (want := exif.image_data_md5(f.path))
                            and exif.image_data_md5(target) == want):
                        dup = target
                    else:
                        target = unique_path(target)
                        while target in reserved:
                            target = unique_path(target)
            if dup is not None:
                result.duplicates += 1
                rep.log(f"  [{'SKIP - duplicate' if dry_run else 'DUPLICATE - already in vault'}]  {f.name}", "warn")
                tracker.add(f.size)
            elif dry_run:
                reserved.add(target)
                rep.log(f"  [WOULD COPY >> {folder}]  {f.name}  ({fmt_bytes(f.size)})  ->  {day.name}", "dim")
                tracker.add(f.size)
            else:
                touched_days.add(day)
                ok = _copy_one(f, target, meta_args, exif, cancel, tracker, rep, src_md5)
                if ok:
                    reserved.add(target)
                    written[f.path] = target
                    index.add(target, target.stat().st_size)
                    result.copied += 1
                else:
                    result.failed.append(f.name)
            files_done[0] += 1
            rep.progress(files_done[0], result.files_total, tracker.percent, tracker.speed(), tracker.eta())
            rep.log(f"  [{fmt_bytes(tracker.done)} / {fmt_bytes(tracker.total)}]  {f.name}", "dim")
        rep.log(f"[OK]    {label} step complete.", "ok")

    # -- scaffold, verify, summary ---------------------------------------------
    if dry_run:
        rep.log(f"\n[INFO]  Would create client folders ({', '.join(CLIENT_FOLDERS)}) in touched day "
                f"folder(s), with RAW split into {', '.join(RAW_TYPE_FOLDERS)}.", "dim")
    else:
        for day in sorted(touched_days):
            for sub in CLIENT_FOLDERS:
                (day / sub).mkdir(parents=True, exist_ok=True)
            for sub in RAW_TYPE_FOLDERS:
                (day / "RAW" / sub).mkdir(parents=True, exist_ok=True)
        if touched_days:
            rep.log(f"\n[OK]    Client folder structure ensured in {len(touched_days)} day folder(s).", "ok")

    rep.progress(files_done[0], result.files_total, 100, 0.0, 0.0)
    rep.current("")
    if dry_run:
        rep.log("[INFO]  Skipping verification (dry run).", "dim")
        rep.status("Detecting copyright...")
        result.detected_copyright = exif.detect_copyright(
            [f.path for f in scan.of("raw", "jpg", "video")])
    else:
        rep.status("Verifying...")
        rep.log("\n[INFO]  Verifying ingest (presence + size)...", "warn")
        result.verify = verify_files(to_ingest, dest_root, exif, rep, cancel, deep=False,
                                     index=index, known_md5=src_md5, known_targets=written)
        _log_verify_verdict(result.verify, scan, rep, deep=False, copy_paired=cfg.copy_paired_jpgs)

    _log_summary(result, scan, rep, mode, tracker, cfg.copy_paired_jpgs)

    if eject and not dry_run:
        if result.ok:
            result.ejected = _eject(drive_root, rep)
        else:
            rep.log("[WARN]  Not ejecting - the ingest had problems. Check the log above.", "warn")


def _copy_one(f: SourceFile, target: Path, meta_args: list[str], exif: ExifTool,
              cancel: CancelToken, tracker: ProgressTracker, rep: Reporter,
              src_md5: dict[Path, str]) -> bool:
    """Copy one file into the vault. Never leaves a partial file under the real name."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if meta_args and f.kind in TAGGED_KINDS:
            part = target.with_name(target.name + PART_SUFFIX)
            # Hash the card file first: it's then in the OS cache for the tagging read below,
            # and the end-of-ingest verify needn't read the card a second time.
            if want := exif.image_data_md5(f.path):
                src_md5[f.path] = want
            ok, lines = exif.write_copy(f.path, part, meta_args)
            for line in lines:
                rep.log(f"  {line}", "warn")
            cancel.check()
            if ok:
                copy_times(f.path, part)
                os.replace(part, target)
                tracker.add(f.size)
                return True
            try:
                part.unlink()
            except OSError:
                pass
            rep.log(f"  [WARN]  Could not write metadata to {f.name} - copying without tags.", "warn")
        copy_file_atomic(f.path, target, cancel, tracker.add)
        if target.stat().st_size != f.size:
            target.unlink()
            raise OSError(f"copied size differs from source ({f.size} bytes)")
        return True
    except Cancelled:
        raise
    except OSError as exc:
        rep.log(f"  [ERROR] {f.name}: {exc}", "error")
        return False


def _log_not_ingested(scan: CardScan, rep: Reporter, copy_paired: bool) -> None:
    paired = scan.paired_jpgs()
    if paired and not copy_paired:
        rep.log(f"[INFO]  {len(paired)} JPG file(s) have a matching RAW and are NOT copied "
                f"(tick \"Also copy JPGs that have a matching RAW\" to include them).", "dim")
    if scan.other_count:
        rep.log(f"[INFO]  {scan.other_count} other file(s) ({fmt_bytes(scan.other_bytes)}) match no "
                f"configured type and are NOT copied.", "dim")


def _log_verify_verdict(v: VerifyResult, scan: CardScan, rep: Reporter, deep: bool,
                        copy_paired: bool) -> None:
    what = "SHA-256 verified" if deep else "present with matching size"
    rep.log(f"  Card files : {v.total}", "info")
    rep.log(f"  Intact     : {v.hash_verified if deep else v.size_ok}  ({what})", "info")
    if v.tagged:
        rep.log(f"  Tagged     : {v.tagged}  (bytes differ only by the Copyright tag; image data identical)", "info")
    for status, label in ((MISSING, "Missing"), (SIZE, "Size mismatch"), (HASH, "Hash mismatch")):
        names = v.names[status]
        if names:
            rep.log(f"  {label:<11}: {len(names)}", "error")
            for n in names:
                rep.log(f"    {status}: {n}", "error")
    if v.ok:
        rep.log(f"[OK]    All {v.total} files verified.", "ok")
        extras = (0 if copy_paired else len(scan.paired_jpgs())) + scan.other_count
        if extras:
            rep.log(f"[OK]    Safe to format SD card - except the {extras} file(s) noted above "
                    f"that VaultFlow does not copy.", "ok")
        else:
            rep.log("[OK]    Safe to format SD card.", "ok")
    else:
        rep.log(f"[WARN]  {v.problems} file(s) missing or incomplete. Do NOT format the SD card - "
                f"re-run Live Ingest (it skips intact duplicates).", "error")


def _log_summary(result: IngestResult, scan: CardScan, rep: Reporter, mode: str,
                 tracker: ProgressTracker, copy_paired: bool) -> None:
    rep.log("\n-- SUMMARY ---------------------", "accent")
    rep.log(f"  Mode        : {mode}", "warn" if result.dry_run else "ok")
    for label, kind in (("RAW files", "raw"), ("Video files", "video"),
                        ("Audio files", "audio"), ("Aux files", "aux")):
        rep.log(f"  {label:<12}: {len(scan.files[kind])}  ({fmt_bytes(scan.bytes_of(kind))})")
    rep.log(f"  Duplicates  : {result.duplicates} skipped", "warn" if result.duplicates else "info")
    rep.log(f"  JPGs        : {len(scan.jpgs_to_ingest(copy_paired))}"
            f"{'' if copy_paired else '  (orphans only)'}")
    if result.failed:
        rep.log(f"  FAILED      : {len(result.failed)}", "error")
        for name in result.failed:
            rep.log(f"    {name}", "error")
    rep.log(f"  Total size  : {fmt_bytes(result.bytes_total)}")
    if not result.dry_run:
        rep.log(f"  Avg speed   : {fmt_speed(tracker.done / max(tracker.elapsed, 1e-6))}")
    if result.log_path:
        rep.log(f"  Log         : {result.log_path}", "dim")
    rep.log("---------------------------------", "accent")


def _preview(scan: CardScan, exif: ExifTool, rep: Reporter, cancel: CancelToken,
             copy_paired: bool) -> None:
    candidates = sorted(scan.of("raw", "video", "aux") + scan.jpgs_to_ingest(copy_paired),
                        key=lambda f: f.mtime)
    sample = candidates[:THUMB_CAP]
    rep.thumbs_reset(max(0, len(candidates) - len(sample)) + len(scan.files["audio"]))
    rep.status("Generating previews...")
    # exiftool.exe takes ~0.8s to start per file, so pull several thumbnails at once;
    # results are still reported in order.
    with ThreadPoolExecutor(max_workers=THUMB_WORKERS) as pool:
        jobs = [(f, pool.submit(exif.thumbnail, f.path) if f.kind in ("raw", "jpg") else None)
                for f in sample]
        for f, future in jobs:
            cancel.check()
            rep.thumb(f.name, f.ext, future.result() if future else None)


def run_verify(cfg: Config, drive_root: str, rep: Reporter, cancel: CancelToken) -> VerifyResult | None:
    """The Verify button: SHA-256 comparison of every ingestible card file against the vault."""
    log_path = None
    exif = ExifTool(cfg.exiftool, cancel)
    if cfg.log_dir.strip():
        try:
            log_path = Path(cfg.log_dir.strip()) / f"verify_{datetime.now():%Y%m%d_%H%M%S}.log"
            rep = TeeReporter(rep, log_path)
        except OSError:
            log_path = None
    try:
        card_root = Path(drive_root) / "DCIM"
        dest_root = Path(cfg.dest.strip())
        if not card_root.is_dir():
            rep.log(f"[ERROR] SD card not found at {card_root} - verification needs the card "
                    f"inserted (not ejected).", "error")
            return None
        if not cfg.dest.strip() or not dest_root.is_dir():
            rep.log(f"[ERROR] Vault destination not found: {cfg.dest}", "error")
            return None
        rep.status("Scanning...")
        rep.log("[INFO]  Scanning card and vault...", "warn")
        scan = scan_card(card_root, cfg, cancel)
        files = scan.ingest_files(cfg.copy_paired_jpgs)
        if not files:
            rep.log("[WARN]  No files matching the configured file types found on the card.", "warn")
            return None
        rep.log(f"[INFO]  Card: {len(files)} files ({fmt_bytes(sum(f.size for f in files))})")
        _log_not_ingested(scan, rep, cfg.copy_paired_jpgs)
        rep.status("Verifying...")
        v = verify_files(files, dest_root, exif, rep, cancel, deep=True)
        rep.log("\n-- VERIFY SUMMARY --------------", "accent")
        _log_verify_verdict(v, scan, rep, deep=True, copy_paired=cfg.copy_paired_jpgs)
        rep.log("--------------------------------", "accent")
        if log_path:
            rep.log(f"  Log: {log_path}", "dim")
        return v
    except Cancelled:
        rep.log("[WARN]  Verification stopped.", "error")
        return None
    finally:
        exif.close()


def eject_drive(drive_root: str) -> bool:
    """Ask Windows to eject the drive (same mechanism as the PowerShell version)."""
    letter = drive_root.rstrip("\\/")
    script = ("(New-Object -ComObject Shell.Application).Namespace(17)"
              f".ParseName('{letter}').InvokeVerb('Eject')")
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                       creationflags=NO_WINDOW, timeout=30, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _eject(drive_root: str, rep: Reporter) -> bool:
    rep.log(f"\n[INFO]  Ejecting {drive_root}...", "warn")
    if eject_drive(drive_root):
        rep.log("[OK]    SD card ejected.", "ok")
        return True
    rep.log("[WARN]  Eject error - remove the card manually.", "warn")
    return False
