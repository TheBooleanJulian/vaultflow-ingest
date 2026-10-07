"""Thin wrapper around the external exiftool.exe (the only non-stdlib tool VaultFlow needs)."""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from pathlib import Path

from .util import NO_WINDOW, CancelToken

_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class ExifTool:
    def __init__(self, path: str, cancel: CancelToken | None = None) -> None:
        self.path = (path or "").strip()
        self.cancel = cancel

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
            _, out, _ = self._run(["-m", "-s3", "-ImageDataMD5", str(path)])
        except OSError:
            return ""
        text = out.decode("utf-8", "replace").strip()
        return text.lower() if _MD5_RE.match(text) else ""

    def write_copy(self, src: Path, dst: Path, meta_args: list[str]) -> tuple[bool, list[str]]:
        """Copy src to dst with metadata tags written. Returns (ok, warning/error lines).

        ExifTool silently skips the copy (and exits non-zero) when it can't write
        tags - e.g. Insta360 MP4/LRV files with a proprietary trailer - so a False
        result means nothing usable exists at dst and the caller should fall back.
        """
        rc, out, err = self._run([*meta_args, "-o", str(dst), str(src)])
        text = out.decode("utf-8", "replace") + "\n" + err.decode("utf-8", "replace")
        lines = [ln.strip() for ln in text.splitlines()
                 if ln.strip().startswith(("Error", "Warning"))]
        failed = rc != 0 or "weren't updated" in text or not dst.exists()
        return (not failed), lines
