<div align="center">

![VaultFlow](assets/vaultflow-logo.png)

# VaultFlow Ingest

**A Windows GUI tool that ingests SD card photos and video into a dated vault — sorted by capture date, deduped, verified, then ready to format.**

![Version](https://img.shields.io/badge/version-1.8.0-00D4C8)
![Python](https://img.shields.io/badge/-Python%203-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-AGPLv3%20%2F%20Commercial-00D4C8.svg)

</div>

---

## What it does

VaultFlow Ingest is a Windows GUI utility for photographers and videographers. It auto-detects the SD card and sorts your RAW, video, audio, and auxiliary/proxy footage into a structured vault by capture date — skipping duplicates, flagging orphan JPGs, tagging your copyright into stills, and verifying the copy before you format the card. It's camera-agnostic: tell it your RAW/video/audio/aux file extensions once (Canon CR2/CR3, Sony ARW, Fujifilm RAF, GoPro/Insta360 LRV/INSV, WAV field audio, etc.) and it works the same as it does for a Nikon NEF shooter.

Each ingest creates a client-ready folder structure per shoot day, with `RAW` pre-sorted by media type:

```
Dest\YYYY_MM\YYYY_MM_DD[ Job description]\
  |-- RAW
  |   |-- CAM_RAW     <- RAW files (nef, cr2, arw, ...)
  |   |-- JPG         <- orphan JPGs with no matching RAW file
  |   |-- VIDEO       <- video files (mp4, mov, ...)
  |   |-- AUDIO       <- audio files (wav, mp3, ...)
  |   `-- MISC        <- proxy/360 footage (lrv, insv, ...)
  |-- PX01            <- empty export target (Photoshop round 1)
  `-- LR01            <- empty export target (Lightroom round 1)
```

The day folder comes from each file's **modified date**, which cameras set to the capture time (local), so it is correct for Insta360/GoPro clips whose embedded timestamps are UTC.

## Features

- Auto-detects the SD card (removable drive with a `DCIM` folder) without ever freezing the window
- Camera-agnostic file typing — configure your own RAW / video / audio / auxiliary extensions
- **Dry run first**: Live Ingest stays locked until a dry run has previewed what will be copied; a dry run also reads any Copyright tag already on the card and pre-fills the Copyright box
- Detects multiple shoots (30+ minute gaps) and offers a folder per shoot, each with its own job description
- Skips true duplicates (same name, size **and** SHA-256); a same-name file with different content is kept as `name_1.ext`, never overwritten
- Copies are **atomic** (`name.part` then renamed), so a stopped or failed copy can never leave a truncated file in the vault; STOP works mid-file
- Writes your **Copyright** into RAW/JPG stills via ExifTool. Video, audio and proxy files are copied byte-for-byte (ExifTool can't tag Insta360 clips, and tagging rewrites the whole file)
- Verifies the vault against the card after every live ingest (presence + size), and a **Verify Card vs Vault** button does a full SHA-256 comparison on demand. Copyright-tagged stills are confirmed by comparing image data, since their bytes legitimately differ
- Tells you about files it does *not* copy (JPGs paired with a RAW, other file types) before you format
- Real log files in your log folder for every live ingest and verify
- Completion popup when a live ingest finishes; optional eject (only if everything verified)

## Tech Stack

| Layer | Choice |
|---|---|
| App | Python 3 + Tkinter (standard library only) |
| EXIF / tagging | [ExifTool](https://exiftool.org/) (external) |
| Thumbnails (optional) | [Pillow](https://python-pillow.org/) — without it the preview shows file-type tiles instead of photo thumbnails |

The original PowerShell version is archived, still working, in [`archive/powershell/`](archive/powershell) (git tag `powershell-final`).

## Screenshots

_Screenshots coming soon — the logo above is the only visual asset in the repo so far._

## Quick Start

1. Run `Run VaultFlow Ingest.bat` (or `python vaultflow_ingest.py`).
2. Set your **Vault Destination**, **Log Folder**, and **ExifTool Path** under Paths, then click **Save Paths**. An existing `vaultflow_config.json` from the PowerShell version is picked up as-is.
3. Under **File Types**, set your camera's extensions — e.g. **RAW**: `nef` (Nikon), `cr2, cr3` (Canon), `arw` (Sony); **Video**: `mp4, mov`; **Audio**: `wav, mp3`; **MISC** (optional): `lrv, insv` for GoPro/Insta360 proxy or 360 footage.
4. Optionally enter your **Copyright**.
5. Confirm the detected **SD Card Drive** (or pick manually); toggle **Eject SD card after ingest** if wanted.
6. Click **START DRY RUN** to preview, then **START LIVE INGEST** to copy. Before formatting the card, run **VERIFY CARD vs VAULT** if you want a full byte-level check.

## Requirements

- Windows with Python 3.10+ (Tkinter ships with the standard Windows installer)
- [ExifTool](https://exiftool.org/) installed and its path set in the UI/config — needed for copyright tagging, photo thumbnails and copyright detection; plain copying works without it
- Optional: `pip install pillow` for photo thumbnails

## Configuration

| Field | Required | Description |
|---|---|---|
| `Dest` | Yes | Root path of your photo vault, e.g. `D:\Photos\Vault` |
| `LogDir` | Yes | Where ingest/verify logs are written, e.g. `D:\Photos\Vault\_logs` |
| `Exiftool` | No* | Full path to the ExifTool executable, e.g. `C:\exiftool\exiftool.exe` (*required if you set a Copyright) |
| `RawExt` | Yes | Comma-separated RAW extensions, no dots, e.g. `nef, cr2, cr3, arw, raf, orf, rw2, dng` |
| `VideoExt` | Yes | Comma-separated video extensions, e.g. `mp4, mov` |
| `AudioExt` | No | Comma-separated audio extensions, e.g. `wav, mp3` |
| `AuxExt` | No | Comma-separated proxy/360 extensions, e.g. `lrv, insv` |
| `Copyright` | No | Written to the ExifTool `Copyright` tag of RAW/JPG stills, e.g. `(c) 2026 Jane Doe` |

Config is stored in `vaultflow_config.json` in the repo root (git-ignored). Copy `vaultflow_config.example.json` to get started.

## Project Structure

```
vaultflow-ingest/
|-- vaultflow_ingest.py           <- run this
|-- Run VaultFlow Ingest.bat
|-- vaultflow/                    <- the app
|   |-- gui.py                    <- Tkinter UI (worker thread + queue)
|   |-- ingest.py                 <- ingest & verify runs
|   |-- scan.py                   <- card scan, shoot grouping, SD detection
|   |-- files.py                  <- atomic copy, hashing, vault duplicate index
|   |-- verify.py                 <- size / SHA-256 verification
|   |-- exiftool.py               <- ExifTool wrapper
|   `-- config.py, util.py, report.py
|-- tests/                        <- python -m unittest discover -s tests
|-- archive/powershell/           <- the original PowerShell version (working, frozen)
|-- vaultflow_config.example.json
|-- assets/
|-- LICENSE
|-- COMMERCIAL-LICENSE.md
`-- README.md
```

Run the tests with `python -m unittest discover -s tests -v`. The GUI tests drive a hidden window; the ExifTool tests are skipped if `C:\exiftool\exiftool.exe` isn't present.

## Versioning

This project follows [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`), staying at `MAJOR` version `1` while it has a single known user and no compatibility contract to keep:

- **MAJOR** stays `1` until there's a reason to promise stability to other users
- **MINOR** — breaking changes to config format, folder structure, or workflow, as well as new backward-compatible features — both bump MINOR rather than MAJOR
- **PATCH** — bug fixes and small tweaks with no behavior change

As of 2026-08-09, versioning stops bumping MAJOR — `2.0.0`–`5.0.0` were retroactively renumbered to `1.3.0`–`1.6.0` so the changelog reads as one continuous MINOR sequence from `1.0.0` onward instead of implying a stability contract that was never real. See the changelog entry at `1.6.0` for details.

## Status / Roadmap

**Done**

- [x] EXIF-based date sorting for configurable RAW, video, audio, and auxiliary (proxy/360) file types
- [x] `01_RAW` split into `CAM_RAW`/`JPG`/`VIDEO`/`AUDIO`/`MISC` subfolders by media type
- [x] Camera-agnostic file typing — no longer hardcoded to a single body's extensions
- [x] Duplicate detection (name + size + checksum) and orphan JPG flagging
- [x] Dry run mode and live progress reporting
- [x] Post-ingest verification and optional SD card eject
- [x] Persisted JSON config with in-UI editing
- [x] Two-column WinForms layout with live output log
- [x] Optional auto-launch on SD card insertion via Task Scheduler
- [x] Client folder scaffold (`01_RAW`…`05_DELIVERED`) created automatically under every day folder
- [x] Enforced client-facing day-folder naming (`YYYY-MM-DD_ClientName_EventName`)
- [x] Metadata embedding (Copyright/Credit/Source/Keywords) with per-job-type keyword presets
- [x] Completion popup (Telegram notification removed)

**Planned / Suggestions**

- **Config profiles** — support multiple named destination/camera profiles (e.g. different bodies, different vault drives) instead of a single global config
- **Resume/retry on interruption** — recover cleanly from a killed process or dropped SD card mid-ingest instead of requiring a full re-run
- **Structured logging** — write machine-readable (JSON/CSV) ingest logs alongside the human-readable log for easier auditing over time
- **Packaging** — distribute as a signed `.exe` (e.g. via ps2exe) so it can run without an explicit PowerShell execution policy change
- **Cross-platform ingest core** — split the ingest/verify logic from the WinForms UI so a CLI-only mode is possible on non-Windows setups running PowerShell Core
- **CI parse-check** — a lightweight GitHub Actions workflow that runs PowerShell's AST parser against `vaultflow_ingest.ps1` on every push/PR, catching syntax errors before they reach a user's machine
- **Mirrored/dual-destination ingest** — copy to a second vault path (e.g. a backup drive) in the same pass, for photographers who want on-site redundancy before formatting the card
- **Toast notification on completion** — a native Windows notification most useful once auto-launch is running unattended in the background
- **Config schema versioning** — an internal `ConfigVersion` field so future config-shape changes can auto-migrate old files instead of silently falling back to defaults
- **Adjustable duplicate-check strictness** — an option to skip the MD5 checksum pass and trust name+size alone, for users ingesting very large cards where per-file hashing adds noticeable time
- **Job-type presets in the UI** — keyword presets per job type currently live in a hashtable in the script (`$JobTypeKeywords`); an in-UI editor would let users manage them without touching code
- **Per-job-type folder template variants** — the 5-folder client scaffold is currently one fixed template for every job type; some shooters may want a different structure for e.g. corporate vs. wedding work
- No `.env.example` equivalent is provided for the PowerShell config path defaults — a setup script or first-run wizard could reduce manual config steps
- No automated tests for ingest logic (duplicate detection, path construction, verification counts)

Suggestions and feedback welcome — open an issue or reach out directly.

## Changelog

All notable changes to this project are documented here, newest first. Versions prior to 1.0.0 predate this repository's git history (the tool evolved as a single script across iterations); dates below are only as precise as the available evidence — 0.5.0–0.8.0 are anchored to file timestamps, 0.1.0–0.4.0 predate those and are undated.

### [1.8.0] - 2026-10-08
- **Breaking:** the app is now a Python/Tkinter program (`vaultflow_ingest.py`); the PowerShell version is archived, still working, in `archive/powershell/` (tag `powershell-final`). Settings carry over (`vaultflow_config.json` is unchanged). ContactEmail, Website and Telegram settings are gone; the completion popup stays
- Fixed: Insta360/other files ExifTool can't tag were silently not copied (empty vault folders). Video/audio/proxy files are now copied byte-for-byte; stills are tagged, falling back to a plain copy if tagging fails
- Fixed: the "(c) 2026 Jane Doe" placeholder was written into photos as the real copyright when the field was empty; and copyright detection could pick up an ExifTool `========` header line
- Fixed: "Safe to format" only checked file names; verification now checks size, and a Verify button adds SHA-256. Copies are atomic, so no truncated files
- Fixed: the window froze ("Not Responding") for ~15s at startup while an empty card reader was queried; all work now runs off the UI thread
- Day folders use the file's modified date (capture time, local) rather than ExifTool's last-matching-tag rule; shoot detection uses the same time
- Per-run log files are actually written now (the PowerShell version printed a log path but never created the file)

### [1.7.0] - 2026-09-21
- **Breaking:** `01_RAW`'s two problem subfolder names are renamed: `RAW` → `CAM_RAW` (was creating a redundant/confusing `01_RAW\RAW` nested folder) and `AUX` → `MISC` (`AUX` is a reserved Windows device name — `New-Item` can never actually create a folder called that, so every ingest with aux files was silently failing to scaffold that subfolder); existing vault folders are unaffected, new ingests use the corrected names

### [1.6.0] - 2026-08-09
- Versioning stops bumping MAJOR — with a single known user and no compatibility contract to keep, MAJOR bumps for every folder/config-breaking tweak (2.0.0 → 5.0.0 in one day) added ceremony without adding information; those versions are retroactively renumbered below (2.0.0→1.3.0, 3.0.0→1.4.0, 4.0.0→1.5.0) so the changelog reads as one continuous MINOR sequence — see **Versioning** above
- **Location** field restored to the **SESSION** section (dropped in what's now 1.3.0); day-folder naming is now `YYYY-MM-DD_ClientName_EventName_Location` — existing vault folders are unaffected, new ingests append Location as a fourth optional segment

### [1.5.0] - 2026-08-09
- **Breaking:** `01_RAW` is now split into `RAW` / `JPG` / `VIDEO` / `AUDIO` / `AUX` subfolders by media type instead of landing everything in `01_RAW` directly (aux previously nested in `01_RAW\aux`); existing vault folders are unaffected, new ingests use the subfoldered layout
- New **Audio** file-type category (`AudioExt`, default `wav`) alongside RAW/Video/Aux — set under **File Types**, defaults to `wav` if left blank
- Automation scripts moved into `backend/` (`vaultflow_autolaunch.ps1`, `vaultflow_register_autolaunch.ps1`) so `vaultflow_ingest.ps1` at the repo root is unambiguously the file to run
- Added a VaultFlow window icon and in-app logo badge, plus the full logo in this README

### [1.4.0] - 2026-08-09
- **Breaking:** rebranded from Accurova Ingest to **VaultFlow Ingest** — all scripts, config files, UI text, and docs renamed (`accurova_*.ps1` → `vaultflow_*.ps1`, `accurova_config.json` → `vaultflow_config.json`); rename your existing config file to match, or the app will fall back to defaults
- **Breaking:** client folder scaffold reduced from 10 folders to 5 — `01_RAW`, `02_SELECTS`, `03_EDITED`, `04_EXPORTS`, `05_DELIVERED`, replacing `01_RAW`…`10_Archive` (Catalog/Selects/Photoshop/Exports/Social/Prints/Contracts/Deliverables/Archive); existing vault folders are unaffected, but new day folders (and re-scaffolds of existing ones) will only get the new 5 folders

### [1.3.0] - 2026-08-04
- **Breaking:** day-folder naming changed from `YYYY_MM_DD [Event] [Location]` to `YYYY-MM-DD_ClientName_EventName`; the **Location** field was removed in favor of a new **Client Name** field, and ingested files now land in a `01_RAW` subfolder instead of directly in the day folder — existing vault folders from before this change are unaffected, but re-running an ingest for an in-progress job will create a new differently-named/structured folder rather than adding to the old one
- Client folder scaffold — every day folder touched by an ingest now gets the full `01_RAW`…`10_Archive` structure created automatically (Catalog/Selects/Photoshop/Exports/Social/Prints/Contracts/Deliverables/Archive), not just wherever files happen to land (superseded by the 5-folder scaffold in 1.4.0)
- Metadata embedding — new **Metadata / Notifications** UI section for Copyright, Contact Email, and Website, written into every ingested file via ExifTool (`Copyright`/`Source`/`Credit` tags) on every run
- Job Type dropdown (Wedding / Corporate / Event / Portrait / Other) layers extra keywords onto the base metadata per shoot; presets are editable in the `$JobTypeKeywords` hashtable near the top of the script
- Optional Telegram notification on ingest completion (job name, file count, size, duration) — set a bot token + chat ID, leave blank to skip; failures are logged but never abort the ingest
- Replaced the dry-run toggle with two explicit buttons, **START DRY RUN** and **START LIVE INGEST** (gold-trimmed to flag it as the destructive action), instead of a switch plus a single ambiguous run button
- Fixed a rendering bug in the **File Types** row where the Video/Aux extension labels overlapped and clipped each other (a shared label helper always sized boxes at 300px regardless of column width)

### [1.2.0] - 2026-08-04
- Auto-detect now shows the SD card's volume label next to the drive dropdown (e.g. `Auto-detected: D "EOS_SD01" (DCIM found)`), not just the drive letter

### [1.1.0] - 2026-08-04
- Camera-agnostic file typing — replaced the hardcoded NEF/MP4/LRV/INSV extension list with a **FILE TYPES** UI section (RAW / Video / Aux, comma-separated, persisted to config); works for any camera, not just the D850
- Note: the auxiliary-file subfolder is now named `aux` instead of `360`, since aux files aren't always 360 footage — existing vault folders are unaffected, new ingests will create `aux` going forward
- Duplicate detection now confirms name+size matches with an MD5 checksum before treating a file as a true duplicate, so a camera's recycled file counter (e.g. wrapping back to `0001` after ~9999 shots) can't cause a false-positive skip
- Live (non-dry-run) ingest now actually skips confirmed duplicates before invoking ExifTool, instead of copying/overwriting them anyway while only mislabeling the log
- Optional auto-launch on SD card insertion — `accurova_register_autolaunch.ps1` registers a Task Scheduler event trigger that runs `accurova_autolaunch.ps1`, which pops the GUI up automatically when a DCIM-bearing drive appears
- Fixed a crash ("cannot call a method on a null-valued expression") that could occur ~2 seconds after clicking **Save Paths**, caused by a WinForms Timer closure referencing an out-of-scope variable
- Removed the verbose "Output folders" listing from the end-of-run log
- Rebranded away from Nikon D850-specific naming for public release

### [1.0.0] - 2026-07-25
- First tracked release of the public repo (git-backed versioning starts here); code already includes everything through 0.8.0 below

### [0.8.0] - 2026-03-14
- 360 camera file support — `.lrv`/`.insv` ingested as a third file type, nested into a `360` subfolder within the date folder; skipped silently if none found; included in progress tracking, verification, and summary

### [0.7.0] - 2026-03-14
- Two-column layout — form widened to 1140×760px; output log moved to a full-height right panel with a vertical divider

### [0.6.0] - 2026-03-14
- Fixed toggle controls and Browse buttons — WinForms closure scoping bugs caused the eject/dry run toggles and Browse buttons to crash at runtime; toggles inlined with explicit `$script:` scoped state, Browse buttons fixed by storing the textbox reference in `$this.Tag`

### [0.5.0] - 2026-03-10
- In-UI config with JSON persistence — vault destination, log folder, and ExifTool path moved into editable fields with Browse buttons; config saved to `accurova_config.json` next to the script and reloaded on next launch; log folder auto-updates when vault path changes

### [0.4.0] - date unknown
- Progress bar, per-file transfer speed, and ETA calculated from bytes transferred vs. total
- Pre-flight storage check with continue/cancel dialog
- Vault indexed on startup; duplicate files skipped and logged
- SD card auto-detected by scanning for removable drives with a `DCIM` folder
- Dry run toggle to simulate the full pipeline without touching any files
- Post-ingest verification comparing source vs. destination file counts, flagging missing files, confirming safe-to-format

### [0.3.0] - date unknown
- GUI wrapper — replaced terminal interaction with a WinForms dark-themed UI: event name and location fields, SD card drive dropdown, eject checkbox, live output log panel, Stop button, status label

### [0.2.0] - date unknown
- Ported to PowerShell (Windows) — rebuilt entirely for Windows, same logic as the original shell script, PowerShell-native syntax, `.ps1` file

### [0.1.0] - date unknown (predecessor, not in this repo)
- Initial concept as a bash shell script on Mac using ExifTool: sorted NEFs and MP4s by EXIF date, rescued orphan JPGs, logged output, optionally ejected the SD card

## License

This project is dual licensed.

- Community Edition — [GNU Affero General Public License v3 (AGPLv3)](LICENSE). Free to use, modify, and self-host. If you distribute a modified version or run it as a network service, you must make the corresponding source available.
- Commercial License — for organisations that want to embed, modify, or distribute this software without AGPLv3's obligations. See [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md).

---

<div align="center">
<sub>Built by <a href="https://github.com/TheBooleanJulian">@TheBooleanJulian</a></sub>
</div>
