"""Engine tests (stdlib unittest): python -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vaultflow.config import Config, load_config, save_config
from vaultflow.exiftool import ExifTool
from vaultflow.files import (ReadAhead, VaultIndex, copy_file_atomic, find_duplicate, sha256_file,
                             sha256_uncached)
from vaultflow.ingest import Hooks, run_ingest, run_verify
from vaultflow.report import ListReporter
from vaultflow.scan import assign_shoots, group_shoots, scan_card
from vaultflow.util import (Cancelled, CancelToken, fmt_bytes, fmt_eta, parse_ext_list,
                            sanitize_description)
from vaultflow.verify import (HASH, MISSING, OK, OK_TAGGED, SIZE, VerifyResult, WriteChecker,
                              recheck_from_disk, verify_files)

REAL_EXIFTOOL = r"C:\exiftool\exiftool.exe"


def stamp(year=2026, month=10, day=7, hour=10, minute=0) -> float:
    return datetime(year, month, day, hour, minute).timestamp()


class Sandbox(unittest.TestCase):
    """A fake card (<tmp>/card/DCIM) and an empty vault (<tmp>/vault)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vaultflow_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.card = self.tmp / "card"
        self.dcim = self.card / "DCIM" / "100CAM"
        self.dcim.mkdir(parents=True)
        self.vault = self.tmp / "vault"
        self.cfg = Config(dest=str(self.vault), log_dir=str(self.tmp / "logs"),
                          exiftool=str(self.tmp / "no_exiftool.exe"),
                          raw_ext="nef", video_ext="mp4", audio_ext="wav", aux_ext="lrv")
        self.cancel = CancelToken()
        self.rep = ListReporter()

    def card_file(self, name, data=b"x" * 1000, when=None, folder=None) -> Path:
        d = folder or self.dcim
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_bytes(data)
        t = when if when is not None else stamp()
        os.utime(p, (t, t))
        return p

    def ingest(self, dry_run=False, hooks=None, cfg=None, eject=False):
        return run_ingest(cfg or self.cfg, str(self.card), dry_run, self.rep, hooks or Hooks(),
                          self.cancel, eject)

    def vault_files(self):
        return sorted(p.relative_to(self.vault).as_posix() for p in self.vault.rglob("*") if p.is_file())


class TestHelpers(unittest.TestCase):
    def test_parse_ext_list(self):
        self.assertEqual(parse_ext_list("nef, CR2 , .arw,,nef"), ["nef", "cr2", "arw"])
        self.assertEqual(parse_ext_list(""), [])

    def test_sanitize_description(self):
        self.assertEqual(sanitize_description('  XDC: Raffles/Place? '), "XDC- Raffles-Place-")
        self.assertEqual(sanitize_description("trailing dot. "), "trailing dot")

    def test_formatters(self):
        self.assertEqual(fmt_bytes(500), "500 B")
        self.assertEqual(fmt_bytes(1 << 30), "1.00 GB")
        self.assertEqual(fmt_eta(0), "--:--")
        self.assertEqual(fmt_eta(75), "1m 15s")
        self.assertEqual(fmt_eta(3700), "1h 01m")


class TestConfig(unittest.TestCase):
    def test_reads_powershell_json_with_bom_and_ignores_removed_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_bytes(b"\xef\xbb\xbf" + json.dumps({
                "Dest": r"J:\vault", "LogDir": r"J:\vault\_logs", "Exiftool": r"C:\e.exe",
                "RawExt": "nef", "VideoExt": "mp4", "AudioExt": "wav", "AuxExt": "lrv, insv",
                "Copyright": "", "ContactEmail": "a@b.c", "Website": "x", "TelegramToken": "t"}).encode())
            cfg = load_config(p)
            self.assertEqual(cfg.dest, r"J:\vault")
            self.assertEqual(cfg.aux_ext, "lrv, insv")
            self.assertIn("cr3", cfg.raw_ext)  # PowerShell's bare "nef" default is not a customisation

    def test_copy_paired_jpgs_defaults_off_and_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            cfg = load_config(p)
            self.assertFalse(cfg.copy_paired_jpgs)
            cfg.copy_paired_jpgs = True
            save_config(p, cfg)
            self.assertTrue(load_config(p).copy_paired_jpgs)
            self.assertIs(json.loads(p.read_text("utf-8"))["CopyPairedJpgs"], True)
            p.write_text(json.dumps({"Dest": "x", "CopyPairedJpgs": "yes"}), "utf-8")
            self.assertFalse(load_config(p).copy_paired_jpgs)        # not a real boolean -> default

    def test_round_trip_and_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            self.assertTrue(load_config(p).dest)  # defaults when absent
            cfg = load_config(p)
            cfg.copyright = "(c) 2026 Test"
            save_config(p, cfg)
            self.assertEqual(load_config(p).copyright, "(c) 2026 Test")
            self.assertEqual(json.loads(p.read_text("utf-8"))["Copyright"], "(c) 2026 Test")


