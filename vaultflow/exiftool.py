"""Thin wrapper around the external exiftool.exe (the only non-stdlib tool VaultFlow needs)."""

from __future__ import annotations

import os
import re
import subprocess
import threading
from collections import Counter
from pathlib import Path

from .util import NO_WINDOW, CancelToken

_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class ExifTool:
    def __init__(self, path: str, cancel: CancelToken | None = None) -> None:
        self.path = (path or "").strip()
        self.cancel = cancel
        self._proc: subprocess.Popen | None = None   # persistent -stay_open process
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.path) and os.path.isfile(self.path)

    def _run(self, args: list[str]) -> tuple[int, bytes, bytes]:
        proc = subprocess.Popen([self.path, *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        if self.cancel:
            self.cancel.attach(proc)
        try:
            out, err = proc.communicate()
        finally:
            if self.cancel:
                self.cancel.detach(proc)
        return proc.returncode, out, err

    # -- persistent session ---------------------------------------------------
    # exiftool.exe takes ~0.8s just to start, which dominated a card of hundreds of files
    # (several launches per file). One long-lived -stay_open process pays that once.

    def _start(self) -> subprocess.Popen:
        proc = subprocess.Popen([self.path, "-charset", "filename=utf8", "-stay_open", "True", "-@", "-"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        if self.cancel:
            self.cancel.attach(proc)
        self._proc = proc
        return proc

    def _drop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if self.cancel:
            self.cancel.detach(proc)
        try:
            proc.kill()
            proc.stdout.close()
        except OSError:
            pass

    def _session_run(self, args: list[str]) -> str | None:
        """Output of one command run in the persistent process, or None if the session is
        unusable (caller falls back to a one-shot run). Raises Cancelled if STOP was pressed."""
        with self._lock:
            try:
                proc = self._proc if self._proc and self._proc.poll() is None else self._start()
                command = ["-charset", "filename=utf8", *args, "-execute", ""]
                proc.stdin.write("\n".join(command).encode("utf-8"))
                proc.stdin.flush()
                lines: list[str] = []
                while True:
                    raw = proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.strip() == "{ready}":
                        return "\n".join(lines)
                    lines.append(line)
            except OSError:
                pass
            self._drop()
        if self.cancel:
            self.cancel.check()
        return None

    def _exec(self, args: list[str]) -> tuple[int, str]:
        """(exit code, combined output). Session exit codes aren't available, so it reports 0
        and callers judge success from the output and the files on disk."""
        text = self._session_run(args)
        if text is not None:
            return 0, text
        rc, out, err = self._run(args)
        return rc, out.decode("utf-8", "replace") + "\n" + err.decode("utf-8", "replace")

    def close(self) -> None:
        """End the persistent process (safe to call repeatedly)."""
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.write(b"-stay_open\nFalse\n")
            proc.stdin.close()
            proc.wait(timeout=5)
            proc.stdout.close()
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        finally:
            if self.cancel:
                self.cancel.detach(proc)

    def thumbnail(self, path: Path) -> bytes | None:
        """Embedded JPEG thumbnail/preview, or None (video, unsupported, no tag)."""
        if not self.available:
            return None
        for tag in ("ThumbnailImage", "PreviewImage"):
            try:
                _, out, _ = self._run(["-b", f"-{tag}", "-q", "-q", str(path)])
            except OSError:
                return None
            if out:
                return out
        return None

    def detect_copyright(self, paths: list[Path], limit: int = 20) -> str:
        """Most common non-empty Copyright tag among the first `limit` files ('' if none)."""
        sample = [str(p) for p in paths[:limit]]
        if not self.available or not sample:
            return ""
        try:
            # -p prints just the values. (-s3 with several files also prints a
            # "======== path" header per file, which used to be mistaken for a copyright.)
            _, out, _ = self._run(["-m", "-p", "$Copyright", *sample])
        except OSError:
            return ""
        values = [ln.strip() for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
        return Counter(values).most_common(1)[0][0] if values else ""

    def image_data_md5(self, path: Path) -> str:
        """Hash of just the image data - unchanged by a metadata-only rewrite. '' if unsupported."""
        if not self.available:
            return ""
        try:
            _, text = self._exec(["-m", "-s3", "-ImageDataMD5", str(path)])
        except OSError:
            return ""
        for line in text.splitlines():
            if _MD5_RE.match(line.strip()):
                return line.strip().lower()
        return ""

    def write_copy(self, src: Path, dst: Path, meta_args: list[str]) -> tuple[bool, list[str]]:
        """Copy src to dst with metadata tags written. Returns (ok, warning/error lines).

        ExifTool silently skips the copy (and exits non-zero) when it can't write
        tags - e.g. Insta360 MP4/LRV files with a proprietary trailer - so a False
        result means nothing usable exists at dst and the caller should fall back.
        """
        rc, text = self._exec([*meta_args, "-o", str(dst), str(src)])
        lines = [ln.strip() for ln in text.splitlines()
                 if ln.strip().startswith(("Error", "Warning"))]
        failed = (rc != 0 or "weren't updated" in text or not dst.exists()
                  or any(ln.startswith("Error") for ln in lines))
        return (not failed), lines
