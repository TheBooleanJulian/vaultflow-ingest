"""Small shared helpers: formatting, extension parsing, cancellation, progress."""

from __future__ import annotations

import re
import subprocess
import threading
import time

# Stops a console window flashing up for every ExifTool call on Windows.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Cancelled(Exception):
    """Raised inside a worker thread when the user pressed STOP."""


class CancelToken:
    """Cooperative cancel flag that can also kill a running ExifTool process."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            for proc in list(self._procs):
                try:
                    proc.kill()
                except OSError:
                    pass

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def attach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)
        if self._event.is_set():
            try:
                proc.kill()
            except OSError:
                pass

    def detach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)


class ProgressTracker:
    """Bytes-done counter with speed and ETA, measured from construction.

    `on_update(tracker)` is called from add(), at most every `interval` seconds,
    so a long single file still moves the progress bar.
    """

    def __init__(self, total_bytes: int, on_update=None, interval: float = 0.2) -> None:
        self.total = total_bytes
        self.done = 0
        self._t0 = time.monotonic()
        self._on_update = on_update
        self._interval = interval
        self._last = 0.0

    def add(self, n: int) -> None:
        self.done += n
        if self._on_update:
            now = time.monotonic()
            if now - self._last >= self._interval:
                self._last = now
                self._on_update(self)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return min(int(self.done * 100 / self.total), 100)

    def speed(self) -> float:
        elapsed = time.monotonic() - self._t0
        return self.done / elapsed if elapsed > 1 and self.done > 0 else 0.0

    def eta(self) -> float:
        speed = self.speed()
        return (self.total - self.done) / speed if speed > 0 else 0.0


def fmt_bytes(n: float) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):,.2f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):,.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):,.0f} KB"
    return f"{int(n)} B"


def fmt_speed(bps: float) -> str:
    if bps >= 1 << 20:
        return f"{bps / (1 << 20):,.1f} MB/s"
    if bps >= 1 << 10:
        return f"{bps / (1 << 10):,.0f} KB/s"
    return f"{int(bps)} B/s"


def fmt_eta(secs: float) -> str:
    if secs <= 0:
        return "--:--"
    secs = int(secs)
    hours, rem = divmod(secs, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def parse_ext_list(raw: str) -> list[str]:
    """'nef, cr2 , .ARW' -> ['nef', 'cr2', 'arw'] (lowercase, no dots)."""
    out = []
    for part in (raw or "").split(","):
        ext = part.strip().lstrip(".").lower()
        if ext and ext not in out:
            out.append(ext)
    return out


_INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_description(text: str) -> str:
    """Make a job description safe to use inside a Windows folder name."""
    return _INVALID_NAME_CHARS.sub("-", text.strip()).rstrip(" .")