class TestScan(Sandbox):
    def test_kinds_orphans_and_other(self):
        self.card_file("A.NEF"); self.card_file("A.JPG"); self.card_file("B.JPG")
        self.card_file("C.MP4"); self.card_file("C.LRV"); self.card_file("T.WAV")
        self.card_file("note.xml")
        scan = scan_card(self.card / "DCIM", self.cfg)
        self.assertEqual([f.name for f in scan.files["raw"]], ["A.NEF"])
        self.assertEqual([f.name for f in scan.orphan_jpgs()], ["B.JPG"])
        self.assertEqual([f.name for f in scan.paired_jpgs()], ["A.JPG"])
        self.assertEqual(len(scan.files["video"]), 1)
        self.assertEqual(len(scan.files["aux"]), 1)
        self.assertEqual(scan.other_count, 1)
        self.assertEqual(len(scan.ingest_files()), 5)  # nef, mp4, wav, lrv, orphan B.JPG

    def test_paired_jpgs_follow_the_toggle(self):
        self.card_file("A.NEF"); self.card_file("A.JPG"); self.card_file("B.JPG")
        scan = scan_card(self.card / "DCIM", self.cfg)
        self.assertEqual([f.name for f in scan.jpgs_to_ingest(False)], ["B.JPG"])
        self.assertEqual(sorted(f.name for f in scan.jpgs_to_ingest(True)), ["A.JPG", "B.JPG"])
        self.assertEqual(len(scan.ingest_files()), 2)                 # NEF + orphan B.JPG
        self.assertEqual(len(scan.ingest_files(copy_paired=True)), 3)

    def test_extension_listed_twice_is_ingested_once(self):
        self.card_file("C.MP4")
        self.cfg.aux_ext = "mp4"
        scan = scan_card(self.card / "DCIM", self.cfg)
        self.assertEqual(len(scan.files["video"]), 1)
        self.assertEqual(len(scan.files["aux"]), 0)


class TestShoots(Sandbox):
    def test_gap_grouping_and_nearest_assignment(self):
        a1 = self.card_file("1.NEF", when=stamp(hour=9, minute=0))
        a2 = self.card_file("2.NEF", when=stamp(hour=9, minute=20))
        b1 = self.card_file("3.NEF", when=stamp(hour=14, minute=0))
        audio = self.card_file("t.wav", when=stamp(hour=13, minute=50))
        scan = scan_card(self.card / "DCIM", self.cfg)
        shoots = group_shoots(scan.of("raw"))
        self.assertEqual([len(s.files) for s in shoots], [2, 1])
        mapping = assign_shoots(shoots, scan.ingest_files())
        self.assertEqual(mapping[a1], 0)
        self.assertEqual(mapping[a2], 0)
        self.assertEqual(mapping[b1], 1)
        self.assertEqual(mapping[audio], 1)  # nearest shoot in time


