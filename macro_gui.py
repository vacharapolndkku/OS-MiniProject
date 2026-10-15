"""A small Windows desktop interface for MacroCore. Run: python macro_gui.py"""

import json
import queue
import sys
import time
import tkinter as tk
from argparse import Namespace
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from macrocore import MacroCore, parse_args, user32, validate_settings


class MacroApp:
    def __init__(self, root, core):
        self.root = root
        self.core = core
        self.dirty = False
        self.closed = False
        self.restore_when_idle = False
        self.hook_error_shown = False
        self.play_requested_at = 0.0
        root.title("MacroCore — Personal Macro Recorder")
        root.geometry("760x690")
        root.minsize(700, 650)
        root.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TLabel", font=("Leelawadee UI", 10))
        style.configure("TButton", padding=(10, 8), font=("Leelawadee UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"))

        outer = ttk.Frame(root, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="MacroCore", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="อัดงานที่ทำซ้ำ แล้วเล่นกลับด้วยจังหวะที่คุณกำหนด").pack(anchor="w")

        self.status = ttk.Label(outer, text="พร้อมใช้งาน", foreground="#16714a",
                                font=("Leelawadee UI", 13, "bold"))
        self.status.pack(anchor="w", pady=(16, 3))
        self.summary = ttk.Label(outer, text="0 events · 0.00 s")
        self.summary.pack(anchor="w")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(12, 10))
        for column in range(3):
            actions.columnconfigure(column, weight=1)
        self.record_button = ttk.Button(actions, text="อัด   F9", command=self.record)
        self.play_button = ttk.Button(actions, text="เล่น   F10", command=self.play)
        self.stop_button = ttk.Button(actions, text="หยุดทั้งหมด   F11", command=core.stop)
        for column, button in enumerate((self.record_button, self.play_button, self.stop_button)):
            button.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0))

        files = ttk.Frame(outer)
        files.pack(fill="x")
        self.open_button = ttk.Button(files, text="เปิดไฟล์…", command=self.open_file)
        self.open_button.pack(side="left")
        self.save_button = ttk.Button(files, text="บันทึกเป็น…   F8", command=self.save_file)
        self.save_button.pack(side="left", padx=6)
        self.filename = ttk.Label(files, text=Path(core.args.file).name, wraplength=350)
        self.filename.pack(side="left", padx=6)

        options = ttk.LabelFrame(outer, text="ตั้งค่าการเล่น", padding=12)
        options.pack(fill="x", pady=14)
        options.columnconfigure(1, weight=1)
        options.columnconfigure(3, weight=1)
        self.speed = tk.StringVar(value=str(core.args.speed))
        self.loops = tk.StringVar(value=str(max(1, core.args.loop)))
        self.forever = tk.BooleanVar(value=core.args.loop == 0)
        self.delay = tk.StringVar(value=str(core.args.delay))
        self.loop_delay = tk.StringVar(value=str(core.args.loop_delay))
        self.mouse_hz = tk.StringVar(value=str(core.args.mouse_hz))
        self.precision = tk.BooleanVar(value=core.args.wait == "hybrid")
        self.timer = tk.BooleanVar(value=core.args.timer_1ms)
        self.minimize = tk.BooleanVar(value=True)
        self.setting_widgets = []

        def field(label, variable, row, column, values=None):
            ttk.Label(options, text=label).grid(row=row, column=column, sticky="w", padx=(0, 8), pady=5)
            widget = ttk.Combobox(options, textvariable=variable, width=12, values=values or ())
            widget.grid(row=row, column=column + 1, sticky="ew", padx=(0, 12), pady=5)
            self.setting_widgets.append(widget)
            return widget

        field("ความเร็ว (เท่า)", self.speed, 0, 0, (0.25, 0.5, 1, 1.5, 2, 3, 5, 10))
        self.loop_input = field("จำนวนรอบ", self.loops, 0, 2, (1, 2, 5, 10, 20, 100))
        field("รอก่อนเล่น (วินาที)", self.delay, 1, 0, (0, 1, 3, 5, 10))
        field("พักระหว่างรอบ (วินาที)", self.loop_delay, 1, 2, (0, 0.5, 1, 3, 5))
        field("Mouse rate (Hz)", self.mouse_hz, 2, 0, (0, 125, 250, 500, 1000))
        infinite = ttk.Checkbutton(options, text="วนต่อเนื่องจนกดหยุด", variable=self.forever)
        infinite.grid(row=2, column=2, columnspan=2, sticky="w")
        accurate = ttk.Checkbutton(options, text="จับเวลาละเอียด (ใช้ CPU เพิ่ม)", variable=self.precision)
        accurate.grid(row=3, column=0, columnspan=2, sticky="w", pady=5)
        timer = ttk.Checkbutton(options, text="ขอ timer resolution 1 ms", variable=self.timer)
        timer.grid(row=3, column=2, columnspan=2, sticky="w")
        minimize = ttk.Checkbutton(options, text="ย่อหน้าต่างเมื่อเริ่มอัด / เล่น", variable=self.minimize)
        minimize.grid(row=4, column=0, columnspan=4, sticky="w", pady=5)
        self.setting_widgets.extend((infinite, accurate, timer, minimize))
        ttk.Label(options, text="250 Hz ลดการขยับเมาส์ที่ถี่เกินไป · 0 เก็บทุกจุด · คลิกและปุ่มยังครบ",
                  wraplength=650).grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 0))

        ttk.Label(outer, text="สถานะล่าสุด").pack(anchor="w")
        self.log = scrolledtext.ScrolledText(outer, height=7, wrap="word", state="disabled",
                                            font=("Consolas", 9), relief="solid", borderwidth=1)
        self.log.pack(fill="both", expand=True, pady=(5, 10))
        ttk.Label(outer, text="F9 อัด/หยุดอัด   ·   F10 เล่น/หยุดเล่น   ·   F11 หยุดทั้งหมด   ·   F12 ปิด",
                  wraplength=700).pack(anchor="w")
        self._write_log("พร้อมใช้งาน — มาโครอ้างอิงตำแหน่งหน้าจอ ควรจัดหน้าต่างปลายทางให้เหมือนตอนอัด")
        self.poll()

    def _write_log(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 200:
            self.log.delete("1.0", "2.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _restore(self):
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
        if self.minimize.get():
            self.root.iconify()
            self.restore_when_idle = True
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
        if self.minimize.get():
            self.root.iconify()
            self.restore_when_idle = True
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
            label, color = "กำลังอัด — F9 / F11 เพื่อหยุด", "#bb3434"
        elif playing:
            remaining = self.core.args.delay - (time.perf_counter() - self.play_requested_at)
            label = f"เริ่มเล่นใน {remaining:.1f} วินาที" if remaining > 0 else "กำลังเล่น — F10 / F11 เพื่อหยุด"
            color = "#2456ad"
        elif self.core.hook_error:
            label, color = "Input hooks หยุดทำงาน — บันทึกไฟล์แล้วเปิดโปรแกรมใหม่", "#bb3434"
        else:
            label, color = "พร้อมใช้งาน", "#16714a"
        self.status.configure(text=label, foreground=color)
        self.summary.configure(text=f"{count:,} events · {duration:.2f} s")
        self.filename.configure(text=("* " if self.dirty else "") + Path(self.core.args.file).name)
        self.root.title(("* " if self.dirty else "") + "MacroCore — Personal Macro Recorder")
        self.record_button.configure(text="หยุดอัด   F9" if recording else "อัด   F9")
        self.play_button.configure(text="หยุดเล่น   F10" if playing else "เล่น   F10")
        self.record_button.state(["disabled"] if playing or self.core.hook_error else ["!disabled"])
        self.play_button.state(["disabled"] if recording or not count or self.core.hook_error else ["!disabled"])
        self.stop_button.state(["!disabled"] if busy else ["disabled"])
        for widget in (self.open_button, self.save_button, *self.setting_widgets):
            widget.state(["disabled"] if busy else ["!disabled"])
        if self.forever.get():
            self.loop_input.state(["disabled"])
        if self.restore_when_idle and not busy:
            self._restore()
            self.restore_when_idle = False
        if self.core.hook_error and not self.hook_error_shown:
            self.hook_error_shown = True
            self._restore()
            messagebox.showerror("Input hooks หยุดทำงาน", str(self.core.hook_error), parent=self.root)
        self.root.after(100, self.poll)


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
            report = {
                "frozen": bool(getattr(sys, "frozen", False)),
                "gui_ready": bool(root.winfo_exists()),
                "tcl_version": str(root.tk.call("info", "patchlevel")),
                "tk_version": str(root.tk.call("package", "provide", "Tk")),
                "hooks_ready": core.hook_thread.is_alive() and core.hook_error is None,
                "recording": core.recording,
                "playback_started": core.player is not None,
            }
            core.shutdown()
            report["hooks_stopped"] = not core.hook_thread.is_alive()
            report["passed"] = (report["gui_ready"] and report["hooks_ready"]
                                and report["hooks_stopped"] and not report["recording"]
                                and not report["playback_started"])
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
