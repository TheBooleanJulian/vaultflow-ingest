"""Tkinter front end. All real work runs on a worker thread and talks back through a
queue, so the window never freezes (the PowerShell version ran everything on the UI thread)."""

from __future__ import annotations

import io
import os
import queue
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:  # optional: only used to show photo thumbnails; everything works without it
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover
    Image = ImageTk = None

from .config import CONFIG_NAME, Config, load_config, save_config
from .exiftool import ExifTool
from .ingest import Hooks, IngestResult, run_ingest, run_verify
from .report import Reporter
from .scan import Shoot, detect_sd_cards, list_drive_letters
from .util import CancelToken, fmt_bytes, fmt_eta, fmt_speed

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / CONFIG_NAME
ASSETS = ROOT_DIR / "assets"

# Palette and fonts carried over unchanged from the PowerShell version.
BG, PANEL = "#0D1117", "#161E28"
TEAL, TEAL_DIM, SECTION_BG = "#00D2BE", "#008C7E", "#001E1C"
FG, FG_DIM = "#DCE6F0", "#648296"
RED, GREEN, YELLOW, ORANGE, GOLD = "#FF5555", "#50DC8C", "#FFC83C", "#FF8C28", "#D4AF37"
DRYRUN_BG = "#3C3200"
LOG_COLORS = {"info": FG, "dim": FG_DIM, "accent": TEAL, "ok": GREEN, "warn": YELLOW, "error": RED}

F_UI = ("Segoe UI", 9)
F_LBL = ("Segoe UI Semibold", 8)
F_SUB = ("Segoe UI", 8)
F_BTN = ("Segoe UI Semibold", 9)
F_LOG = ("Consolas", 9)

THUMB = 76


# -- widgets ---------------------------------------------------------------

class PlaceholderEntry(tk.Entry):
    """Entry that shows grey hint text while empty; get_value() ignores the hint."""

    def __init__(self, master, placeholder: str, **kw) -> None:
        super().__init__(master, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                         highlightthickness=1, highlightbackground=FG_DIM, highlightcolor=TEAL,
                         font=F_UI, **kw)
        self._placeholder = placeholder
        self._showing = False
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self._focus_out()

    def _focus_in(self, _event=None) -> None:
        if self._showing:
            self.delete(0, "end")
            self.config(fg=FG)
            self._showing = False

    def _focus_out(self, _event=None) -> None:
        if not self.get():
            self.insert(0, self._placeholder)
            self.config(fg=FG_DIM)
            self._showing = True

    def get_value(self) -> str:
        return "" if self._showing else self.get().strip()

    def set_value(self, text: str) -> None:
        self._focus_in()
        self.delete(0, "end")
        self.insert(0, text)
        self.config(fg=FG)
        self._showing = False
        if not text:
            self._focus_out()


def make_photo(data: bytes | None, size: int):
    """Tk image from JPEG bytes, or None (no Pillow, no data, or an unreadable thumbnail)."""
    if not (Image and ImageTk and data):
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.thumbnail((size, size))
        return ImageTk.PhotoImage(img)
    except Exception:  # corrupt/odd embedded thumbnails of any format -> placeholder tile
        return None