class TestFiles(Sandbox):
    def test_atomic_copy_keeps_content_and_mtime_and_no_part_file(self):
        src = self.card_file("A.mp4", data=os.urandom(9_000_000), when=stamp(hour=3))
        dst = self.tmp / "out.mp4"
        copy_file_atomic(src, dst)
        self.assertEqual(sha256_file(src), sha256_file(dst))
        self.assertAlmostEqual(dst.stat().st_mtime, src.stat().st_mtime, delta=1)
        self.assertEqual([p.name for p in self.tmp.glob("*.part")], [])

    def test_uncached_hash_matches_normal_hash_for_awkward_sizes(self):
        for size in (0, 1, 4095, 5000, (4 << 20) + 17):
            p = self.tmp / f"f{size}.bin"
            p.write_bytes(os.urandom(size))
            self.assertEqual(sha256_uncached(p), sha256_file(p), size)

    def test_copy_file_atomic_returns_sha256_of_what_it_read(self):
        src = self.card_file("A.mp4", data=os.urandom(70000))
        self.assertEqual(copy_file_atomic(src, self.tmp / "out.mp4"), sha256_file(src))

    def test_disk_recheck_passes_intact_files_and_flags_a_corrupted_one(self):
        good = self.tmp / "good.bin"; good.write_bytes(os.urandom(9000))
        bad = self.tmp / "bad.bin"; bad.write_bytes(os.urandom(9000))
        checker = WriteChecker(str(self.tmp / "no_exiftool.exe"), self.cancel)
        self.addCleanup(checker.close)
        checker.note_copy(self.tmp / "g", good, sha256_file(good))
        checker.note_copy(self.tmp / "b", bad, sha256_file(bad))
        result = VerifyResult()
        recheck_from_disk(checker.written, result, self.rep, self.cancel)
        self.assertEqual((result.disk_checked, result.problems), (2, 0))
        bad.write_bytes(b"x" + bad.read_bytes()[1:])                 # silent corruption after the write
        result = VerifyResult()
        recheck_from_disk(checker.written, result, self.rep, self.cancel)
        self.assertEqual((result.disk_checked, result.names[HASH]), (1, ["bad.bin"]))

    def test_read_ahead_is_harmless_and_propagates_cancel(self):
        a = self.card_file("A.NEF", data=os.urandom(5000))
        ahead = ReadAhead(self.cancel)
        ahead.start(a)
        ahead.wait(a)                                   # reads the file, returns
        ahead.wait(a)                                   # never started / already waited: no-op
        ahead.start(self.tmp / "missing.NEF")           # unreadable files are ignored, not raised
        ahead.wait(self.tmp / "missing.NEF")
        self.cancel.cancel()
        ahead.start(a)
        with self.assertRaises(Cancelled):
            ahead.wait(a)
        ahead.close()

    def test_cancelled_copy_leaves_no_file_at_all(self):
        src = self.card_file("A.mp4", data=os.urandom(9_000_000))
        dst = self.tmp / "out.mp4"
        with self.assertRaises(Cancelled):
            copy_file_atomic(src, dst, self.cancel, lambda n: self.cancel.cancel())
        self.assertFalse(dst.exists())
        self.assertEqual(list(self.tmp.glob("out*")), [])

    def test_duplicate_needs_same_name_size_and_content(self):
        src = self.card_file("A.NEF", data=b"a" * 500)
        vault = self.tmp / "v"
        (vault / "x").mkdir(parents=True)
        (vault / "x" / "A.NEF").write_bytes(b"b" * 500)           # same name+size, different bytes
        scan = scan_card(self.card / "DCIM", self.cfg)
        f = scan.files["raw"][0]
        self.assertIsNone(find_duplicate(VaultIndex(vault), f))
        (vault / "y").mkdir()
        (vault / "y" / "a.nef").write_bytes(b"a" * 500)           # identical (name case-insensitive)
        self.assertEqual(find_duplicate(VaultIndex(vault), f), vault / "y" / "a.nef")


