"""Progress/log sink the engine writes to. The GUI supplies a thread-safe one."""

from __future__ import annotations

import datetime
from pathlib import Path

# Log levels the GUI maps to colours: info, dim, accent, ok, warn, error.


class Reporter:
    """No-op base. The engine calls these from a worker thread."""

    def log(self, msg: str, level: str = "info") -> None: ...

    def status(self, text: str) -> None: ...

    def current(self, text: str) -> None: ...

    def progress(self, files_done: int, files_total: int, percent: int,
                 speed: float, eta: float) -> None: ...

    def thumbs_reset(self, extra_count: int) -> None: ...

    def thumb(self, name: str, ext: str, data: bytes | None) -> None: ...

    def highlight(self, name: str) -> None: ...


class ListReporter(Reporter):
    """Collects everything in memory - used by the tests."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def log(self, msg: str, level: str = "info") -> None:
        self.lines.append((level, msg))

    def text(self) -> str:
        return "\n".join(m for _, m in self.lines)


class TeeReporter(Reporter):
    """Forwards to another reporter and also appends every log line to a file."""

    def __init__(self, inner: Reporter, path: Path | None) -> None:
        self.inner = inner
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, msg: str, level: str = "info") -> None:
        self.inner.log(msg, level)
        if self.path is not None:
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    for line in msg.splitlines() or [""]:
                        fh.write(f"{stamp}  {line}\n")
            except OSError:
                pass  # a log-file problem must never abort an ingest

    def status(self, text): self.inner.status(text)
    def current(self, text): self.inner.current(text)
    def progress(self, *args): self.inner.progress(*args)
    def thumbs_reset(self, extra_count): self.inner.thumbs_reset(extra_count)
    def thumb(self, name, ext, data): self.inner.thumb(name, ext, data)
    def highlight(self, name): self.inner.highlight(name)