def draw_tile(canvas: tk.Canvas, size: int, ext: str, photo) -> None:
    canvas.delete("all")
    if photo is not None:
        canvas.photo = photo  # keep a reference or Tk drops the image
        canvas.create_image(size // 2, size // 2, image=photo)
    else:
        canvas.create_rectangle(1, 1, size - 2, size - 2, outline=FG_DIM)
        canvas.create_text(size // 2, size // 2, text=ext.upper(), fill=FG_DIM, font=F_SUB)


def flat_button(master, text, command, fg=FG, border=FG_DIM, bg=PANEL, font=F_BTN, **kw) -> tk.Button:
    return tk.Button(master, text=text, command=command, fg=fg, bg=bg, activeforeground=fg,
                     activebackground=PANEL, relief="flat", bd=0, highlightthickness=1,
                     highlightbackground=border, highlightcolor=border, font=font,
                     cursor="hand2", **kw)


def set_button_state(btn: tk.Button, enabled: bool, fg: str, border: str) -> None:
    """Flat buttons don't grey themselves out, so do it by hand."""
    btn.config(state="normal" if enabled else "disabled",
               fg=fg if enabled else FG_DIM, highlightbackground=border if enabled else FG_DIM,
               highlightcolor=border if enabled else FG_DIM,
               cursor="hand2" if enabled else "arrow", disabledforeground=FG_DIM)


class Toggle(tk.Canvas):
    def __init__(self, master, command=None) -> None:
        super().__init__(master, width=36, height=18, bg=PANEL, highlightthickness=0, cursor="hand2")
        self.on = False
        self._command = command
        self.bind("<Button-1>", lambda _e: self.toggle())
        self._draw()

    def toggle(self) -> None:
        self.on = not self.on
        self._draw()
        if self._command:
            self._command(self.on)

    def _draw(self) -> None:
        self.delete("all")
        self.config(bg=TEAL if self.on else PANEL)
        x = 21 if self.on else 3
        self.create_rectangle(x, 3, x + 12, 15, fill=BG if self.on else FG_DIM, outline="")


# -- dialogs (run on the UI thread) --------------------------------------------

class _Modal(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str) -> None:
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.transient(parent)
        self.result = None
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def show(self):
        self.update_idletasks()
        px_, py_ = self.master.winfo_rootx(), self.master.winfo_rooty()
        self.geometry(f"+{px_ + 80}+{py_ + 60}")
        self.grab_set()
        self.wait_window()
        return self.result


def _load_thumbnails(jobs, exif: ExifTool, inbox: queue.Queue) -> None:
    """Thread body. Deliberately a plain function: a bound method would keep the dialog (and its
    Tk objects) alive in this thread, and Tk objects must be freed on the UI thread."""
    for index, path in jobs:
        inbox.put((index, exif.thumbnail(path)))


class ShootDialog(_Modal):
    """Lists the detected shoots with thumbnails; returns True for separate folders."""

    PER_SHOOT = 12

    def __init__(self, parent, shoots: list[Shoot], exiftool_path: str) -> None:
        super().__init__(parent, "Multiple Shoots Detected")
        self.geometry("900x560")
        self.result = False
        tk.Label(self, text="Multiple shoots detected (gaps over 30 minutes). Create a folder per shoot, "
                            "or merge everything into one day folder?",
                 bg=BG, fg=FG_DIM, font=F_UI, anchor="w").pack(fill="x", padx=12, pady=(12, 6))

        holder = tk.Frame(self, bg=PANEL)
        holder.pack(fill="both", expand=True, padx=12)
        canvas = tk.Canvas(holder, bg=PANEL, highlightthickness=0)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=PANEL)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._tiles: list[tuple[tk.Canvas, str]] = []
        jobs: list[tuple[int, Path]] = []
        for shoot in shoots:
            start = datetime.fromtimestamp(shoot.start).strftime("%Y-%m-%d %H:%M")
            end = datetime.fromtimestamp(shoot.end).strftime("%H:%M")
            box = tk.LabelFrame(inner, text=f" Shoot {shoot.index + 1}  -  {start} to {end}  "
                                            f"({len(shoot.files)} files) ",
                                bg=PANEL, fg=TEAL, font=F_LBL, bd=1, relief="groove")
            box.pack(fill="x", padx=6, pady=5)
            row = tk.Frame(box, bg=BG)
            row.pack(fill="x", padx=6, pady=6)
            for f in shoot.files[: self.PER_SHOOT]:
                tile = tk.Canvas(row, width=THUMB, height=THUMB, bg=PANEL, highlightthickness=0)
                tile.pack(side="left", padx=2)
                draw_tile(tile, THUMB, f.ext, None)
                self._tiles.append((tile, f.ext))
                jobs.append((len(self._tiles) - 1, f.path))

        buttons = tk.Frame(self, bg=BG)
        buttons.pack(fill="x", padx=12, pady=10)
        flat_button(buttons, "Create Separate Folders", self._yes, fg=BG, bg=TEAL, border=TEAL,
                    width=22).pack(side="left")
        flat_button(buttons, "Merge to Single Folder", self.destroy, fg=FG_DIM,
                    width=22).pack(side="left", padx=10)

        self._inbox: queue.Queue = queue.Queue()
        if Image and jobs:  # thumbnails load in the background; the dialog is usable immediately
            threading.Thread(target=_load_thumbnails, args=(jobs, ExifTool(exiftool_path), self._inbox),
                             daemon=True).start()
            self.after(100, self._poll)

    def _yes(self) -> None:
        self.result = True
        self.destroy()

    def _poll(self) -> None:
        try:
            while True:
                index, data = self._inbox.get_nowait()
                tile, ext = self._tiles[index]
                draw_tile(tile, THUMB, ext, make_photo(data, THUMB))
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)