class TestIngest(Sandbox):
    def test_dry_run_writes_nothing(self):
        self.card_file("A.NEF")
        res = self.ingest(dry_run=True)
        self.assertTrue(res.ok)
        self.assertFalse(self.vault.exists())
        self.assertFalse((self.tmp / "logs").exists())
        self.assertIn("WOULD COPY >> CAM_RAW", self.rep.text())

    def test_live_layout_scaffold_verify_and_mtime(self):
        when = stamp(hour=23, minute=50)
        self.card_file("A.NEF", when=when); self.card_file("C.MP4", when=when)
        self.card_file("C.LRV", when=when); self.card_file("T.WAV", when=when)
        self.card_file("A.JPG", when=when)          # paired with A.NEF -> not copied
        self.card_file("B.JPG", when=when)          # orphan -> copied
        res = self.ingest()
        self.assertTrue(res.ok, self.rep.text())
        self.assertEqual(res.copied, 5)
        day = "2026_10/2026_10_07"
        self.assertEqual(self.vault_files(), sorted(
            [f"{day}/RAW/CAM_RAW/A.NEF", f"{day}/RAW/VIDEO/C.MP4", f"{day}/RAW/MISC/C.LRV",
             f"{day}/RAW/AUDIO/T.WAV", f"{day}/RAW/JPG/B.JPG"]))
        for sub in ("PX01", "LR01", "RAW/CAM_RAW", "RAW/JPG", "RAW/VIDEO", "RAW/AUDIO", "RAW/MISC"):
            self.assertTrue((self.vault / "2026_10" / "2026_10_07" / sub).is_dir(), sub)
        self.assertAlmostEqual((self.vault / day / "RAW/VIDEO/C.MP4").stat().st_mtime, when, delta=1)
        self.assertIsNotNone(res.log_path)
        self.assertTrue(res.log_path.is_file())
        self.assertIn("1 JPG file(s) have a matching RAW", self.rep.text())

    def test_paired_jpgs_are_skipped_by_default_and_copied_when_toggled_on(self):
        self.card_file("A.NEF", data=b"raw" * 100); self.card_file("A.JPG", data=b"jpg" * 100)
        res = self.ingest()
        self.assertEqual(res.copied, 1)
        self.assertFalse(list(self.vault.rglob("A.JPG")))
        self.assertIn("NOT copied", self.rep.text())
        self.assertIn("(orphans only)", self.rep.text())

        self.cfg.copy_paired_jpgs = True
        self.rep = ListReporter()
        res = self.ingest()
        self.assertTrue(res.ok, self.rep.text())
        self.assertEqual((res.copied, res.duplicates), (1, 1))        # the JPG is new, the NEF a duplicate
        self.assertEqual(len(list(self.vault.rglob("RAW/JPG/A.JPG"))), 1)
        self.assertEqual(len(list(self.vault.rglob("RAW/CAM_RAW/A.NEF"))), 1)
        self.assertNotIn("NOT copied", self.rep.text())
        # verify (button path) covers the paired JPG once the toggle is on...
        v = run_verify(self.cfg, str(self.card), ListReporter(), self.cancel)
        self.assertEqual(v.total, 2)
        # ...and a missing paired JPG is then reported as a problem
        next(self.vault.rglob("A.JPG")).unlink()
        v = run_verify(self.cfg, str(self.card), ListReporter(), self.cancel)
        self.assertEqual(v.names[MISSING], ["A.JPG"])

    def test_rerun_skips_duplicates_and_copies_nothing_new(self):
        self.card_file("A.NEF"); self.card_file("C.MP4")
        self.ingest()
        before = self.vault_files()
        self.rep = ListReporter()
        res = self.ingest()
        self.assertEqual(res.duplicates, 2)
        self.assertEqual(res.copied, 0)
        self.assertEqual(self.vault_files(), before)

    def test_same_name_different_content_is_kept_not_overwritten(self):
        a = self.card_file("DSC_0001.NEF", data=b"first" * 100)
        self.ingest()
        a.write_bytes(b"other" * 100)
        t = stamp(); os.utime(a, (t, t))
        res = self.ingest()
        self.assertEqual(res.copied, 1)
        names = [n for n in self.vault_files() if "CAM_RAW" in n]
        self.assertEqual(sorted(Path(n).name for n in names), ["DSC_0001.NEF", "DSC_0001_1.NEF"])

    def test_two_cards_folders_with_same_filename_same_day(self):
        self.card_file("DSC_0001.NEF", data=b"one" * 100, folder=self.card / "DCIM" / "100A")
        self.card_file("DSC_0001.NEF", data=b"two" * 100, folder=self.card / "DCIM" / "101A")
        res = self.ingest()
        self.assertEqual(res.copied, 2)
        self.assertEqual(len([n for n in self.vault_files() if "CAM_RAW" in n]), 2)

    def test_multiple_shoots_get_separate_named_folders(self):
        self.card_file("1.NEF", when=stamp(hour=9)); self.card_file("2.NEF", when=stamp(hour=15))
        self.card_file("t.wav", when=stamp(hour=15, minute=5))

        seen = {}

        class TwoShoots(Hooks):
            def choose_shoot_folders(self, shoots): return True
            def describe_jobs(self, count, multiple, shoots=None):
                seen["shoots"] = shoots
                return ["Morning: A", "Afternoon"]
        res = self.ingest(hooks=TwoShoots())
        # the naming step is handed the shoots, so a UI can show what each one contains
        self.assertEqual([len(s.files) for s in seen["shoots"]], [1, 1])   # audio joins its nearest shoot later
        self.assertTrue(res.ok, self.rep.text())
        files = self.vault_files()
        self.assertIn("2026_10/2026_10_07 Morning- A/RAW/CAM_RAW/1.NEF", files)
        self.assertIn("2026_10/2026_10_07 Afternoon/RAW/CAM_RAW/2.NEF", files)
        self.assertIn("2026_10/2026_10_07 Afternoon/RAW/AUDIO/t.wav", files)

    def test_cancel_describe_dialog_copies_nothing(self):
        self.card_file("A.NEF")

        class Cancelling(Hooks):
            def describe_jobs(self, count, multiple, shoots=None): return None
        res = self.ingest(hooks=Cancelling())
        self.assertTrue(res.cancelled)
        self.assertFalse(self.vault.exists() and any(self.vault.rglob("*.NEF")))

    def test_preview_lists_only_files_that_will_be_copied(self):
        class Thumbs(ListReporter):
            def __init__(self): super().__init__(); self.names = []; self.extra = None
            def thumbs_reset(self, extra_count): self.extra = extra_count
            def thumb(self, name, ext, data): self.names.append(name)
        self.card_file("A.NEF"); self.card_file("A.JPG"); self.card_file("B.JPG")  # A.JPG is paired
        self.rep = Thumbs()
        self.ingest(dry_run=True)
        self.assertEqual(sorted(self.rep.names), ["A.NEF", "B.JPG"])
        self.assertEqual(self.rep.extra, 0)

    def test_missing_card_aborts_cleanly(self):
        shutil.rmtree(self.card / "DCIM")
        res = self.ingest()
        self.assertIn("SD card not found", res.aborted)


