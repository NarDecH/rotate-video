"""app.py - Rotate Video (Thai GUI + CLI).

GUI : python app.py [files...]
Test: python app.py --selftest    (writes %TEMP%\\RotateVideo-selftest.log)
CLI : RotateVideo.exe in.mp4 --rotate cw90 --mode copy [--out DIR] [--gpu]
"""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import rotate_core as core

APP_NAME = "Rotate Video"
APP_VERSION = "2.1.0"


def resource(rel: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel


def _say(text: str = "") -> None:
    try:
        print(text, flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Theme helpers
# --------------------------------------------------------------------------- #

def _pick_font(root: tk.Misc) -> str:
    try:
        families = set(tkfont.families(root))
    except Exception:
        return "Segoe UI"
    for name in ("Leelawadee UI", "Segoe UI", "Tahoma"):
        if name in families:
            return name
    return "Segoe UI"


def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class RotateApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.family = _pick_font(root)
        settings = core.load_settings()

        self.action = tk.StringVar(
            value=settings.get("action") if settings.get("action") in core.ACTIONS else "cw90")
        self.mode = tk.StringVar(
            value=settings.get("mode") if settings.get("mode") in core.MODES else "copy")
        out_dir = settings.get("out_dir") or ""
        if out_dir and not Path(out_dir).is_dir():
            out_dir = ""
        self.out_dir = tk.StringVar(value=out_dir)
        self.gpu = tk.BooleanVar(value=bool(settings.get("gpu", False)))

        self.files: list[Path] = []
        self.infos: dict[Path, core.MediaInfo] = {}
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_output: Path | None = None
        self.gpu_available = False
        self.file_started = 0.0
        self.total_files = 0
        self.done_files = 0
        self.successes = 0

        self._style()
        self._build()
        self._update_hints()
        self._refresh_summary()
        self._bind_shortcuts()
        self._hook_drop()
        self._center()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll)
        threading.Thread(target=self._probe_gpu, daemon=True).start()

    # -- look & feel ------------------------------------------------------- #

    def _style(self) -> None:
        self.root.title(f"{APP_NAME} — หมุนวิดีโอไม่เสียคุณภาพ")
        self.root.minsize(720, 560)
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        base = tkfont.nametofont("TkDefaultFont")
        base.configure(family=self.family, size=10)

        icon = resource("assets/app.ico")
        if icon.exists():
            try:
                self.root.iconbitmap(default=str(icon))
            except Exception:
                pass

        style.configure("TLabelframe.Label", font=(self.family, 10, "bold"))
        style.configure("Hint.TLabel", foreground="#6b7280", font=(self.family, 9))
        style.configure("Warn.TLabel", foreground="#b45309", font=(self.family, 9))
        style.configure("Info.TLabel", foreground="#374151", font=(self.family, 9))
        style.configure("Status.TLabel", foreground="#111827", font=(self.family, 9, "bold"))
        style.configure("Treeview", font=(self.family, 9), rowheight=22)
        style.configure("Treeview.Heading", font=(self.family, 9, "bold"))

    def _center(self) -> None:
        """Size to the real content (the canvas does not report it by itself)."""
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        content_h = (self.header.winfo_reqheight()
                     + self.body.winfo_reqheight() + 22)
        content_w = self.body.winfo_reqwidth() + 44
        w = max(740, min(content_w, screen_w - 40))
        h = min(content_h, screen_h - 80)
        x = max(0, (screen_w - w) // 2)
        y = max(0, (screen_h - h) // 2 - 16)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # -- layout ------------------------------------------------------------ #

    def _build(self) -> None:
        root = self.root

        header = self.header = tk.Frame(root, bg="#0f172a")
        header.pack(fill="x")
        tk.Label(header, text="หมุนวิดีโอ", bg="#0f172a", fg="#ffffff",
                 font=(self.family, 18, "bold")).pack(anchor="w", padx=20, pady=(9, 0))
        tk.Label(header,
                 text="ไม่สูญเสียคุณภาพ • ฝัง FFmpeg ในตัว • ใช้ได้เลยไม่ต้องติดตั้งอะไรเพิ่ม",
                 bg="#0f172a", fg="#94a3b8",
                 font=(self.family, 9)).pack(anchor="w", padx=20, pady=(1, 9))
        tk.Label(header, text=f"v{APP_VERSION}", bg="#0f172a", fg="#64748b",
                 font=(self.family, 8)).place(relx=1.0, y=10, anchor="ne", x=-16)

        wrap = ttk.Frame(root)
        wrap.pack(fill="both", expand=True)
        bg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
        self.canvas = tk.Canvas(wrap, highlightthickness=0, borderwidth=0, background=bg)
        self.vbar = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")

        body = self.body = ttk.Frame(self.canvas, padding=(18, 9, 18, 14))
        self.body_id = self.canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.root.bind_all("<MouseWheel>", self._on_wheel)
        body.columnconfigure(0, weight=1)

        # --- files --------------------------------------------------------
        box_file = ttk.LabelFrame(body, text=" 1. ไฟล์วิดีโอ ", padding=8)
        box_file.grid(row=0, column=0, sticky="ew")
        box_file.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(box_file, columns=("name", "meta"), show="headings",
                                 height=3, selectmode="extended")
        self.tree.heading("name", text="ไฟล์")
        self.tree.heading("meta", text="ข้อมูล")
        self.tree.column("name", width=300, anchor="w")
        self.tree.column("meta", width=380, anchor="w")
        self.tree.grid(row=0, column=0, sticky="ew")
        bar = ttk.Scrollbar(box_file, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        bar.grid(row=0, column=1, sticky="ns")

        tools = ttk.Frame(box_file)
        tools.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(tools, text="เพิ่มไฟล์…", command=self.pick_files).pack(side="left")
        ttk.Button(tools, text="ลบที่เลือก", command=self.remove_selected).pack(
            side="left", padx=(6, 0))
        ttk.Button(tools, text="ล้างทั้งหมด", command=self.clear_files).pack(
            side="left", padx=(6, 0))

        self.summary_var = tk.StringVar(value="ลากไฟล์วิดีโอมาวางในหน้าต่างนี้ "
                                             "หรือกด “เพิ่มไฟล์…” (เลือกได้หลายไฟล์)")
        ttk.Label(box_file, textvariable=self.summary_var, style="Info.TLabel",
                  wraplength=640, justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # --- direction ----------------------------------------------------
        box_dir = ttk.LabelFrame(body, text=" 2. ทิศทาง ", padding=8)
        box_dir.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for i in range(3):
            box_dir.columnconfigure(i, weight=1)

        dirs = [
            ("cw90", "หมุนขวา 90°"),
            ("ccw90", "หมุนซ้าย 90°"),
            ("rot180", "หมุน 180°"),
            ("fliph", "พลิกซ้าย–ขวา"),
            ("flipv", "พลิกบน–ล่าง"),
        ]
        for idx, (key, label) in enumerate(dirs):
            ttk.Radiobutton(box_dir, text=label, value=key, variable=self.action,
                            command=self._on_action).grid(
                row=idx // 3, column=idx % 3, sticky="w", pady=1)

        # --- quality ------------------------------------------------------
        box_q = ttk.LabelFrame(body, text=" 3. คุณภาพ ", padding=8)
        box_q.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        box_q.columnconfigure(0, weight=1)

        self.copy_radio = ttk.Radiobutton(
            box_q, value="copy", variable=self.mode, command=self._on_mode,
            text="ไม่เข้ารหัสใหม่ — ภาพเดิม 100% (แนะนำ)")
        self.copy_radio.grid(row=0, column=0, sticky="w", pady=1)
        ttk.Radiobutton(box_q, value="crf", variable=self.mode, command=self._on_mode,
                        text="ฝังการหมุนจริง — คุณภาพสูง (CRF 16)").grid(
            row=1, column=0, sticky="w", pady=1)
        ttk.Radiobutton(box_q, value="lossless", variable=self.mode, command=self._on_mode,
                        text="ฝังการหมุนจริง — Lossless (pixel-exact)").grid(
            row=2, column=0, sticky="w", pady=1)

        self.gpu_chk = ttk.Checkbutton(
            box_q, text="ใช้ GPU (เร็วขึ้น) — เฉพาะโหมด CRF", variable=self.gpu,
            command=self._on_mode, state="disabled")
        self.gpu_chk.grid(row=3, column=0, sticky="w", pady=(4, 0))

        self.mode_hint = ttk.Label(box_q, style="Hint.TLabel", wraplength=640,
                                   justify="left")
        self.mode_hint.grid(row=4, column=0, sticky="w", padx=(22, 0), pady=(4, 0))

        # --- output -------------------------------------------------------
        box_out = ttk.LabelFrame(body, text=" 4. ปลายทาง ", padding=8)
        box_out.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        box_out.columnconfigure(0, weight=1)

        ttk.Entry(box_out, textvariable=self.out_dir).grid(
            row=0, column=0, sticky="ew", padx=(0, 8), ipady=3)
        ttk.Button(box_out, text="เลือกโฟลเดอร์…", command=self.pick_out_dir).grid(
            row=0, column=1)

        self.out_hint = ttk.Label(box_out, style="Hint.TLabel",
                                  text="เว้นว่างไว้ = บันทึกโฟลเดอร์เดียวกับไฟล์ต้นฉบับ "
                                       "(ไม่ทับไฟล์เดิม)")
        self.out_hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # --- action -------------------------------------------------------
        action_row = ttk.Frame(body)
        action_row.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        action_row.columnconfigure(0, weight=1)

        self.start_btn = tk.Button(
            action_row, text="หมุนวิดีโอ", command=self.start,
            bg="#2563eb", fg="white", activebackground="#1d4ed8",
            activeforeground="white", relief="flat", bd=0, cursor="hand2",
            font=(self.family, 12, "bold"), pady=8)
        self.start_btn.grid(row=0, column=0, sticky="ew")
        self.cancel_btn = ttk.Button(action_row, text="ยกเลิก", command=self.cancel,
                                     state="disabled", width=8)
        self.cancel_btn.grid(row=0, column=1, padx=(8, 0))

        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.grid(row=5, column=0, sticky="ew", pady=(9, 0))

        # --- status + log -------------------------------------------------
        status_row = ttk.Frame(body)
        status_row.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        status_row.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="พร้อมใช้งาน")
        ttk.Label(status_row, textvariable=self.status_var, style="Status.TLabel").grid(
            row=0, column=0, sticky="w")
        self.open_file_btn = ttk.Button(status_row, text="เปิดไฟล์ผลลัพธ์",
                                        command=self.open_output_file, state="disabled")
        self.open_file_btn.grid(row=0, column=1, padx=(8, 0), sticky="e")
        self.open_btn = ttk.Button(status_row, text="เปิดโฟลเดอร์",
                                   command=self.open_output_folder, state="disabled")
        self.open_btn.grid(row=0, column=2, padx=(6, 0), sticky="e")

        self.log = tk.Text(body, height=4, bg="#0b1220", fg="#cbd5e1", relief="flat",
                           font=("Consolas", 8), wrap="none", padx=8, pady=5)
        self.log.grid(row=7, column=0, sticky="nsew", pady=(6, 0))
        body.rowconfigure(7, weight=1)
        self.log.configure(state="disabled")

    # -- scrolling --------------------------------------------------------- #

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self.body_id, width=event.width)
        self.root.after_idle(self._sync_scrollbar)

    def _sync_scrollbar(self) -> None:
        """Show the scrollbar exactly when the content is taller than the view."""
        bbox = self.canvas.bbox("all")
        need = bool(bbox) and bbox[3] > self.canvas.winfo_height() + 1
        if need and not self.vbar.winfo_ismapped():
            self.vbar.pack(side="right", fill="y")
        elif not need and self.vbar.winfo_ismapped():
            self.vbar.pack_forget()

    def _on_scroll(self, first: str, last: str) -> None:
        if float(first) <= 0.0 and float(last) >= 1.0:
            if self.vbar.winfo_ismapped():
                self.vbar.pack_forget()
        elif not self.vbar.winfo_ismapped():
            self.vbar.pack(side="right", fill="y")

    def _on_wheel(self, event) -> None:
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if widget is not None:
            name = str(widget)
            if widget is self.log or name.startswith(str(self.tree)):
                return
        bbox = self.canvas.bbox("all")
        if bbox and bbox[3] > self.canvas.winfo_height():
            self.canvas.yview_scroll(-1 * int(event.delta / 120), "units")

    # -- shortcuts & drag & drop ------------------------------------------- #

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Return>", lambda _e: self.start())
        self.root.bind("<Control-o>", lambda _e: self.pick_files())

    def _hook_drop(self) -> None:
        try:
            import windnd  # type: ignore

            hwnd = int(self.root.wm_frame(), 16)
            windnd.hook_dropfiles(hwnd, func=self._on_drop)
        except Exception:
            pass  # drag & drop is a bonus; the buttons always work

    def _on_drop(self, files) -> None:
        paths = []
        for raw in files:
            path = Path(os.fsdecode(raw))
            if path.suffix.lower() in core.KNOWN_EXTENSIONS:
                paths.append(path)
        if paths:
            self.root.after(0, self.add_files, paths)

    # -- file list --------------------------------------------------------- #

    def pick_files(self) -> None:
        types = [("ไฟล์วิดีโอ", " ".join(f"*{e}" for e in sorted(core.KNOWN_EXTENSIONS))),
                 ("ทุกไฟล์", "*.*")]
        names = filedialog.askopenfilenames(title="เลือกไฟล์วิดีโอ", filetypes=types)
        if names:
            self.add_files([Path(n) for n in names])

    def pick_out_dir(self) -> None:
        folder = filedialog.askdirectory(title="เลือกโฟลเดอร์ปลายทาง")
        if folder:
            self.out_dir.set(folder)
            self._update_hints()
            self._refresh_summary()

    def add_files(self, paths) -> None:
        added = 0
        for path in paths:
            path = Path(path)
            if path in self.files or not path.exists():
                continue
            if path.suffix.lower() not in core.KNOWN_EXTENSIONS:
                continue
            self.files.append(path)
            try:
                self.infos[path] = core.probe(path)
            except Exception:
                self.infos[path] = core.MediaInfo()
            self.tree.insert("", "end", iid=str(path),
                             values=(path.name, self._row_text(path)))
            added += 1
        if added:
            self._update_hints()
            self._refresh_summary()
            self.status_var.set(f"เพิ่ม {added} ไฟล์แล้ว — พร้อมใช้งาน")

    def remove_selected(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        for iid in self.tree.selection():
            path = Path(iid)
            self.tree.delete(iid)
            if path in self.files:
                self.files.remove(path)
            self.infos.pop(path, None)
        self._update_hints()
        self._refresh_summary()

    def clear_files(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.tree.delete(*self.tree.get_children())
        self.files.clear()
        self.infos.clear()
        self._update_hints()
        self._refresh_summary()

    def _row_text(self, path: Path) -> str:
        info = self.infos.get(path)
        if not info or not info.width:
            return "อ่านข้อมูลไม่ได้"
        bits = [f"{info.width}×{info.height}"]
        if info.duration:
            bits.append(core.human_duration(info.duration))
        bits.append(core.human_size(path.stat().st_size))
        if info.has_audio:
            bits.append("เสียง")
        if abs(info.rotation) > 0.5:
            bits.append(f"หมุน {abs(info.rotation):g}°")
        if info.is_hdr:
            bits.append("HDR")
        return " • ".join(bits)

    # -- option state ------------------------------------------------------ #

    def _on_action(self) -> None:
        self._update_hints()
        self._refresh_summary()

    def _on_mode(self) -> None:
        self._update_hints()
        self._refresh_summary()

    def _probe_gpu(self) -> None:
        works = core.gpu_works()
        self.root.after(0, self._apply_gpu_state, works)

    def _apply_gpu_state(self, works: bool) -> None:
        self.gpu_available = works
        self.gpu_chk.configure(state="normal" if works else "disabled")
        if not works:
            self.gpu.set(False)
            self.gpu_chk.configure(text="ใช้ GPU — ไม่พบการ์ดจอที่รองรับ (ปิดอยู่)")
        else:
            self.gpu_chk.configure(text=f"ใช้ GPU ({core.gpu_encoder()}) — เฉพาะโหมด CRF")
        self._update_hints()

    def _copy_allowed(self) -> bool:
        action = core.ACTIONS[self.action.get()]
        if action.is_flip:
            return False
        return all(core.supports_copy_mode(p) for p in self.files)

    def _update_hints(self) -> None:
        action = core.ACTIONS[self.action.get()]
        flip = action.is_flip
        copy_ok = self._copy_allowed()

        self.copy_radio.configure(state="normal" if copy_ok else "disabled")
        if not copy_ok and self.mode.get() == "copy":
            self.mode.set("crf")

        mode = self.mode.get()
        if mode == "copy":
            text = ("เขียนแค่ค่าหมุนลงในไฟล์ ใช้เวลาไม่ถึงวินาที "
                    "ข้อมูลภาพและเสียงไม่ถูกแตะเลย (bitstream เดิมทุกไบต์)")
            style = "Hint.TLabel"
        elif mode == "lossless":
            text = ("หมุนพิกเซลจริงแบบ pixel-exact (PSNR = ∞) — ไฟล์ใหญ่ขึ้น 3–10 เท่า "
                    "สำหรับงานที่ต้องการความแม่นยำสูงสุด")
            style = "Hint.TLabel"
        else:
            text = ("หมุนพิกเซลจริง เข้ารหัสใหม่คมชัดสูง ไฟล์ใหญ่ขึ้นเล็กน้อย "
                    "— เหมาะเมื่อปลายทางตัดค่าหมุนทิ้ง")
            style = "Hint.TLabel"
            if flip:
                text = "การพลิกภาพต้องเข้ารหัสใหม่เสมอ — " + text
                style = "Warn.TLabel"
            elif self.files and not copy_ok:
                text = "มีไฟล์ที่คอนเทนเนอร์ไม่เก็บค่าหมุน — " + text
                style = "Warn.TLabel"

        if self.files and any((self.infos.get(p) or core.MediaInfo()).is_hdr
                              for p in self.files):
            text += " • มีไฟล์ HDR: โปรดตรวจสอบเครื่องเล่นปลายทาง"
            style = "Warn.TLabel"

        if self.gpu_available:
            self.gpu_chk.configure(state="normal" if mode == "crf" else "disabled")
        else:
            self.gpu_chk.configure(state="disabled")

        self.mode_hint.configure(text=text, style=style)
        self.out_hint.configure(
            text=(f"บันทึกที่: {self.out_dir.get()}" if self.out_dir.get()
                  else "เว้นว่างไว้ = บันทึกโฟลเดอร์เดียวกับไฟล์ต้นฉบับ (ไม่ทับไฟล์เดิม)"))

    def _refresh_summary(self) -> None:
        if not self.files:
            self.summary_var.set("ลากไฟล์วิดีโอมาวางในหน้าต่างนี้ หรือกด “เพิ่มไฟล์…” "
                                 "(เลือกได้หลายไฟล์)")
            self.start_btn.configure(text="หมุนวิดีโอ")
            return

        mode = self.mode.get()
        total = 0
        estimate = 0
        for path in self.files:
            try:
                total += path.stat().st_size
            except OSError:
                continue
            info = self.infos.get(path) or core.MediaInfo()
            estimate += core.estimate_output_bytes(info, mode, path.stat().st_size)

        folder = Path(self.out_dir.get()) if self.out_dir.get() else self.files[0].parent
        free = core.free_space(folder)

        parts = [f"{len(self.files)} ไฟล์", f"รวม {core.human_size(total)}",
                 f"ประมาณผลลัพธ์ {core.human_size(estimate)}"]
        if free:
            parts.append(f"พื้นที่ว่าง {core.human_size(free)}")
            if estimate > free:
                parts.append("⚠ พื้นที่อาจไม่พอ")
        self.summary_var.set(" • ".join(parts))
        self.start_btn.configure(
            text=f"หมุนวิดีโอ ({len(self.files)} ไฟล์)" if len(self.files) > 1
            else "หมุนวิดีโอ")

    # -- run --------------------------------------------------------------- #

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.files:
            messagebox.showinfo(APP_NAME, "กรุณาเพิ่มไฟล์วิดีโอก่อน")
            return

        action_key = self.action.get()
        mode = self.mode.get()

        if core.ACTIONS[action_key].is_flip and mode == "copy":
            self.mode.set("crf")
            mode = "crf"
        if mode == "copy" and not all(core.supports_copy_mode(p) for p in self.files):
            if not messagebox.askokcancel(
                    APP_NAME,
                    "มีไฟล์ที่คอนเทนเนอร์ไม่รองรับการเก็บค่าหมุน\n"
                    "ต้องการสลับไปโหมด “ฝังการหมุนจริง (CRF 16)” ให้ทุกไฟล์หรือไม่?"):
                return
            self.mode.set("crf")
            mode = "crf"

        out_dir = Path(self.out_dir.get()) if self.out_dir.get() else None
        folder = out_dir or self.files[0].parent
        needed = 0
        for path in self.files:
            info = self.infos.get(path) or core.MediaInfo()
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            needed += core.estimate_output_bytes(info, mode, size)
        free = core.free_space(folder)
        if free and needed > free:
            if not messagebox.askyesno(
                    APP_NAME,
                    f"พื้นที่ว่างอาจไม่พอ\n\nต้องใช้ประมาณ {core.human_size(needed)}\n"
                    f"เหลืออยู่ {core.human_size(free)}\n\nต้องการทำต่อหรือไม่?"):
                return

        core.save_settings({"action": action_key, "mode": mode,
                            "out_dir": self.out_dir.get(), "gpu": bool(self.gpu.get())})

        use_gpu = bool(self.gpu.get()) and self.gpu_available and mode == "crf"
        self.cancel_event.clear()
        self.total_files = len(self.files)
        self.done_files = 0
        self.successes = 0
        self.start_btn.configure(state="disabled", text="กำลังทำงาน…")
        self.cancel_btn.configure(state="normal")
        self.open_file_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self.status_var.set("เริ่มต้น…")
        self._log_clear()
        self._log(f"โหมด: {core.MODES[mode]}")
        self._log(f"ทิศทาง: {core.ACTIONS[action_key].label}")
        if use_gpu:
            self._log(f"ตัวเข้ารหัส: GPU ({core.gpu_encoder()})")

        self.worker = threading.Thread(
            target=self._run_queue,
            args=(list(self.files), action_key, mode, out_dir, use_gpu),
            daemon=True)
        self.worker.start()

    def _run_queue(self, files: list[Path], action_key: str, mode: str,
                   out_dir: Path | None, use_gpu: bool) -> None:
        total = len(files)
        results: list[core.RunResult] = []
        for idx, src in enumerate(files, 1):
            if self.cancel_event.is_set():
                break
            self.events.put(("file_start", (idx, total, src.name)))
            try:
                result = core.rotate(
                    src, action_key, mode, out_dir, use_gpu=use_gpu,
                    on_progress=lambda pct, speed, i=idx: self.events.put(
                        ("progress", (i, total, pct, speed))),
                    cancel=self.cancel_event)
            except Exception as exc:  # pragma: no cover - defensive
                result = core.RunResult(False, -1, src, str(exc))
            results.append(result)
            self.events.put(("file_done", (idx, total, result)))
        self.events.put(("queue_done", results))

    def cancel(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.status_var.set("กำลังยกเลิก…")

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "file_start":
                    idx, total, name = payload
                    self.file_started = time.monotonic()
                    self.status_var.set(f"กำลังทำ {idx}/{total}: {name}")
                    self._log(f"▶ {name}")
                elif kind == "progress":
                    idx, total, pct, speed = payload
                    self.progress.configure(value=((idx - 1) + pct / 100.0) / total * 100.0)
                    bits = [f"ไฟล์ {idx}/{total}", f"{pct:.0f}%"]
                    if speed > 0:
                        bits.append(f"{speed:.1f}x")
                    if pct > 2:
                        elapsed = time.monotonic() - self.file_started
                        remain = elapsed / (pct / 100.0) * (1 - pct / 100.0)
                        bits.append(f"เหลือ {core.human_duration(remain)}")
                    self.status_var.set(" • ".join(bits))
                elif kind == "file_done":
                    idx, total, result = payload
                    self._on_file_done(idx, total, result)
                elif kind == "queue_done":
                    self._on_queue_done(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _on_file_done(self, idx: int, total: int, result: core.RunResult) -> None:
        self.done_files = idx
        if result.ok:
            self.successes += 1
            self.last_output = result.output
            self.open_file_btn.configure(state="normal")
            self.open_btn.configure(state="normal")
            size = ""
            try:
                size = f" • {core.human_size(result.output.stat().st_size)}"
            except OSError:
                pass
            self._log(f"   เสร็จ: {result.output.name}{size}")
            if result.verified:
                self._log(f"   ตรวจแล้ว: {result.verified}")
            if result.warning:
                self._log(f"   หมายเหตุ: {result.warning}")
        else:
            self._log(f"   ไม่สำเร็จ: {result.log.strip()[:400] or 'ไม่ทราบสาเหตุ'}")

    def _on_queue_done(self, results: list[core.RunResult]) -> None:
        self.start_btn.configure(state="normal", text="หมุนวิดีโอ")
        self.cancel_btn.configure(state="disabled")
        if not self.files:
            self._refresh_summary()
        failed = [r for r in results if not r.ok and r.log != "ยกเลิกแล้ว"]
        cancelled = self.cancel_event.is_set()

        if cancelled:
            self.progress.configure(value=0)
            self.status_var.set("ยกเลิกแล้ว")
            self._log("ยกเลิกโดยผู้ใช้")
        elif failed:
            self.status_var.set(f"เสร็จสิ้น: สำเร็จ {self.successes}/{len(results)}")
            self._log(f"สรุป: สำเร็จ {self.successes} • ไม่สำเร็จ {len(failed)}")
            messagebox.showwarning(
                APP_NAME,
                f"สำเร็จ {self.successes} จาก {len(results)} ไฟล์\n\n"
                "ไฟล์ที่ไม่สำเร็จ:\n" + "\n".join(f"• {r.output.name}: {r.log[:160]}"
                                                  for r in failed[:5]))
        else:
            self.progress.configure(value=100)
            self.status_var.set(f"เสร็จสิ้น: สำเร็จ {self.successes}/{len(results)} ไฟล์")
            self._log(f"สรุป: สำเร็จ {self.successes} ไฟล์")

    # -- misc -------------------------------------------------------------- #

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _log_clear(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def open_output_file(self) -> None:
        if self.last_output and self.last_output.exists():
            try:
                os.startfile(str(self.last_output))  # type: ignore[attr-defined]
            except Exception:
                subprocess.Popen(["explorer", str(self.last_output)])

    def open_output_folder(self) -> None:
        if self.last_output:
            folder = self.last_output.parent
            try:
                os.startfile(str(folder))  # type: ignore[attr-defined]
            except Exception:
                subprocess.Popen(["explorer", str(folder)])

    def _on_close(self) -> None:
        core.save_settings({"action": self.action.get(), "mode": self.mode.get(),
                            "out_dir": self.out_dir.get(), "gpu": bool(self.gpu.get())})
        self.root.destroy()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def run_cli(argv: list[str]) -> int:
    core.attach_console()
    parser = argparse.ArgumentParser(
        prog="RotateVideo",
        description="หมุนวิดีโอแบบไม่เสียคุณภาพ (ค่าเริ่มต้น: ไม่เข้ารหัสใหม่)")
    parser.add_argument("inputs", nargs="+", help="ไฟล์วิดีโอ (หลายไฟล์ได้)")
    parser.add_argument("--rotate", choices=sorted(core.ACTIONS), default="cw90",
                        help="cw90 | ccw90 | rot180 | fliph | flipv")
    parser.add_argument("--mode", choices=["copy", "crf", "lossless"], default="copy")
    parser.add_argument("--out", default="", help="โฟลเดอร์ปลายทาง")
    parser.add_argument("--gpu", action="store_true", help="ใช้ GPU (โหมด crf)")
    parser.add_argument("--yes", action="store_true", help="ทำต่อแม้พื้นที่อาจไม่พอ")
    args = parser.parse_args(argv)

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for raw in args.inputs:
        src = Path(raw)
        if not src.exists():
            _say(f"FAIL  {src}: not found")
            failures += 1
            continue
        _say(f"...   {src.name}")
        result = core.rotate(src, args.rotate, args.mode, out_dir, use_gpu=args.gpu)
        if result.ok:
            _say(f"OK    {src.name} -> {result.output}")
            if result.verified:
                _say(f"      checked: {result.verified}")
            if result.warning:
                _say(f"      note: {result.warning}")
        else:
            _say(f"FAIL  {src.name}: {result.log.strip()[:300]}")
            failures += 1
    _say(f"done: {len(args.inputs) - failures} ok, {failures} failed")
    return 0 if failures == 0 else 1


# --------------------------------------------------------------------------- #
# Self test
# --------------------------------------------------------------------------- #

def run_selftest() -> int:
    log_path = Path(tempfile.gettempdir()) / "RotateVideo-selftest.log"
    lines: list[str] = []
    ok = True

    def note(text: str) -> None:
        lines.append(text)

    def check(condition: bool, text: str) -> None:
        nonlocal ok
        note(("PASS  " if condition else "FAIL  ") + text)
        ok = ok and condition

    tmp = Path(tempfile.mkdtemp(prefix="rotatevideo_selftest_"))
    try:
        ff = str(core.ffmpeg_exe())
        note(f"ffmpeg: {ff}")
        ver = subprocess.run([ff, "-version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        note("version: " + (ver.stdout.splitlines() or ["?"])[0])

        clip = tmp / "selftest.mp4"
        subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
             "-t", "2", "-c:v", "libx264", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)],
            check=True, capture_output=True)
        check(clip.exists(), "สร้างไฟล์ทดสอบ")

        info = core.probe(clip)
        check((info.width, info.height) == (640, 360), f"probe ขนาด {info.width}x{info.height}")
        check(abs(info.rotation) < 0.01, f"probe ค่าหมุนเริ่มต้น {info.rotation}")
        check(info.has_audio, "probe ตรวจพบเสียง")
        check(core.estimate_output_bytes(info, "copy", clip.stat().st_size) > 0,
              "ประมาณขนาดไฟล์ทำงาน")

        # 1) metadata-only mode: bitstream must stay byte-identical
        res_copy = core.rotate(clip, "cw90", "copy", tmp)
        check(res_copy.ok, "โหมดคัดลอกทำงานสำเร็จ")
        rot_copy = core.probe(res_copy.output).rotation if res_copy.ok else 0
        check(abs(rot_copy + 90) < 0.01, f"โหมดคัดลอกเขียนค่าหมุน = {rot_copy}")
        check(bool(res_copy.verified), f"ตรวจผลลัพธ์อัตโนมัติ: {res_copy.verified}")

        def stream_md5(path: Path) -> str:
            out = subprocess.run(
                [ff, "-v", "error", "-noautorotate", "-i", str(path),
                 "-map", "0:v", "-c", "copy", "-f", "md5", "-"],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            return out.stdout.strip().split("=")[-1]

        check(stream_md5(clip) == stream_md5(res_copy.output),
              "โหมดคัดลอก: bitstream เหมือนต้นฉบับทุกไบต์")

        # 2) lossless mode: dimensions swap and PSNR is infinite
        res_ll = core.rotate(clip, "cw90", "lossless", tmp)
        check(res_ll.ok, "โหมด lossless ทำงานสำเร็จ")
        ll_info = core.probe(res_ll.output) if res_ll.ok else core.MediaInfo()
        check((ll_info.width, ll_info.height) == (360, 640),
              f"lossless หมุนขนาดเป็น {ll_info.width}x{ll_info.height}")
        check(abs(ll_info.rotation) < 0.01, f"lossless เคลียร์ค่าหมุน ({ll_info.rotation})")

        psnr = subprocess.run(
            [ff, "-hide_banner", "-y", "-i", str(clip), "-i", str(res_ll.output),
             "-lavfi", "[0:v]transpose=1[r];[r][1:v]psnr", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        psnr_line = next((ln for ln in psnr.stderr.splitlines() if "PSNR" in ln), "")
        check("PSNR y:inf" in psnr_line, "lossless: PSNR = inf (พิกเซลตรงทุกจุด)")

        # 3) already rotated input keeps the correct absolute orientation
        rotated = tmp / "already_rotated.mp4"
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                        "-display_rotation", "-90", "-i", str(clip),
                        "-map", "0", "-c", "copy", str(rotated)], check=True,
                       capture_output=True)
        res_two = core.rotate(rotated, "cw90", "lossless", tmp)
        two_info = core.probe(res_two.output) if res_two.ok else core.MediaInfo()
        check(res_two.ok and (two_info.width, two_info.height) == (640, 360),
              f"ไฟล์ที่หมุนอยู่แล้ว + 90° ได้ขนาด {two_info.width}x{two_info.height}")
        check(abs(two_info.rotation) < 0.01,
              "ไฟล์ที่หมุนอยู่แล้ว: ไม่หมุนซ้อน (ค่า metadata = 0)")

        # 4) CRF mode + audio preservation
        res_crf = core.rotate(clip, "ccw90", "crf", tmp)
        check(res_crf.ok, "โหมด CRF ทำงานสำเร็จ")
        crf_info = core.probe(res_crf.output) if res_crf.ok else core.MediaInfo()
        check((crf_info.width, crf_info.height) == (360, 640),
              f"CRF หมุนขนาดเป็น {crf_info.width}x{crf_info.height}")
        check(crf_info.has_audio, "CRF รักษาแทร็กเสียงไว้")

        # 5) container fallback: .ogv cannot hold H.264 -> must become .mp4
        ogv = tmp / "selftest.ogv"
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                        "-f", "lavfi", "-i", "sine=frequency=300", "-t", "1",
                        "-c:v", "libtheora", "-c:a", "libvorbis", "-shortest",
                        str(ogv)], check=True, capture_output=True)
        ext, note_txt = core.output_extension(".ogv", "crf")
        check(ext == ".mp4", f"ตัดสินคอนเทนเนอร์: {ext} {note_txt[:40]}")
        res_ogv = core.rotate(ogv, "cw90", "crf", tmp)
        check(res_ogv.ok and res_ogv.output.suffix == ".mp4",
              f"แปลง .ogv สำเร็จ -> {res_ogv.output.suffix}")

        # 6) MKV attachments (subtitle fonts) survive a re-encode
        asset = resource("assets/app.ico")
        if asset.exists():
            att = tmp / "with_attachment.mkv"
            subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                            "-i", str(clip), "-map", "0", "-c", "copy",
                            "-attach", str(asset),
                            "-metadata:s:t", "mimetype=application/octet-stream",
                            str(att)], check=True, capture_output=True)
            att_info = core.probe(att)
            check(att_info.has_attachments, "อ่านพบไฟล์แนบใน MKV")
            res_att = core.rotate(att, "cw90", "lossless", tmp)
            text_att = core._read_media_text(res_att.output) if res_att.ok else ""
            check(res_att.ok and "Attachment:" in text_att,
                  "โหมดฝังจริงเก็บไฟล์แนบ (ฟอนต์ซับ) ไว้")

        # 7) CLI works end to end
        cli_out = tmp / "cli_out"
        cli_out.mkdir(exist_ok=True)
        cmd = [sys.executable]
        if not getattr(sys, "frozen", False):
            cmd.append(str(Path(__file__).resolve()))
        cmd += [str(clip), "--rotate", "cw90", "--mode", "copy", "--out", str(cli_out)]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
        cli_file = cli_out / (clip.stem + "_rot90cw.mp4")
        check(proc.returncode == 0 and cli_file.exists(),
              f"โหมด CLI ทำงาน (rc={proc.returncode})")

        # 8) GPU detection must not crash (availability depends on hardware)
        note(f"INFO  gpu encoder={core.gpu_encoder()} works={core.gpu_works()}")
    except Exception as exc:  # pragma: no cover
        ok = False
        note(f"EXCEPTION {type(exc).__name__}: {exc}")
    finally:
        note("RESULT: " + ("OK" if ok else "FAILED"))
        log_path.write_text("\n".join(lines), encoding="utf-8")
        for line in lines:
            _say(line)
    return 0 if ok else 1


# --------------------------------------------------------------------------- #

def main() -> None:
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(run_selftest())
    if any(a in argv for a in ("--rotate", "--mode", "--out", "--gpu", "--yes")):
        sys.exit(run_cli(argv))

    _enable_dpi_awareness()
    root = tk.Tk()
    app = RotateApp(root)

    paths = [Path(a) for a in argv
             if not a.startswith("-") and Path(a).exists()
             and Path(a).suffix.lower() in core.KNOWN_EXTENSIONS]
    if paths:
        root.after(80, lambda: app.add_files(paths))

    root.mainloop()


if __name__ == "__main__":
    main()