class JobDialog(_Modal):
    """Asks for the folder-name description(s); returns a list of strings or None."""

    def __init__(self, parent, count: int, multiple: bool) -> None:
        super().__init__(parent, "Job Description")
        self.resizable(False, False)
        self._entries: list[tk.Entry] = []
        n = count if multiple else 1
        heading = "Name each shoot:" if multiple else "Job description:"
        tk.Label(self, text=heading, bg=BG, fg=FG_DIM, font=F_LBL).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(18, 6))
        for i in range(n):
            if multiple:
                tk.Label(self, text=f"Shoot {i + 1}:", bg=BG, fg=FG_DIM, font=F_LBL).grid(
                    row=i + 1, column=0, sticky="w", padx=(20, 8), pady=4)
            entry = tk.Entry(self, width=48, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                             highlightthickness=1, highlightbackground=FG_DIM, highlightcolor=TEAL, font=F_UI)
            entry.grid(row=i + 1, column=1 if multiple else 0, columnspan=1 if multiple else 2,
                       sticky="ew", padx=(0 if multiple else 20, 20), pady=4, ipady=3)
            self._entries.append(entry)
        buttons = tk.Frame(self, bg=BG)
        buttons.grid(row=n + 1, column=0, columnspan=2, sticky="e", padx=20, pady=16)
        flat_button(buttons, "Cancel", self.destroy, fg=FG_DIM, width=9).pack(side="right", padx=(8, 0))
        flat_button(buttons, "OK", self._ok, fg=TEAL, border=TEAL, width=9).pack(side="right")
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._entries[0].focus_set()

    def _ok(self) -> None:
        self.result = [e.get().strip() for e in self._entries]
        self.destroy()


# -- worker <-> UI plumbing --------------------------------------------------------

class QueueReporter(Reporter):
    """Reporter whose calls (made on the worker thread) are queued for the UI thread."""

    def __init__(self, q: queue.Queue) -> None:
        self.q = q

    def log(self, msg, level="info"): self.q.put(("log", msg, level))
    def status(self, text): self.q.put(("status", text))
    def current(self, text): self.q.put(("current", text))
    def progress(self, files_done, files_total, percent, speed, eta):
        self.q.put(("progress", files_done, files_total, percent, speed, eta))
    def thumbs_reset(self, extra_count): self.q.put(("thumbs_reset", extra_count))
    def thumb(self, name, ext, data): self.q.put(("thumb", name, ext, data))
    def highlight(self, name): self.q.put(("highlight", name))


class GuiHooks(Hooks):
    """Runs the engine's questions as dialogs on the UI thread and hands back the answer."""

    def __init__(self, app: "App") -> None:
        self.app = app

    def _ask(self, fn):
        reply: queue.Queue = queue.Queue()
        self.app.q.put(("call", fn, reply))
        ok, value = reply.get()
        if not ok:
            raise value
        return value

    def choose_shoot_folders(self, shoots):
        return self._ask(lambda: ShootDialog(self.app.root, shoots, self.app.exiftool_path).show())

    def describe_jobs(self, count, multiple):
        return self._ask(lambda: JobDialog(self.app.root, count, multiple).show())

    def confirm_low_space(self, needed, available):
        return self._ask(lambda: messagebox.askokcancel(
            "VaultFlow - Storage Warning",
            f"Not enough space on the vault drive.\n\nRequired : {fmt_bytes(needed)}\n"
            f"Available: {fmt_bytes(available)}\n\nContinue anyway?", icon="warning"))