@unittest.skipUnless(os.name == "nt", "fake exiftool is a .bat")
class TestTaggingFallback(Sandbox):
    """Regression: ExifTool skips the copy entirely when it can't write tags
    (Insta360 files) - the file must still reach the vault."""

    def setUp(self):
        super().setUp()
        self.fake = self.tmp / "fake_exiftool.bat"
        self.fake.write_text("@echo off\r\necho     1 files weren't updated due to errors\r\nexit /b 1\r\n")
        self.cfg.exiftool = str(self.fake)
        self.cfg.copyright = "(c) 2026 Test"

    def test_still_whose_tags_cannot_be_written_is_copied_untagged(self):
        src = self.card_file("A.NEF", data=os.urandom(5000))
        res = self.ingest()
        self.assertTrue(res.ok, self.rep.text())
        out = next(self.vault.rglob("A.NEF"))
        self.assertEqual(sha256_file(out), sha256_file(src))
        self.assertIn("copying without tags", self.rep.text())
        self.assertEqual(list(self.vault.rglob("*.part")), [])

    def test_video_is_copied_byte_for_byte_without_calling_exiftool(self):
        src = self.card_file("C.MP4", data=os.urandom(5000))
        res = self.ingest()
        self.assertTrue(res.ok, self.rep.text())
        self.assertEqual(sha256_file(next(self.vault.rglob("C.MP4"))), sha256_file(src))
        self.assertNotIn("without tags", self.rep.text())


