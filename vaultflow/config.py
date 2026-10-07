"""Settings, stored in vaultflow_config.json (same file and keys as the PowerShell version)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "vaultflow_config.json"

DEFAULT_RAW_EXT = "nef, cr2, cr3, arw, raf, orf, rw2, dng"

# dataclass field -> JSON key (PascalCase, as the PowerShell version wrote it)
_KEYS = {
    "dest": "Dest",
    "log_dir": "LogDir",
    "exiftool": "Exiftool",
    "raw_ext": "RawExt",
    "video_ext": "VideoExt",
    "audio_ext": "AudioExt",
    "aux_ext": "AuxExt",
    "copyright": "Copyright",
}


@dataclass
class Config:
    dest: str
    log_dir: str
    exiftool: str
    raw_ext: str = DEFAULT_RAW_EXT
    video_ext: str = "mp4"
    audio_ext: str = "wav"
    aux_ext: str = ""
    copyright: str = ""


def default_config() -> Config:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    vault = home / "Pictures" / "PhotoVault"
    return Config(dest=str(vault), log_dir=str(vault / "_logs"),
                  exiftool=r"C:\exiftool\exiftool.exe")


def load_config(path: Path) -> Config:
    cfg = default_config()
    try:
        # utf-8-sig: Windows PowerShell's Set-Content wrote a BOM
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    for attr, key in _KEYS.items():
        value = raw.get(key)
        if isinstance(value, str) and value != "":
            # An old PowerShell default of just "nef" meant "never customised".
            if attr == "raw_ext" and value.strip().lower() == "nef":
                continue
            setattr(cfg, attr, value)
    return cfg


def save_config(path: Path, cfg: Config) -> None:
    data = {key: getattr(cfg, attr) for attr, key in _KEYS.items()}
    Path(path).write_text(json.dumps(data, indent=4), encoding="utf-8")
