"""A small Windows desktop interface for MacroCore. Run: python macro_gui.py"""

import ctypes
import json
import queue
import sys
import time
import tkinter as tk
from argparse import Namespace
from collections import deque
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from macrocore import MacroCore, parse_args, user32, validate_settings


BG, PANEL, TEXT, MUTED = "#111820", "#1b2633", "#eef3fa", "#9aadc3"
BLUE, RED, GREEN = "#508dff", "#ef6476", "#69d3a7"
HUD_STYLES = 0x00080000 | 0x00000020 | 0x08000000 | 0x00000080

user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
_get_window_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
_get_window_style.argtypes = [wintypes.HWND, ctypes.c_int]
_get_window_style.restype = ctypes.c_ssize_t
_set_window_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
_set_window_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_set_window_style.restype = ctypes.c_ssize_t
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD,
                                             wintypes.BYTE, wintypes.DWORD]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                        wintypes.LPVOID, wintypes.UINT]


def configure_hud(window):
    """A layered, click-through tool window that never takes keyboard focus."""
    window.update_idletasks()
    hwnd = user32.GetAncestor(window.winfo_id(), 2)  # GA_ROOT: Tk's native wrapper
    if not hwnd:
        raise RuntimeError("Could not find the status window")
    style = _get_window_style(hwnd, -20)
    ctypes.set_last_error(0)
    previous = _set_window_style(hwnd, -20, style | HUD_STYLES)
    if previous == 0 and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    if not user32.SetLayeredWindowAttributes(hwnd, 0, 242, 2):
        raise ctypes.WinError(ctypes.get_last_error())
    return hwnd