class TestVerify(Sandbox):
    def test_detects_missing_truncated_and_corrupt_copies(self):
        self.card_file("ok.NEF", data=b"a" * 3000)
        self.card_file("short.mp4", data=b"b" * 3000)
        self.card_file("flip.mp4", data=b"c" * 3000)
        self.card_file("gone.wav", data=b"d" * 3000)
        self.ingest()
        (next(self.vault.rglob("short.mp4"))).write_bytes(b"b" * 1000)        # truncated
        flip = next(self.vault.rglob("flip.mp4"))
        flip.write_bytes(b"x" + flip.read_bytes()[1:])                          # same size, corrupt
        next(self.vault.rglob("gone.wav")).unlink()                             # missing
        scan = scan_card(self.card / "DCIM", self.cfg)
        v = verify_files(scan.ingest_files(), self.vault, None, self.rep, self.cancel, deep=True)
        self.assertEqual(v.names[MISSING], ["gone.wav"])
        self.assertEqual(v.names[SIZE], ["short.mp4"])
        self.assertEqual(v.names[HASH], ["flip.mp4"])
        self.assertEqual(v.hash_verified, 1)
        self.assertFalse(v.ok)

    def test_quick_verify_after_ingest_flags_a_truncated_file(self):
        self.card_file("A.mp4", data=b"a" * 3000)
        self.ingest()
        next(self.vault.rglob("A.mp4")).write_bytes(b"a" * 10)
        scan = scan_card(self.card / "DCIM", self.cfg)
        v = verify_files(scan.ingest_files(), self.vault, None, self.rep, self.cancel, deep=False)
        self.assertEqual(v.names[SIZE], ["A.mp4"])

    def test_same_named_files_elsewhere_in_the_vault_are_not_read(self):
        """Cameras reuse counters, so 850_5315.NEF can exist in many shoots; each extra
        ExifTool read is slow on a USB HDD. The verify must only look at the right copy."""
        class CountingExif:
            available = True
            def __init__(self): self.paths = []
            def image_data_md5(self, path):
                self.paths.append(Path(path))
                return "ab" * 16

        src = self.card_file("A.NEF", data=b"s" * 3000)
        tagged = self.vault / "2026_10" / "2026_10_07" / "RAW" / "CAM_RAW" / "A.NEF"
        tagged.parent.mkdir(parents=True)
        tagged.write_bytes(b"t" * 3100)                       # "tagged": different size
        os.utime(tagged, (stamp(), stamp()))
        old = self.vault / "2025_01" / "2025_01_01" / "RAW" / "CAM_RAW" / "A.NEF"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"o" * 4000)                          # an old shoot reusing the name
        os.utime(old, (stamp(2025, 1, 1), stamp(2025, 1, 1)))
        scan = scan_card(self.card / "DCIM", self.cfg)
        for kwargs in ({}, {"known_targets": {src: tagged}}):
            exif = CountingExif()
            v = verify_files(scan.ingest_files(), self.vault, exif, self.rep, self.cancel,
                             deep=False, **kwargs)
            self.assertEqual(v.tagged, 1, kwargs)
            self.assertNotIn(old, exif.paths, kwargs)

    def test_run_verify_button_path(self):
        self.card_file("A.NEF"); self.card_file("C.MP4")
        self.ingest()
        self.rep = ListReporter()
        v = run_verify(self.cfg, str(self.card), self.rep, self.cancel)
        self.assertTrue(v.ok)
        self.assertEqual(v.hash_verified, 2)
        self.assertIn("Safe to format SD card", self.rep.text())

    def test_run_verify_reports_missing_card(self):
        shutil.rmtree(self.card / "DCIM")
        self.assertIsNone(run_verify(self.cfg, str(self.card), self.rep, self.cancel))
        self.assertIn("SD card not found", self.rep.text())