# -- main window --------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cfg = load_config(CONFIG_PATH)
        self.q: queue.Queue = queue.Queue()
        self.cancel: CancelToken | None = None
        self.busy = False
        self.dry_run_done = False
        self.vault_ready = False
        self.last_folder: str | None = None
        self.exiftool_path = self.cfg.exiftool
        self._detect_running = False
        self._thumb_cells: dict[str, tk.Frame] = {}
        self._highlighted: str | None = None
        self._thumb_count = 0
        self.worker: threading.Thread | None = None

        root.title("VaultFlow Ingest")
        root.configure(bg=BG)
        root.geometry("1140x1010")
        root.minsize(1000, 960)
        ico = ASSETS / "vaultflow.ico"
        if ico.is_file():
            try:
                root.iconbitmap(str(ico))
            except tk.TclError:
                pass
        self._style()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump_id = root.after(50, self._pump)
        self.detect_sd()

    # ---- styling ----
    def _style(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure("Vault.Horizontal.TProgressbar", troughcolor=PANEL, background=TEAL,
                    bordercolor=BG, lightcolor=TEAL, darkcolor=TEAL, thickness=14)
        thumb = "#2A3644"
        for name in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
            s.configure(name, background=thumb, troughcolor=BG, bordercolor=BG, arrowcolor=FG_DIM,
                        lightcolor=thumb, darkcolor=thumb, gripcount=0)
            s.map(name, background=[("active", TEAL_DIM), ("pressed", TEAL_DIM)],
                  lightcolor=[("active", TEAL_DIM)], darkcolor=[("active", TEAL_DIM)])
        s.configure("Vault.TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG,
                    arrowcolor=FG, bordercolor=FG_DIM, lightcolor=PANEL, darkcolor=PANEL,
                    selectbackground=PANEL, selectforeground=FG)
        s.map("Vault.TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)],
              selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", FG)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", TEAL_DIM)

    # ---- layout helpers ----
    def _section(self, parent, text: str) -> None:
        tk.Label(parent, text=f"  {text}", bg=SECTION_BG, fg=TEAL, font=("Segoe UI Semibold", 8),
                 anchor="w").pack(fill="x", pady=(10, 6))

    def _label(self, parent, text: str) -> tk.Label:
        return tk.Label(parent, text=text, bg=BG, fg=FG_DIM, font=F_LBL, anchor="w")

    def _entry(self, parent, value: str = "") -> tk.Entry:
        e = tk.Entry(parent, bg=PANEL, fg=FG, insertbackground=FG, relief="flat", font=F_UI,
                     highlightthickness=1, highlightbackground=FG_DIM, highlightcolor=TEAL)
        e.insert(0, value)
        return e

    def _path_row(self, parent, label: str, value: str, pick) -> tk.Entry:
        self._label(parent, label).pack(fill="x", pady=(4, 0))
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(2, 0))
        entry = self._entry(row, value)
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        flat_button(row, "Browse", lambda: pick(entry), fg=FG_DIM, font=F_SUB, width=9).pack(side="left", padx=(6, 0))
        return entry

    def _build(self) -> None:
        # header
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=24, pady=(14, 0))
        logo_path = ASSETS / "vaultflow-icon.png"
        self._logo = None
        if logo_path.is_file():
            try:
                img = tk.PhotoImage(file=str(logo_path))
                self._logo = img.subsample(max(1, img.width() // 44))
                tk.Label(head, image=self._logo, bg=BG).pack(side="left", padx=(0, 10))
            except tk.TclError:
                pass
        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left")
        tk.Label(titles, text="VAULTFLOW", bg=BG, fg=TEAL, font=("Segoe UI Light", 18)).pack(anchor="w")
        tk.Label(titles, text="SD CARD INGEST UTILITY", bg=BG, fg=FG_DIM, font=F_SUB).pack(anchor="w")
        tk.Frame(self.root, bg=TEAL, height=1).pack(fill="x", padx=24, pady=(8, 0))

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 10))
        left = tk.Frame(body, bg=BG, width=640)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Frame(body, bg=PANEL, width=1).pack(side="left", fill="y", padx=14)
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self._build_left(left)
        self._build_right(right)

    def _build_left(self, p: tk.Frame) -> None:
        self._section(p, "PATHS")
        self.e_dest = self._path_row(p, "VAULT DESTINATION", self.cfg.dest, self._pick_dir)
        self.e_logs = self._path_row(p, "LOG FOLDER", self.cfg.log_dir, self._pick_dir)
        self.e_exif = self._path_row(p, "EXIFTOOL PATH", self.cfg.exiftool, self._pick_file)
        row = tk.Frame(p, bg=BG)
        row.pack(fill="x", pady=(8, 0))
        flat_button(row, "Save Paths", self._save, fg=TEAL, border=TEAL, font=F_SUB, width=12).pack(side="left")
        self.l_saved = tk.Label(row, text="", bg=BG, fg=GREEN, font=F_SUB)
        self.l_saved.pack(side="left", padx=10)

        self._section(p, "FILE TYPES")
        grid = tk.Frame(p, bg=BG)
        grid.pack(fill="x")
        self.type_entries: dict[str, tk.Entry] = {}
        for col, (key, label, value) in enumerate((
                ("raw", "RAW EXTENSIONS", self.cfg.raw_ext), ("video", "VIDEO EXTENSIONS", self.cfg.video_ext),
                ("audio", "AUDIO EXTENSIONS", self.cfg.audio_ext), ("aux", "MISC EXT. (optional)", self.cfg.aux_ext))):
            cell = tk.Frame(grid, bg=BG)
            cell.grid(row=0, column=col, sticky="ew", padx=(0, 8 if col < 3 else 0))
            grid.columnconfigure(col, weight=1, uniform="types")
            self._label(cell, label).pack(fill="x")
            if key == "aux":
                entry = PlaceholderEntry(cell, "e.g. lrv, insv")
                if value:
                    entry.set_value(value)
            else:
                entry = self._entry(cell, value)
            entry.pack(fill="x", ipady=3)
            self.type_entries[key] = entry
        tk.Label(p, text="Comma-separated, no dots. MISC = proxy/360 footage, e.g. GoPro/Insta360 .lrv/.insv",
                 bg=BG, fg=FG_DIM, font=F_SUB, anchor="w").pack(fill="x", pady=(6, 0))

        self._section(p, "METADATA")
        self._label(p, "COPYRIGHT").pack(fill="x")
        self.e_copyright = PlaceholderEntry(p, "(c) 2026 Jane Doe")
        if self.cfg.copyright:
            self.e_copyright.set_value(self.cfg.copyright)
        self.e_copyright.pack(fill="x", ipady=3)
        tk.Label(p, text="Written into RAW/JPG stills via ExifTool (video is copied untouched). "
                         "Auto-detected from the card on a dry run if blank.",
                 bg=BG, fg=FG_DIM, font=F_SUB, anchor="w", wraplength=620, justify="left").pack(fill="x", pady=(6, 0))

        self._section(p, "SESSION")
        self._label(p, "SD CARD DRIVE").pack(fill="x")
        row = tk.Frame(p, bg=BG)
        row.pack(fill="x", pady=(2, 0))
        self.cmb_drive = ttk.Combobox(row, state="readonly", width=6, style="Vault.TCombobox",
                                      values=list_drive_letters(), postcommand=self._on_dropdown, font=F_UI)
        self.cmb_drive.pack(side="left")
        letters = list_drive_letters()
        self.cmb_drive.set("D:\\" if "D:\\" in letters else (letters[0] if letters else ""))
        self.l_detect = tk.Label(row, text="Detecting SD card...", bg=BG, fg=FG_DIM, font=F_SUB)
        self.l_detect.pack(side="left", padx=12)
        row = tk.Frame(p, bg=BG)
        row.pack(fill="x", pady=(10, 0))
        self.toggle_eject = Toggle(row, self._eject_changed)
        self.toggle_eject.pack(side="left")
        self.l_eject = tk.Label(row, text="Eject SD card after ingest", bg=BG, fg=FG_DIM, font=F_SUB, cursor="hand2")
        self.l_eject.pack(side="left", padx=8)
        self.l_eject.bind("<Button-1>", lambda _e: self.toggle_eject.toggle())

        self._section(p, "PREVIEW  (files queued for copy)")
        strip = tk.Frame(p, bg=PANEL, height=128)
        strip.pack(fill="x")
        strip.pack_propagate(False)
        self.cv_strip = tk.Canvas(strip, bg=PANEL, highlightthickness=0, height=108)
        hscroll = ttk.Scrollbar(strip, orient="horizontal", command=self.cv_strip.xview)
        self.cv_strip.configure(xscrollcommand=hscroll.set)
        self.cv_strip.pack(side="top", fill="both", expand=True)
        hscroll.pack(side="bottom", fill="x")
        self.strip_inner = tk.Frame(self.cv_strip, bg=PANEL)
        self.cv_strip.create_window((0, 0), window=self.strip_inner, anchor="nw")
        self.strip_inner.bind("<Configure>", lambda _e: self.cv_strip.configure(scrollregion=self.cv_strip.bbox("all")))
        self.l_hint = tk.Label(self.strip_inner, text="Populated when a dry run or live ingest starts.",
                               bg=PANEL, fg=FG_DIM, font=F_SUB)
        self.l_hint.grid(row=0, column=0, padx=10, pady=10)
        tk.Label(p, text="Files are automatically organized into: CAM_RAW / JPG / VIDEO / AUDIO / MISC subfolders",
                 bg=BG, fg=TEAL, font=F_SUB, anchor="w").pack(fill="x", pady=(6, 0))
        self.banner = tk.Label(p, text="  DRY RUN ACTIVE - No files will be copied or moved", bg=DRYRUN_BG,
                               fg=YELLOW, font=F_LBL, anchor="w")

        # buttons
        self._div_buttons = tk.Frame(p, bg=PANEL, height=1)
        self._div_buttons.pack(fill="x", pady=(10, 8))
        row = tk.Frame(p, bg=BG)
        row.pack(fill="x")
        self.b_dry = flat_button(row, "START DRY RUN", lambda: self._start("dry"), width=17)
        self.b_dry.pack(side="left", ipady=6)
        self.b_live = flat_button(row, "START LIVE INGEST", lambda: self._start("live"), fg=BG, bg=TEAL,
                                  border=GOLD, width=17)
        self.b_live.pack(side="left", padx=8, ipady=6)
        self.b_stop = flat_button(row, "STOP", self._stop, fg=FG, bg=RED, border=RED, width=8)
        self.b_stop.pack(side="left", ipady=6)
        self.l_status = tk.Label(row, text="Ready. Run a dry run first.", bg=BG, fg=FG_DIM, font=F_SUB,
                                 anchor="w")
        self.l_status.pack(side="left", padx=12, fill="x", expand=True)
        row = tk.Frame(p, bg=BG)
        row.pack(fill="x", pady=(8, 0))
        self.b_open = flat_button(row, "OPEN VAULT FOLDER", self._open_vault, width=34)
        self.b_open.pack(side="left", ipady=4)
        self.b_verify = flat_button(row, "VERIFY CARD vs VAULT", lambda: self._start("verify"), fg=GOLD,
                                    border=GOLD, width=34)
        self.b_verify.pack(side="left", padx=8, ipady=4)

        # progress
        tk.Frame(p, bg=PANEL, height=1).pack(fill="x", pady=(10, 8))
        self.bar = ttk.Progressbar(p, style="Vault.Horizontal.TProgressbar", maximum=100)
        self.bar.pack(fill="x")
        row = tk.Frame(p, bg=BG)
        row.pack(fill="x", pady=(4, 0))
        self.l_pct = tk.Label(row, text="0%", bg=BG, fg=TEAL, font=F_BTN, width=5, anchor="w")
        self.l_pct.pack(side="left")
        self.l_files = tk.Label(row, text="0 / 0 files", bg=BG, fg=FG_DIM, font=F_SUB, width=16, anchor="w")
        self.l_files.pack(side="left")
        self.l_speed = tk.Label(row, text="", bg=BG, fg=FG_DIM, font=F_SUB, width=14, anchor="w")
        self.l_speed.pack(side="left")
        self.l_eta = tk.Label(row, text="", bg=BG, fg=FG_DIM, font=F_SUB, anchor="w")
        self.l_eta.pack(side="left")
        self.l_current = tk.Label(p, text="", bg=BG, fg=FG_DIM, font=F_SUB, anchor="w")
        self.l_current.pack(fill="x")

        self._refresh_buttons()

    def _build_right(self, p: tk.Frame) -> None:
        tk.Label(p, text="  OUTPUT LOG", bg=SECTION_BG, fg=TEAL, font=("Segoe UI Semibold", 8),
                 anchor="w").pack(fill="x", pady=(10, 6))
        frame = tk.Frame(p, bg=PANEL)
        frame.pack(fill="both", expand=True)
        self.log = tk.Text(frame, bg=PANEL, fg=FG_DIM, font=F_LOG, relief="flat", bd=0, wrap="word",
                           state="disabled", highlightthickness=0, padx=6, pady=4)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for level, color in LOG_COLORS.items():
            self.log.tag_configure(level, foreground=color)

    # ---- small UI actions ----
    def _pick_dir(self, entry: tk.Entry) -> None:
        chosen = filedialog.askdirectory(initialdir=entry.get() or None, mustexist=False)
        if chosen:
            entry.delete(0, "end")
            entry.insert(0, os.path.normpath(chosen))

    def _pick_file(self, entry: tk.Entry) -> None:
        chosen = filedialog.askopenfilename(title="Select exiftool.exe", filetypes=[("exiftool", "*.exe"), ("All", "*.*")])
        if chosen:
            entry.delete(0, "end")
            entry.insert(0, os.path.normpath(chosen))

    def _gather(self) -> Config:
        return Config(
            dest=self.e_dest.get().strip(), log_dir=self.e_logs.get().strip(),
            exiftool=self.e_exif.get().strip(),
            raw_ext=self.type_entries["raw"].get().strip(), video_ext=self.type_entries["video"].get().strip(),
            audio_ext=self.type_entries["audio"].get().strip(), aux_ext=self.type_entries["aux"].get_value(),
            copyright=self.e_copyright.get_value())

    def _save(self) -> None:
        try:
            save_config(CONFIG_PATH, self._gather())
            self.l_saved.config(text="Saved.", fg=GREEN)
        except OSError as exc:
            self.l_saved.config(text=f"Could not save: {exc}", fg=RED)
        self.root.after(2500, lambda: self.l_saved.config(text=""))

    def _eject_changed(self, on: bool) -> None:
        self.l_eject.config(fg=FG if on else FG_DIM)

    def _open_vault(self) -> None:
        if self.last_folder and os.path.isdir(self.last_folder):
            os.startfile(self.last_folder)  # Windows only, like the rest of VaultFlow

    def _on_close(self) -> None:
        if self.cancel:
            self.cancel.cancel()
        self.root.after_cancel(self._pump_id)
        self.root.destroy()

    def _refresh_buttons(self) -> None:
        set_button_state(self.b_dry, not self.busy, FG, FG_DIM)
        set_button_state(self.b_live, not self.busy and self.dry_run_done, BG, GOLD)
        self.b_live.config(bg=TEAL if (not self.busy and self.dry_run_done) else PANEL)
        set_button_state(self.b_verify, not self.busy, GOLD, GOLD)
        set_button_state(self.b_open, not self.busy and self.vault_ready, TEAL, TEAL)
        set_button_state(self.b_stop, self.busy, FG, RED)
        self.b_stop.config(bg=RED if self.busy else PANEL)

    # ---- SD card detection (background thread) ----
    def _on_dropdown(self) -> None:
        self.cmb_drive.config(values=list_drive_letters())
        self.detect_sd()

    def detect_sd(self) -> None:
        if self._detect_running:
            return
        self._detect_running = True
        self.l_detect.config(text="Detecting SD card...", fg=FG_DIM)

        q = self.q
        threading.Thread(target=lambda: q.put(("sd", detect_sd_cards())), daemon=True).start()

    def _sd_found(self, cards: list[tuple[str, str]]) -> None:
        self._detect_running = False
        if cards:
            root, label = cards[0]
            self.cmb_drive.set(root)
            self.l_detect.config(text=f'Auto-detected: {root.rstrip(chr(92))} "{label}" (DCIM found)', fg=GREEN)
        else:
            self.l_detect.config(text="No SD card detected - select manually", fg=YELLOW)

    # ---- running jobs ----
    def _start(self, kind: str) -> None:
        if self.busy:
            return
        drive = self.cmb_drive.get()
        if not drive:
            messagebox.showerror("VaultFlow", "Select the SD card drive first.")
            return
        cfg = self._gather()
        self.exiftool_path = cfg.exiftool
        self.busy = True
        self.cancel = CancelToken()
        self._refresh_buttons()
        self._clear_log()
        self.bar["value"] = 0
        self.l_pct.config(text="0%")
        self.l_files.config(text="")
        self.l_speed.config(text="")
        self.l_eta.config(text="")
        self.l_current.config(text="")
        self.l_status.config(text="Starting...", fg=YELLOW)
        if kind == "dry":
            self.banner.pack(fill="x", pady=(0, 6), before=self._div_buttons)
        eject = self.toggle_eject.on
        cancel = self.cancel

        def work() -> None:
            rep = QueueReporter(self.q)
            try:
                if kind == "verify":
                    result = run_verify(cfg, drive, rep, cancel)
                    self.q.put(("done", lambda: self._verify_done(result, cancel)))
                else:
                    result = run_ingest(cfg, drive, kind == "dry", rep, GuiHooks(self), cancel, eject)
                    self.q.put(("done", lambda: self._ingest_done(result, cfg)))
            except Exception:  # last resort: show it in the log instead of dying silently
                rep.log("[ERROR] Unexpected error:\n" + traceback.format_exc(), "error")
                self.q.put(("done", lambda: self._finish("Failed - see log.", RED)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        if self.cancel:
            self.cancel.cancel()
            self.l_status.config(text="Stopping...", fg=RED)

    def _finish(self, status: str, color: str) -> None:
        self.busy = False
        self.banner.pack_forget()
        self._refresh_buttons()
        self.l_speed.config(text="")
        self.l_current.config(text="")
        self.l_status.config(text=status, fg=color)

    def _ingest_done(self, result: IngestResult, cfg: Config) -> None:
        elapsed = f"{int(result.elapsed // 60)}m {int(result.elapsed % 60)}s"
        if result.cancelled or result.aborted:
            self._finish("Stopped." if result.cancelled else "Not run - see log.", RED if result.aborted or result.cancelled else FG_DIM)
            return
        if result.dry_run:
            self.dry_run_done = True
            if result.detected_copyright and not self.e_copyright.get_value():
                self.e_copyright.set_value(result.detected_copyright)
                self._append(f"[INFO]  Copyright field filled from the card: {result.detected_copyright}\n", "accent")
            self._finish(f"Dry run complete  {elapsed}", GREEN)
            self.l_eta.config(text=f"Done in {elapsed}")
            return
        self.vault_ready = True
        self.last_folder = cfg.dest
        speed = fmt_speed(result.bytes_total / result.elapsed) if result.elapsed > 0 else "n/a"
        self.l_eta.config(text=f"Done in {elapsed}")
        self._finish(f"{'Done!' if result.ok else 'Finished with problems'}  {elapsed}  |  avg {speed}",
                     GREEN if result.ok else RED)
        if os.path.isdir(cfg.dest):
            try:
                os.startfile(cfg.dest)
            except OSError:
                pass
        if result.ok:
            messagebox.showinfo(
                "VaultFlow Ingest",
                f"Ingest complete!\n\n{result.copied} files copied ({fmt_bytes(result.bytes_total)}) in {elapsed}.\n"
                f"{result.duplicates} duplicate(s) skipped.\n\nVault folder: {cfg.dest}")
        else:
            problems = len(result.failed) + (result.verify.problems if result.verify else 0)
            messagebox.showwarning(
                "VaultFlow Ingest - problems",
                f"Ingest finished with problems ({problems} file(s) failed or are incomplete).\n\n"
                f"Do NOT format the SD card. See the output log for details.\n\nVault folder: {cfg.dest}")

    def _verify_done(self, result, cancel: CancelToken) -> None:
        if result is None:
            self._finish("Stopped." if cancel.cancelled else "Not verified - see log.", RED)
        elif result.ok:
            self._finish("Verified OK.", GREEN)
        else:
            self._finish(f"{result.problems} file(s) failed.", RED)

    # ---- UI-thread message pump ----
    def _pump(self) -> None:
        try:
            for _ in range(300):
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self._pump_id = self.root.after(40, self._pump)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "log":
            self._append(msg[1] + "\n", msg[2])
        elif kind == "status":
            self.l_status.config(text=msg[1], fg=YELLOW)
        elif kind == "current":
            self.l_current.config(text=msg[1])
        elif kind == "progress":
            _, done, total, percent, speed, eta = msg
            self.bar["value"] = percent
            self.l_pct.config(text=f"{percent}%")
            self.l_files.config(text=f"{done} / {total} files")
            self.l_speed.config(text=fmt_speed(speed) if speed else "")
            self.l_eta.config(text=f"ETA  {fmt_eta(eta)}" if speed else "")
        elif kind == "thumbs_reset":
            self._thumbs_reset(msg[1])
        elif kind == "thumb":
            self._add_thumb(msg[1], msg[2], msg[3])
        elif kind == "highlight":
            self._highlight(msg[1])
        elif kind == "sd":
            self._sd_found(msg[1])
        elif kind == "call":
            _, fn, reply = msg
            try:
                reply.put((True, fn()))
            except Exception as exc:  # hand a dialog failure back to the worker
                reply.put((False, exc))
        elif kind == "done":
            msg[1]()

    # ---- log & preview strip ----
    def _append(self, text: str, level: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text, level if level in LOG_COLORS else "info")
        self.log.config(state="disabled")
        self.log.see("end")

    def _clear_log(self) -> None:
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def _thumbs_reset(self, extra: int) -> None:
        for child in self.strip_inner.winfo_children():
            child.destroy()
        self._thumb_cells = {}
        self._highlighted = None
        self._thumb_count = 0
        if extra > 0:
            tk.Label(self.strip_inner, text=f"+{extra} more", bg=PANEL, fg=FG_DIM, font=F_SUB, width=10).grid(
                row=0, column=100000, padx=6)

    def _add_thumb(self, name: str, ext: str, data: bytes | None) -> None:
        cell = tk.Frame(self.strip_inner, bg=BG, width=THUMB + 8, height=108)
        cell.grid(row=0, column=self._thumb_count, padx=2, pady=2)
        cell.grid_propagate(False)
        tile = tk.Canvas(cell, width=THUMB, height=THUMB, bg=PANEL, highlightthickness=0)
        tile.place(x=4, y=4)
        draw_tile(tile, THUMB, ext, make_photo(data, THUMB))
        tk.Label(cell, text=name, bg=BG, fg=FG_DIM, font=F_SUB, wraplength=THUMB + 4, justify="center").place(
            x=2, y=THUMB + 6)
        self._thumb_cells[name] = cell
        self._thumb_count += 1

    def _highlight(self, name: str) -> None:
        if self._highlighted in self._thumb_cells:
            self._thumb_cells[self._highlighted].config(bg=BG)
        cell = self._thumb_cells.get(name)
        if cell is not None:
            cell.config(bg=TEAL_DIM)
            self._highlighted = name


def main() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text instead of a blurry upscale
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    try:
        App(root)
    except Exception:
        messagebox.showerror("VaultFlow - startup error", traceback.format_exc())
        return
    root.mainloop()
