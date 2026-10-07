"""GUI smoke test: drives the real window (hidden) through dry run -> live ingest -> verify."""

from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vaultflow import gui
from vaultflow.config import Config, save_config


def stamp(hour: int) -> float:
    return time.mktime((2026, 10, 7, hour, 0, 0, 0, 0, -1))


class TestGuiFlow(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("no display available")
        self.root.withdraw()
        self.addCleanup(self._close)
        self.tmp = Path(tempfile.mkdtemp(prefix="vaultflow_gui_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        card = self.tmp / "card"
        for name, hour, size in (("1.NEF", 9, 4000), ("2.NEF", 15, 3000), ("V.MP4", 15, 6000)):
            p = card / "DCIM" / "100CAM" / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(os.urandom(size))
            os.utime(p, (stamp(hour), stamp(hour)))
        self.card = card
        self.vault = self.tmp / "vault"
        cfg_path = self.tmp / "vaultflow_config.json"
        save_config(cfg_path, Config(dest=str(self.vault), log_dir=str(self.tmp / "logs"),
                                     exiftool=str(self.tmp / "none.exe"), raw_ext="nef",
                                     video_ext="mp4", audio_ext="wav", aux_ext=""))
        patches = [mock.patch.object(gui, "CONFIG_PATH", cfg_path),
                   mock.patch.object(gui, "detect_sd_cards", lambda: []),
                   mock.patch.object(gui.os, "startfile", lambda *_: None, create=True),
                   mock.patch.object(gui.messagebox, "showinfo", self._record("info")),
                   mock.patch.object(gui.messagebox, "showwarning", self._record("warning")),
                   mock.patch.object(gui.ShootDialog, "show", lambda self_: True),
                   mock.patch.object(gui.JobDialog, "show", lambda self_: ["Job A", "Job B"])]
        self.popups: list[tuple[str, str]] = []
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = gui.App(self.root)
        self.app.cmb_drive.set(str(card))        # DCIM lives at <drive>/DCIM

    def _record(self, kind):
        return lambda title, message, **kw: self.popups.append((kind, message))

    def _close(self):
        try:
            self.app._on_close()
            worker = self.app.worker
            if worker is not None:
                worker.join(10)
        except (tk.TclError, AttributeError):
            pass
        self.app = None
        gc.collect()   # free Tk objects here, on the main thread

    def wait_idle(self, timeout=30):
        end = time.time() + timeout
        while self.app.busy or not self.app.q.empty():
            self.root.update()
            time.sleep(0.01)
            self.assertLess(time.time(), end, "timed out waiting for the worker")
        self.root.update()

    def log_text(self) -> str:
        return self.app.log.get("1.0", "end")

    def test_dry_run_then_live_then_verify(self):
        # live is locked until a dry run has completed
        self.assertEqual(str(self.app.b_live["state"]), "disabled")

        self.app._start("dry")
        self.wait_idle()
        self.assertIn("WOULD COPY >> CAM_RAW", self.log_text())
        self.assertFalse(self.vault.exists())
        self.assertEqual(str(self.app.b_live["state"]), "normal")
        self.assertEqual(self.app.l_status.cget("text")[:7], "Dry run")

        self.app._start("live")
        self.wait_idle()
        files = sorted(p.relative_to(self.vault).as_posix() for p in self.vault.rglob("*") if p.is_file())
        self.assertEqual(files, ["2026_10/2026_10_07 Job A/RAW/CAM_RAW/1.NEF",
                                 "2026_10/2026_10_07 Job B/RAW/CAM_RAW/2.NEF",
                                 "2026_10/2026_10_07 Job B/RAW/VIDEO/V.MP4"])
        self.assertEqual(self.popups[-1][0], "info")
        self.assertIn("3 files copied", self.popups[-1][1])
        self.assertEqual(str(self.app.b_open["state"]), "normal")
        self.assertEqual(int(self.app.bar["value"]), 100)
        self.assertTrue(list((self.tmp / "logs").glob("ingest_*.log")))

        self.app._start("verify")
        self.wait_idle()
        self.assertIn("Safe to format SD card", self.log_text())
        self.assertEqual(self.app.l_status.cget("text"), "Verified OK.")

        # a truncated vault file must be flagged
        next(self.vault.rglob("V.MP4")).write_bytes(b"x")
        self.app._start("verify")
        self.wait_idle()
        self.assertIn("SIZE: V.MP4", self.log_text())
        self.assertIn("failed", self.app.l_status.cget("text"))

    def test_card_with_more_than_the_preview_cap_does_not_kill_the_ui(self):
        """Regression: the '+N more' label used grid column 100000 (Tk max is 9999), the
        resulting error killed the message pump and the window stopped updating."""
        for i in range(60):
            p = self.card / "DCIM" / "100CAM" / f"X{i:03d}.NEF"
            p.write_bytes(os.urandom(500))
            os.utime(p, (stamp(9), stamp(9) + i))
        self.app._start("dry")
        self.wait_idle()
        self.assertEqual(self.app.l_status.cget("text")[:7], "Dry run", self.log_text())
        more = [w for w in self.app.strip_inner.winfo_children()
                if w.winfo_class() == "Label" and str(w.cget("text")).startswith("+")]
        self.assertEqual(len(more), 1)
        self.assertEqual(more[0].cget("text"), "+13 more")      # 62 RAW + 1 video = 63 files, 50 previewed
        # a label that exists but failed to be placed would otherwise pass this test
        self.assertEqual(more[0].winfo_manager(), "grid")
        self.assertEqual(more[0].grid_info()["column"], 50)     # right after the 50 thumbnails
        self.assertNotIn("UI update failed", self.log_text())
        self.assertIn("Found 62 RAW files", self.log_text())

    def test_a_bad_ui_message_does_not_stop_later_ones(self):
        self.app.q.put(("thumbs_reset", 5))
        self.app.q.put(("progress", "not", "a", "valid", "message"))      # raises inside _handle
        self.app.q.put(("status", "still alive"))
        for _ in range(10):
            self.root.update()
            time.sleep(0.05)
        self.assertEqual(self.app.l_status.cget("text"), "still alive")
        self.assertIn("UI update failed", self.log_text())

    def test_stop_cancels_and_unlocks_the_window(self):
        self.app._start("dry")
        self.app._stop()
        self.wait_idle()
        self.assertFalse(self.app.busy)
        self.assertEqual(str(self.app.b_dry["state"]), "normal")

    def test_copyright_field_ignores_placeholder_and_save_round_trips(self):
        self.assertEqual(self.app.e_copyright.get_value(), "")
        self.app.e_copyright.set_value("(c) 2026 Test")
        self.assertEqual(self.app._gather().copyright, "(c) 2026 Test")
        self.app._save()
        self.assertIn('"(c) 2026 Test"', gui.CONFIG_PATH.read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