class MacroApp:
    def __init__(self, root, core):
        self.root = root
        self.core = core
        self.dirty = False
        self.closed = False
        self.restore_when_idle = False
        self.hook_error_shown = False
        self.play_requested_at = 0.0
        self.hud_visible = False
        self.log_messages = deque(maxlen=200)
        self.log_window = None
        self.log = None
        self.poll_id = None
        root.title("MacroCore")
        root.configure(background=BG)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Leelawadee UI", 10))
        style.configure("Muted.TLabel", foreground=MUTED, font=("Leelawadee UI", 9))
        style.configure("TButton", background=PANEL, foreground=TEXT, borderwidth=0,
                        padding=(10, 7), font=("Leelawadee UI", 10))
        style.map("TButton", background=[("active", "#2b3b50"), ("disabled", "#18212c")],
                  foreground=[("disabled", "#64758a")])
        style.configure("Tool.TButton", padding=(8, 5), font=("Leelawadee UI", 9))
        for name, normal, hover in (("Record", "#b94255", "#ce4e63"),
                                    ("Play", "#346ad4", "#447ee9")):
            style.configure(f"{name}.TButton", background=normal, padding=(12, 10))
            style.map(f"{name}.TButton", background=[("disabled", "#18212c"), ("active", hover)])
        style.configure("TCheckbutton", background=BG, foreground=TEXT,
                        font=("Leelawadee UI", 10), indicatorbackground=PANEL)
        style.map("TCheckbutton", background=[("active", BG)],
                  foreground=[("disabled", "#64758a")])
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL,
                        foreground=TEXT, arrowcolor=MUTED, padding=4)
        style.map("TCombobox", fieldbackground=[("disabled", "#18212c")],
                  foreground=[("disabled", "#64758a")])
        root.option_add("*TCombobox*Listbox.background", PANEL)
        root.option_add("*TCombobox*Listbox.foreground", TEXT)
        root.option_add("*TCombobox*Listbox.selectBackground", "#346ad4")

        outer = ttk.Frame(root, padding=14)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="MacroCore", font=("Segoe UI", 13, "bold")).pack(side="left")
        self.status = ttk.Label(header, text="● พร้อม", foreground=GREEN)
        self.status.pack(side="right")
        self.filename = ttk.Label(outer, text=Path(core.args.file).name, style="Muted.TLabel")
        self.filename.pack(anchor="w", pady=(6, 0))
        self.summary = ttk.Label(outer, text="0 events · 0.00 s", style="Muted.TLabel")
        self.summary.pack(anchor="w", pady=(0, 8))

        actions = ttk.Frame(outer)
        actions.pack(fill="x")
        for column in range(3):
            actions.columnconfigure(column, weight=1)
        self.record_button = ttk.Button(actions, text="● อัด  F9", style="Record.TButton", command=self.record)
        self.play_button = ttk.Button(actions, text="▶ เล่น  F10", style="Play.TButton", command=self.play)
        self.stop_button = ttk.Button(actions, text="■ หยุด  F11", command=core.stop)
        for column, button in enumerate((self.record_button, self.play_button, self.stop_button)):
            button.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0))

        files = ttk.Frame(outer)
        files.pack(fill="x", pady=(8, 8))
        self.open_button = ttk.Button(files, text="เปิด…", width=0, style="Tool.TButton", command=self.open_file)
        self.open_button.pack(side="left")
        self.save_button = ttk.Button(files, text="บันทึก  F8", width=0, style="Tool.TButton", command=self.save_file)
        self.save_button.pack(side="left", padx=(5, 0))
        self.settings_button = ttk.Button(files, text="ตั้งค่า…", width=0, style="Tool.TButton", command=self.show_settings)
        self.settings_button.pack(side="right")
        self.history_button = ttk.Button(files, text="ประวัติ", width=0, style="Tool.TButton", command=self.show_history)
        self.history_button.pack(side="right", padx=(0, 5))
        self.settings_summary = ttk.Label(outer, style="Muted.TLabel")
        self.settings_summary.pack(anchor="w")
        ttk.Label(outer, text="F9 อัด · F10 เล่น · F11 หยุด · F12 ปิด", style="Muted.TLabel").pack(anchor="w")

        self.speed = tk.StringVar(value=str(core.args.speed))
        self.loops = tk.StringVar(value=str(max(1, core.args.loop)))
        self.forever = tk.BooleanVar(value=core.args.loop == 0)
        self.delay = tk.StringVar(value=str(core.args.delay))
        self.loop_delay = tk.StringVar(value=str(core.args.loop_delay))
        self.mouse_hz = tk.StringVar(value=str(core.args.mouse_hz))
        self.precision = tk.BooleanVar(value=core.args.wait == "hybrid")
        self.timer = tk.BooleanVar(value=core.args.timer_1ms)
        self.minimize = tk.BooleanVar(value=True)
        self.show_hud = tk.BooleanVar(value=True)
        self.setting_widgets = []

        self.settings_window = tk.Toplevel(root)
        self.settings_window.withdraw()
        self.settings_window.title("ตั้งค่า · MacroCore")
        self.settings_window.configure(background=BG)
        self.settings_window.resizable(False, False)
        self.settings_window.protocol("WM_DELETE_WINDOW", self.apply_settings)
        options = ttk.Frame(self.settings_window, padding=18)
        options.pack(fill="both", expand=True)
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="การเล่นมาโคร", font=("Leelawadee UI", 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        def field(parent, label, variable, row, values):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 20), pady=5)
            widget = ttk.Combobox(parent, textvariable=variable, width=12, values=values)
            widget.grid(row=row, column=1, sticky="ew", pady=5)
            self.setting_widgets.append(widget)
            return widget

        field(options, "ความเร็ว (เท่า)", self.speed, 1, (0.25, 0.5, 1, 1.5, 2, 3, 5, 10))
        self.loop_input = field(options, "จำนวนรอบ", self.loops, 2, (1, 2, 5, 10, 20, 100))
        infinite = ttk.Checkbutton(options, text="วนต่อเนื่องจนกดหยุด", variable=self.forever)
        infinite.grid(row=3, column=0, columnspan=2, sticky="w", pady=4)
        field(options, "รอก่อนเล่น (วินาที)", self.delay, 4, (0, 1, 3, 5, 10))
        field(options, "พักระหว่างรอบ (วินาที)", self.loop_delay, 5, (0, 0.5, 1, 3, 5))
        ttk.Label(options, text="พื้นที่จอขณะอัด / เล่น", font=("Leelawadee UI", 11, "bold")).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(16, 4))
        minimize = ttk.Checkbutton(options, text="ย่อหน้าต่างเมื่อเริ่มอัด / เล่น", variable=self.minimize)
        minimize.grid(row=7, column=0, columnspan=2, sticky="w", pady=4)
        hud_option = ttk.Checkbutton(options, text="แสดงแถบสถานะเล็กที่มุมจอ (คลิกทะลุ)", variable=self.show_hud)
        hud_option.grid(row=8, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(options, text="ปิดแถบสถานะเพื่อใช้พื้นที่จอทั้งหมด · หยุดด้วย F9 / F11",
                  style="Muted.TLabel").grid(row=9, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.advanced = ttk.Frame(options)
        self.advanced.columnconfigure(1, weight=1)
        self.advanced_button = ttk.Button(options, text="▸ ตัวเลือกขั้นสูง", command=self.toggle_advanced)
        self.advanced_button.grid(row=10, column=0, columnspan=2, sticky="w", pady=5)
        field(self.advanced, "Mouse rate (Hz)", self.mouse_hz, 0, (0, 125, 250, 500, 1000))
        accurate = ttk.Checkbutton(self.advanced, text="จับเวลาละเอียด (ใช้ CPU เพิ่ม)", variable=self.precision)
        accurate.grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        timer = ttk.Checkbutton(self.advanced, text="ขอ timer resolution 1 ms", variable=self.timer)
        timer.grid(row=2, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Label(self.advanced, text="250 Hz ลด move ที่ถี่เกินไป · 0 เก็บทุกจุด",
                  style="Muted.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=4)
        self.advanced.grid(row=11, column=0, columnspan=2, sticky="ew")
        self.advanced.grid_remove()
        self.advanced_visible = False
        ttk.Button(options, text="เสร็จ", style="Play.TButton", command=self.apply_settings).grid(
            row=12, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.setting_widgets.extend((infinite, accurate, timer, minimize, hud_option))

        self.hud = tk.Toplevel(root)
        self.hud.withdraw()
        self.hud.overrideredirect(True)
        self.hud.configure(background="#3a4b60")
        self.hud.attributes("-topmost", True)
        self.hud.attributes("-alpha", 0.95)
        self.hud_label = tk.Label(self.hud, text="● REC  00:00     F9 / F11 หยุด", bg=PANEL,
                                  fg=RED, padx=14, pady=9, font=("Leelawadee UI", 10, "bold"))
        self.hud_label.pack(padx=1, pady=1)
        self._write_log("พร้อมใช้งาน · อัด/เล่นแล้วหน้าต่างจะย่อให้พื้นที่จอ")
        self.poll()
        root.update_idletasks()
        root.geometry(f"{max(400, root.winfo_reqwidth())}x{root.winfo_reqheight()}")

    def _write_log(self, message):
        self.log_messages.append(message)
        if self.log is not None and self.log.winfo_exists():
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("end", "\n".join(self.log_messages) + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

    def show_history(self):
        if self.core.recording or (self.core.player and self.core.player.is_alive()):
            return
        if self.log_window is None or not self.log_window.winfo_exists():
            self.log_window = tk.Toplevel(self.root)
            self.log_window.title("ประวัติ · MacroCore")
            self.log_window.geometry("640x340")
            self.log_window.configure(background=BG)
            self.log = scrolledtext.ScrolledText(self.log_window, wrap="word", background=BG,
                                                foreground=TEXT, font=("Consolas", 10),
                                                relief="flat", padx=12, pady=12)
            self.log.pack(fill="both", expand=True)
            self.log.insert("end", "\n".join(self.log_messages) + "\n")
            self.log.configure(state="disabled")
        self.log_window.deiconify()
        self.log_window.lift()

    def show_settings(self):
        if self.core.recording or (self.core.player and self.core.player.is_alive()):
            return
        self.settings_window.deiconify()
        self.settings_window.lift()

    def apply_settings(self):
        try:
            self._settings()
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("ตั้งค่าไม่ถูกต้อง", str(exc), parent=self.settings_window)
            return
        self.settings_window.withdraw()

    def toggle_advanced(self):
        self.advanced_visible = not self.advanced_visible
        if self.advanced_visible:
            self.advanced.grid()
        else:
            self.advanced.grid_remove()
        self.advanced_button.configure(text=("▾" if self.advanced_visible else "▸") + " ตัวเลือกขั้นสูง")

    def _prepare_workspace(self):
        # Close auxiliary windows before capture so they cannot cover the target app.
        self.settings_window.withdraw()
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.withdraw()
        if self.minimize.get():
            self.root.iconify()
            self.restore_when_idle = True
            if self.show_hud.get():
                try:
                    self._show_hud()
                except (OSError, RuntimeError, tk.TclError) as exc:
                    self._hide_hud()
                    self._write_log(f"Status bar unavailable; using hidden mode: {exc}")

    def _show_hud(self):
        hwnd = configure_hud(self.hud)
        work = wintypes.RECT()
        if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        width, height = self.hud.winfo_reqwidth(), self.hud.winfo_reqheight()
        x, y = max(work.left, work.right - width - 16), work.top + 16
        self.hud.geometry(f"{width}x{height}+{x}+{y}")
        self.hud.deiconify()
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        if not user32.SetWindowPos(hwnd, -1, x, y, width, height, 0x0010):
            raise ctypes.WinError(ctypes.get_last_error())
        self.hud_visible = True

    def _hide_hud(self):
        self.hud.withdraw()
        self.hud_visible = False

    def _restore(self):
        self._hide_hud()
        self.root.deiconify()

    def _settings(self):
        # Validate a copy; malformed UI values cannot partially change core settings.
        values = vars(self.core.args).copy()
        values.update(speed=float(self.speed.get()),
                      loop=0 if self.forever.get() else int(self.loops.get()),
                      delay=float(self.delay.get()), loop_delay=float(self.loop_delay.get()),
                      mouse_hz=int(self.mouse_hz.get()),
                      wait="hybrid" if self.precision.get() else "sleep",
                      timer_1ms=self.timer.get())
        if not self.forever.get() and values["loop"] < 1:
            raise ValueError("จำนวนรอบต้องอย่างน้อย 1 หรือเลือกวนต่อเนื่อง")
        settings = Namespace(**values)
        validate_settings(settings)
        self.core.args = settings

    def _discard_or_save(self):
        if not self.dirty:
            return True
        self._restore()
        answer = messagebox.askyesnocancel("มาโครยังไม่ได้บันทึก", "บันทึกมาโครปัจจุบันก่อนดำเนินการต่อหรือไม่?",
                                          parent=self.root)
        if answer is None:
            return False
        return self.save_file() if answer else True

    def record(self):
        if self.core.recording:
            self.core.toggle_record()
            self._restore()
            return
        if self.core.player and self.core.player.is_alive():
            return
        if self.core.hook_error or not self._discard_or_save():
            return
        self._prepare_workspace()
        self.core.toggle_record()
        self.dirty = True

    def play(self):
        if self.core.recording:
            return
        if self.core.player and self.core.player.is_alive():
            self.core.toggle_play()
            return
        if self.core.hook_error:
            return
        try:
            self._settings()
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("ตั้งค่าไม่ถูกต้อง", str(exc), parent=self.root)
            return
        with self.core.lock:
            if not self.core.events:
                self._write_log("ยังไม่มีมาโคร — กด F9 เพื่ออัด หรือเปิดไฟล์ก่อน")
                return
        self._prepare_workspace()
        self.play_requested_at = time.perf_counter()
        self.core.toggle_play()

    def save_file(self):
        if self.core.recording or (self.core.player and self.core.player.is_alive()):
            self._write_log("หยุดอัด / เล่นก่อนบันทึกไฟล์")
            return False
        self._restore()
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json",
                                          initialfile=Path(self.core.args.file).name,
                                          filetypes=[("Macro JSON", "*.json")])
        if not path:
            return False
        try:
            self.core.save(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("บันทึกไม่ได้", str(exc), parent=self.root)
            return False
        self.dirty = False
        return True

    def open_file(self):
        if self.core.recording or (self.core.player and self.core.player.is_alive()):
            return
        if not self._discard_or_save():
            return
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("Macro JSON", "*.json")])
        if not path:
            return
        try:
            self.core.load(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("เปิดไม่ได้", str(exc), parent=self.root)
            return
        self.dirty = False

    def close(self):
        if self.closed:
            return
        self.core.stop()
        # Let the playback thread release held inputs before any save prompt.
        if self.core.player and self.core.player.is_alive():
            self.root.after(50, self.close)
            return
        if not self._discard_or_save():
            return
        self.closed = True
        if self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
        self.core.shutdown()
        self.root.destroy()

    def poll(self):
        if self.closed:
            return
        actions = {"record": self.record, "play": self.play, "stop": self.core.stop,
                   "save": self.save_file, "quit": self.close}
        # Bound each drain so hotkeys and Tk's own events stay responsive.
        for _ in range(50):
            try:
                command = self.core.commands.get_nowait()
            except queue.Empty:
                break
            actions[command]()
            if self.closed:
                return
        for _ in range(50):
            try:
                self._write_log(self.core.messages.get_nowait())
            except queue.Empty:
                break
        playing = bool(self.core.player and self.core.player.is_alive())
        recording = self.core.recording
        busy = recording or playing
        with self.core.lock:
            count = len(self.core.events)
            duration = self.core.events[-1].t if count else 0
        if recording:
            duration = self.core._now()
            label, color = "● กำลังอัด", RED
        elif playing:
            remaining = self.core.args.delay - (time.perf_counter() - self.play_requested_at)
            label = f"● รอ {remaining:.1f}s" if remaining > 0 else "● กำลังเล่น"
            color = BLUE
        elif self.core.hook_error:
            label, color = "● Hooks หยุดทำงาน", RED
        else:
            label, color = "● พร้อม", GREEN
        self.status.configure(text=label, foreground=color)
        self.summary.configure(text=f"{count:,} events · {duration:.2f} s")
        name = Path(self.core.args.file).name
        if len(name) > 38:
            name = name[:20] + "…" + name[-14:]
        self.filename.configure(text=("* " if self.dirty else "") + name)
        self.root.title(("* " if self.dirty else "") + "MacroCore")
        self.record_button.configure(text="■ หยุด  F9" if recording else "● อัด  F9")
        self.play_button.configure(text="■ หยุด  F10" if playing else "▶ เล่น  F10")
        loops = "วนต่อเนื่อง" if self.core.args.loop == 0 else f"{self.core.args.loop} รอบ"
        view = "แถบมุมจอ" if self.show_hud.get() else "ซ่อนหน้าต่าง"
        if not self.minimize.get():
            view = "คงหน้าต่าง"
        self.settings_summary.configure(text=f"{self.core.args.speed:g}× · {loops} · รอ {self.core.args.delay:g}s · {view}")
        if self.hud_visible:
            elapsed = max(0, self.core._now() if recording else time.perf_counter() - self.play_requested_at)
            minutes, seconds = divmod(int(elapsed), 60)
            mode = "REC" if recording else "PLAY"
            self.hud_label.configure(text=f"● {mode}  {minutes:02}:{seconds:02}     F9 / F11 หยุด" if recording
                                     else f"● {mode}  {minutes:02}:{seconds:02}     F10 / F11 หยุด",
                                     fg=RED if recording else BLUE)
        self.record_button.state(["disabled"] if playing or self.core.hook_error else ["!disabled"])
        self.play_button.state(["disabled"] if recording or not count or self.core.hook_error else ["!disabled"])
        self.stop_button.state(["!disabled"] if busy else ["disabled"])
        for widget in (self.open_button, self.save_button, self.settings_button,
                       self.history_button, *self.setting_widgets):
            widget.state(["disabled"] if busy else ["!disabled"])
        if self.forever.get():
            self.loop_input.state(["disabled"])
        if self.restore_when_idle and not busy:
            self._hide_hud()
            self._restore()
            self.restore_when_idle = False
        if self.core.hook_error and not self.hook_error_shown:
            self.hook_error_shown = True
            self._restore()
            messagebox.showerror("Input hooks หยุดทำงาน", str(self.core.hook_error), parent=self.root)
        self.poll_id = self.root.after(50, self.poll)


def main():
    args = parse_args(gui=True)
    user32.SetProcessDPIAware()
    root = None
    app = None
    core = MacroCore(args, output=None)
    try:
        root = tk.Tk()
        root.withdraw()
        if args.load:
            core.load()
        core.start()
        app = MacroApp(root, core)
        if args.self_test:
            root.update_idletasks()
            hud_hwnd = configure_hud(app.hud)
            report = {
                "frozen": bool(getattr(sys, "frozen", False)),
                "gui_ready": bool(root.winfo_exists()),
                "tcl_version": str(root.tk.call("info", "patchlevel")),
                "tk_version": str(root.tk.call("package", "provide", "Tk")),
                "hooks_ready": core.hook_thread.is_alive() and core.hook_error is None,
                "recording": core.recording,
                "playback_started": core.player is not None,
                "main_size": [root.winfo_reqwidth(), root.winfo_reqheight()],
                "hud_size": [app.hud.winfo_reqwidth(), app.hud.winfo_reqheight()],
                "hud_click_through": _get_window_style(hud_hwnd, -20) & HUD_STYLES == HUD_STYLES,
                "settings_hidden": app.settings_window.state() == "withdrawn",
            }
            core.shutdown()
            report["hooks_stopped"] = not core.hook_thread.is_alive()
            report["passed"] = (report["gui_ready"] and report["hooks_ready"]
                                and report["hooks_stopped"] and not report["recording"]
                                and not report["playback_started"] and report["hud_click_through"]
                                and report["settings_hidden"])
            Path(args.self_test).write_text(json.dumps(report, indent=2), encoding="utf-8")
            return 0 if report["passed"] else 1
        root.deiconify()
        root.mainloop()
        return 0
    except (OSError, ValueError, RuntimeError, tk.TclError) as exc:
        if args.self_test:
            Path(args.self_test).write_text(json.dumps({"passed": False, "error": str(exc)}, indent=2),
                                            encoding="utf-8")
        elif root is not None:
            messagebox.showerror("เปิด MacroCore ไม่ได้", str(exc), parent=root)
        return 1
    finally:
        if app is not None:
            app.closed = True
        core.shutdown()
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass


if __name__ == "__main__":
    sys.exit(main())