@unittest.skipUnless(os.path.isfile(REAL_EXIFTOOL), "needs exiftool")
class TestRealExifTool(Sandbox):
    def make_jpeg(self, name, when=None):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("needs Pillow to build a test JPEG")
        p = self.dcim / name
        Image.effect_noise((64, 64), 60).convert("RGB").save(p, "JPEG")
        t = when or stamp()
        os.utime(p, (t, t))
        return p

    def test_copyright_is_written_and_verify_accepts_the_tagged_copy(self):
        self.cfg.exiftool = REAL_EXIFTOOL
        self.cfg.copyright = "(c) 2026 Test Studio"
        self.cfg.raw_ext = "jpg"          # treat the JPEG as a RAW still so it gets tagged
        src = self.make_jpeg("S.jpg")
        res = self.ingest()
        self.assertTrue(res.ok, self.rep.text())
        self.assertEqual(res.verify.tagged, 1)
        self.assertEqual(res.verify.disk_checked, 1, self.rep.text())   # sample is at least 1 file
        out = next(self.vault.rglob("S.jpg"))
        self.assertNotEqual(sha256_file(out), sha256_file(src))   # tag written -> bytes differ
        exif = ExifTool(REAL_EXIFTOOL)
        self.addCleanup(exif.close)   # an unclosed session leaves exiftool.exe processes behind
        self.assertEqual(exif.detect_copyright([out]), "(c) 2026 Test Studio")
        self.assertEqual(exif.image_data_md5(out), exif.image_data_md5(src))
        v = run_verify(self.cfg, str(self.card), self.rep, self.cancel)
        self.assertTrue(v.ok)
        self.assertEqual(v.tagged, 1)
        # re-running must not create S_1.jpg: the tagged copy is recognised as the same image
        res2 = self.ingest()
        self.assertEqual(res2.duplicates, 1, self.rep.text())
        self.assertEqual(len(list(self.vault.rglob("S*.jpg"))), 1)

    def test_one_session_tags_many_files_including_non_ascii_paths(self):
        """The persistent -stay_open process must survive repeated commands, and (unlike a
        one-shot launch) handle accented/CJK folder names."""
        exif = ExifTool(REAL_EXIFTOOL)
        self.addCleanup(exif.close)
        src = self.make_jpeg("S.jpg")
        folder = self.tmp / "vault" / "café 日本"
        folder.mkdir(parents=True)
        for i in range(3):
            dst = folder / f"out{i}.jpg"
            ok, lines = exif.write_copy(src, dst, ["-Copyright=(c) Café"])
            self.assertTrue(ok, lines)
            self.assertEqual(exif.image_data_md5(dst), exif.image_data_md5(src))
        self.assertIn("(c) Café", exif._exec(["-s3", "-Copyright", str(folder / "out2.jpg")])[1])

    def test_cancel_during_session_raises_cancelled(self):
        exif = ExifTool(REAL_EXIFTOOL, self.cancel)
        self.addCleanup(exif.close)
        src = self.make_jpeg("S.jpg")
        self.assertTrue(exif.image_data_md5(src))
        self.cancel.cancel()
        with self.assertRaises(Cancelled):
            exif.image_data_md5(src)

    def test_detect_copyright_ignores_untagged_files_and_exiftool_headers(self):
        """Regression: with several files ExifTool prints '======== path' headers; an
        untagged card used to 'detect' one of those as the copyright."""
        import subprocess
        exif = ExifTool(REAL_EXIFTOOL)
        a, b, c = (self.make_jpeg(n) for n in ("a.jpg", "b.jpg", "c.jpg"))
        self.assertEqual(exif.detect_copyright([a, b, c]), "")           # nothing tagged
        for p in (a, b):
            subprocess.run([REAL_EXIFTOOL, "-overwrite_original", "-Copyright=(c) Mine", str(p)],
                           check=True, capture_output=True)
        self.assertEqual(exif.detect_copyright([a, b, c]), "(c) Mine")   # c is untagged

    def test_dry_run_detects_the_cards_copyright_tag(self):
        self.cfg.exiftool = REAL_EXIFTOOL
        self.cfg.raw_ext = "jpg"
        src = self.make_jpeg("S.jpg")
        import subprocess
        subprocess.run([REAL_EXIFTOOL, "-overwrite_original", "-Copyright=(c) Card Owner", str(src)],
                       check=True, capture_output=True)
        res = self.ingest(dry_run=True)
        self.assertEqual(res.detected_copyright, "(c) Card Owner")


if __name__ == "__main__":
    unittest.main()
