"""
Small Mission: Capturing and Replaying OS Input Events via System Hooks
-------------------------------------------------------------------
Windows only. Pure Python + ctypes (no third-party libraries), so every
OS call (SetWindowsHookExW, GetMessageW, SendInput, timeBeginPeriod ...)
is visible in the source and can be explained in the report.

Hotkeys (work anywhere, even when this window is not focused):
    F9   start / stop recording
    F10  play / stop playback
    F8   save macro to file
    F11  stop recording / playback
    F12  quit

Threads:
    hook thread   - installs low-level hooks and runs a Win32 message loop
    replay thread - pulls events from the macro and injects them with SendInput
    main thread   - receives hotkey commands and controls the other two
"""

import argparse
from collections import deque
import ctypes
import itertools
import json
import math
import os
import queue
import statistics
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path

if sys.platform != "win32":
    sys.exit("Small Mission uses Win32 system hooks and runs on Windows only.")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winmm = ctypes.WinDLL("winmm")

# ---------------------------------------------------------------- Win32 types
ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

WH_KEYBOARD_LL, WH_MOUSE_LL, HC_ACTION = 13, 14, 0
WM_QUIT = 0x0012
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
WM_MOUSEMOVE, WM_MOUSEWHEEL = 0x0200, 0x020A
MOUSE_BUTTON_MSG = {  # message -> (button, is_down)
    0x0201: ("left", True), 0x0202: ("left", False),
    0x0204: ("right", True), 0x0205: ("right", False),
    0x0207: ("middle", True), 0x0208: ("middle", False),
}
LLKHF_EXTENDED, LLKHF_INJECTED, LLMHF_INJECTED = 0x01, 0x10, 0x01

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
MOUSEEVENTF_MOVE, MOUSEEVENTF_WHEEL = 0x0001, 0x0800
MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
MOUSE_BUTTON_FLAG = {
    ("left", True): 0x0002, ("left", False): 0x0004,
    ("right", True): 0x0008, ("right", False): 0x0010,
    ("middle", True): 0x0020, ("middle", False): 0x0040,
}
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

HOTKEYS = {0x78: "record", 0x79: "play", 0x77: "save", 0x7A: "stop", 0x7B: "quit"}
MAX_LOG_EVENTS = 100_000


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HANDLE
user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


# ---------------------------------------------------------------- data model
@dataclass
class Event:
    t: float            # seconds since recording started
    kind: str           # "key" | "move" | "button" | "wheel"
    down: bool = False
    vk: int = 0
    scan: int = 0
    extended: bool = False
    x: int = 0
    y: int = 0
    button: str = ""
    delta: int = 0


# ---------------------------------------------------------------- core
class MacroCore:
    def __init__(self, args, output=print):
        self.args = args
        self.output = output
        self.messages: "queue.Queue[str]" = queue.Queue(maxsize=200)
        self.events: list[Event] = []
        self.lock = threading.Lock()          # protects self.events (shared buffer)
        self.recording = False
        self.t0 = 0.0
        self.commands: "queue.Queue[str]" = queue.Queue()
        self.player: threading.Thread | None = None
        self.stop_play = threading.Event()
        self.hook_thread_id = 0
        self.hook_error: Exception | None = None
        self.hook_thread: threading.Thread | None = None
        self._hotkeys_down: set[int] = set()
        # keep references, otherwise Python garbage-collects the callbacks
        self._kb_proc = HOOKPROC(self._keyboard_hook)
        self._ms_proc = HOOKPROC(self._mouse_hook)

    def _notify(self, message):
        if self.output is not None:
            self.output(message)
        try:
            self.messages.put_nowait(message)
        except queue.Full:
            pass

    # ------------------------------------------------ hook thread (capture)
    def _add(self, ev: Event):
        with self.lock:
            if self.recording:
                self.events.append(ev)

    def _now(self) -> float:
        return time.perf_counter() - self.t0

    def _keyboard_hook(self, n_code, w_param, l_param):
        if n_code == HC_ACTION:
            kb = KBDLLHOOKSTRUCT.from_address(l_param)
            down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
            if not kb.flags & LLKHF_INJECTED:      # ignore our own SendInput events
                if kb.vkCode in HOTKEYS:
                    if down:
                        if kb.vkCode not in self._hotkeys_down:
                            self._hotkeys_down.add(kb.vkCode)
                            self.commands.put(HOTKEYS[kb.vkCode])
                    else:
                        self._hotkeys_down.discard(kb.vkCode)
                    return 1  # reserve control keys; do not send them to the target app
                elif self.recording:
                    self._add(Event(self._now(), "key", down=down, vk=kb.vkCode,
                                    scan=kb.scanCode,
                                    extended=bool(kb.flags & LLKHF_EXTENDED)))
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _mouse_hook(self, n_code, w_param, l_param):
        if n_code == HC_ACTION and self.recording:
            ms = MSLLHOOKSTRUCT.from_address(l_param)
            if not ms.flags & LLMHF_INJECTED:
                x, y, t = ms.pt.x, ms.pt.y, self._now()
                if w_param == WM_MOUSEMOVE:
                    self._add(Event(t, "move", x=x, y=y))
                elif w_param in MOUSE_BUTTON_MSG:
                    button, down = MOUSE_BUTTON_MSG[w_param]
                    self._add(Event(t, "button", down=down, button=button, x=x, y=y))
                elif w_param == WM_MOUSEWHEEL:
                    delta = ctypes.c_short(ms.mouseData >> 16).value
                    self._add(Event(t, "wheel", delta=delta, x=x, y=y))
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _hook_loop(self, ready: threading.Event):
        kb = ms = None
        try:
            self.hook_thread_id = kernel32.GetCurrentThreadId()
            msg = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            hmod = kernel32.GetModuleHandleW(None)
            kb = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, hmod, 0)
            if not kb:
                raise ctypes.WinError(ctypes.get_last_error())
            ms = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, hmod, 0)
            if not ms:
                raise ctypes.WinError(ctypes.get_last_error())
            ready.set()
            # Low-level hooks must pump messages on the installing thread.
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self.hook_error = exc
            self.stop()
            self._notify(f"[hooks] {exc}")
        finally:
            if kb:
                user32.UnhookWindowsHookEx(kb)
            if ms:
                user32.UnhookWindowsHookEx(ms)
            ready.set()

    # ------------------------------------------------ replay thread (inject)
    def _wait_until(self, target: float, strategy=None):
        if (strategy or self.args.wait) == "sleep":  # rely only on the OS scheduler
            remaining = target - time.perf_counter()
            return not self.stop_play.wait(max(remaining, 0))
        while not self.stop_play.is_set():        # hybrid: sleep, then busy-wait
            remaining = target - time.perf_counter()
            if remaining <= 0:
                return True
            if remaining > 0.002:
                if self.stop_play.wait(remaining - 0.002):
                    return False
        return False

    def _to_absolute(self, x: int, y: int) -> tuple[int, int]:
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = max(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) - 1, 1)
        vh = max(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) - 1, 1)
        return (max(0, min(65535, round((x - vx) * 65535 / vw))),
                max(0, min(65535, round((y - vy) * 65535 / vh))))

    def _send(self, ev: Event) -> bool:
        inp = INPUT()
        if ev.kind == "key":
            inp.type = INPUT_KEYBOARD
            flags = (0 if ev.down else KEYEVENTF_KEYUP) | (KEYEVENTF_EXTENDEDKEY if ev.extended else 0)
            inp.ki = KEYBDINPUT(ev.vk, ev.scan, flags, 0, 0)
        else:
            inp.type = INPUT_MOUSE
            ax, ay = self._to_absolute(ev.x, ev.y)
            flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
            data = 0
            if ev.kind == "button":
                flags |= MOUSE_BUTTON_FLAG[(ev.button, ev.down)]
            elif ev.kind == "wheel":
                flags |= MOUSEEVENTF_WHEEL
                data = ev.delta & 0xFFFFFFFF
            inp.mi = MOUSEINPUT(ax, ay, data, flags, 0, 0)
        return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1

    @staticmethod
    def prepare_events(events, speed, mouse_hz):
        """Keep the last move in each time bucket, preserving all other events."""
        if not mouse_hz:
            return list(events)
        result = []
        last_bucket = None
        for ev in events:
            bucket = math.floor(ev.t / speed * mouse_hz) if ev.kind == "move" else None
            if (ev.kind == "move" and result and result[-1].kind == "move"
                    and bucket == last_bucket):
                result[-1] = ev
            else:
                result.append(ev)
            last_bucket = bucket
        return result

    def _release_held(self, keys, buttons):
        failed = 0
        for vk, scan, extended in keys:
            if not self._send(Event(0, "key", vk=vk, scan=scan, extended=extended)):
                failed += 1
        for button in buttons:
            inp = INPUT()
            inp.type = INPUT_MOUSE
            # Release at the current cursor position without moving the mouse.
            inp.mi.dwFlags = MOUSE_BUTTON_FLAG[(button, False)]
            if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
                failed += 1
        keys.clear()
        buttons.clear()
        return failed

    def _play(self, events: list[Event], settings=None):
        # Freeze settings for this run so UI edits cannot alter timing mid-play.
        settings = settings or argparse.Namespace(**vars(self.args))
        log = deque(maxlen=MAX_LOG_EVENTS)
        failed = total = 0
        held_keys, held_buttons = set(), set()
        timer_started = False
        try:
            if self.stop_play.wait(settings.delay):
                return
            if settings.timer_1ms:
                timer_started = winmm.timeBeginPeriod(1) == 0
            loops = itertools.count() if settings.loop == 0 else range(settings.loop)
            for iteration in loops:
                if iteration and self.stop_play.wait(settings.loop_delay):
                    return
                start = time.perf_counter()
                for ev in events:
                    target = start + ev.t / settings.speed
                    if not self._wait_until(target, settings.wait) or self.stop_play.is_set():
                        return
                    actual = time.perf_counter()
                    if not self._send(ev):
                        failed += 1
                        self._notify("[play] stopped: Windows rejected SendInput; "
                                     "check the target application's privilege level")
                        return
                    if ev.kind == "key":
                        key = (ev.vk, ev.scan, ev.extended)
                        if ev.down:
                            held_keys.add(key)
                        else:
                            held_keys.discard(key)
                    elif ev.kind == "button":
                        if ev.down:
                            held_buttons.add(ev.button)
                        else:
                            held_buttons.discard(ev.button)
                    total += 1
                    log.append((ev.kind, (target - start) * 1000, (actual - start) * 1000))
                failed += self._release_held(held_keys, held_buttons)
                if failed:
                    return
        except Exception as exc:
            self._notify(f"[play] error: {exc}")
        finally:
            try:
                failed += self._release_held(held_keys, held_buttons)
            finally:
                if timer_started:
                    winmm.timeEndPeriod(1)
                self._report(log, failed, total, settings)

    def _report(self, log, failed, total, settings):
        self._notify(f"[play] finished: {total} events sent, {failed} failed")
        if not log:
            return
        try:
            with open(settings.drift_log, "w", encoding="utf-8", newline="") as f:
                f.write("index,kind,target_ms,actual_ms,drift_ms\n")
                for i, (kind, tgt, act) in enumerate(log, start=total - len(log)):
                    f.write(f"{i},{kind},{tgt:.3f},{act:.3f},{act - tgt:.3f}\n")
            self._notify(f"[play] per-event log -> {settings.drift_log}")
        except OSError as exc:
            self._notify(f"[play] could not write drift log: {exc}")
        drift = sorted(act - tgt for _, tgt, act in log)
        p95 = drift[math.ceil(len(drift) * 0.95) - 1]
        self._notify(f"[play] drift ms: mean={statistics.mean(drift):.3f}, "
                     f"p95={p95:.3f}, max={drift[-1]:.3f} "
                     f"(last {len(log)} events; measured before SendInput)")

    # ------------------------------------------------ main thread (control)
    def toggle_record(self):
        if self.player and self.player.is_alive():
            self._notify("[rec] stop playback first")
            return
        if not self.recording:
            with self.lock:
                self.events = []
                self.t0 = time.perf_counter()
                self.recording = True
            self._notify("[rec] recording... F9 / F11 to stop")
        else:
            with self.lock:
                self.recording = False
                count = len(self.events)
            self._notify(f"[rec] stopped: {count} events captured")

    def stop(self):
        if self.recording:
            self.toggle_record()
        self.stop_play.set()

    def toggle_play(self):
        if self.recording:
            self._notify("[play] stop recording first")
            return
        if self.player and self.player.is_alive():
            self.stop_play.set()
            self._notify("[play] stopping...")
            return
        with self.lock:
            snapshot = list(self.events)
        if not snapshot:
            self._notify("[play] nothing to play")
            return
        validate_settings(self.args)
        settings = argparse.Namespace(**vars(self.args))
        snapshot = self.prepare_events(snapshot, settings.speed, settings.mouse_hz)
        self.stop_play.clear()
        self.player = threading.Thread(target=self._play, args=(snapshot, settings), daemon=True)
        self.player.start()
        self._notify(f"[play] {len(snapshot)} events at {settings.speed}x, "
                     f"start delay {settings.delay}s ... F10 / F11 to stop")

    def save(self, path=None):
        path = Path(path or self.args.file)
        if path.resolve() == Path(self.args.drift_log).resolve():
            raise ValueError("Macro file and drift log must use different paths")
        with self.lock:
            snapshot = list(self.events)
        data = [asdict(e) for e in snapshot]
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=f".{path.name}.", suffix=".tmp",
                                             delete=False) as f:
                temp_path = Path(f.name)
                json.dump(data, f, indent=1, allow_nan=False)
            os.replace(temp_path, path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
        self.args.file = str(path)
        self._notify(f"[save] {len(data)} events -> {path}")

    def load(self, path=None):
        if self.recording or (self.player and self.player.is_alive()):
            raise ValueError("Stop recording / playback before opening a macro")
        path = Path(path or self.args.file)
        if path.resolve() == Path(self.args.drift_log).resolve():
            raise ValueError("Macro file and drift log must use different paths")
        with path.open(encoding="utf-8") as f:
            events = validate_events(json.load(f))
        with self.lock:
            self.events = events
        self.args.file = str(path)
        self._notify(f"[load] {len(events)} events <- {path}")

    def start(self):
        validate_settings(self.args)
        if self.hook_thread and self.hook_thread.is_alive():
            return
        user32.SetProcessDPIAware()              # use physical pixels, not scaled ones
        self.hook_error = None
        ready = threading.Event()
        self.hook_thread = threading.Thread(target=self._hook_loop, args=(ready,), daemon=True)
        self.hook_thread.start()
        if not ready.wait(5):
            self.shutdown()
            raise RuntimeError("Timed out installing input hooks")
        if self.hook_error:
            self.shutdown()
            raise RuntimeError(f"Could not install hooks: {self.hook_error}")

    def shutdown(self):
        self.stop()
        if self.hook_thread and self.hook_thread.is_alive():
            user32.PostThreadMessageW(self.hook_thread_id, WM_QUIT, 0, 0)
            self.hook_thread.join(timeout=3)
        if self.player and self.player.is_alive():
            self.player.join(timeout=3)

    def run(self):
        if self.args.load:
            self.load()
        self.start()
        self._notify(__doc__.split("Threads:")[0].strip())
        actions = {"record": self.toggle_record, "play": self.toggle_play,
                   "save": self.save, "stop": self.stop}
        try:
            while True:
                try:
                    cmd = self.commands.get(timeout=0.5)
                except queue.Empty:
                    if self.hook_error:
                        break
                    continue
                if cmd == "quit":
                    break
                try:
                    actions[cmd]()
                except (OSError, ValueError) as exc:
                    self._notify(f"[error] {exc}")
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
            self._notify("bye")


def validate_settings(args):
    for name in ("speed", "delay", "loop_delay"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or (name == "speed" and value == 0):
            raise ValueError(f"{name.replace('_', '-')} must be finite and "
                             + ("greater than zero" if name == "speed" else "non-negative"))
    if args.loop < 0:
        raise ValueError("loop must be non-negative (0 = forever)")
    if args.mouse_hz < 0:
        raise ValueError("mouse-hz must be non-negative (0 = original mouse events)")
    if Path(args.file).resolve() == Path(args.drift_log).resolve():
        raise ValueError("macro file and drift log must use different paths")


def validate_events(data):
    if not isinstance(data, list):
        raise ValueError("Macro must be a JSON list of events")
    events, previous = [], 0.0
    for index, item in enumerate(data):
        try:
            if not isinstance(item, dict):
                raise ValueError("event must be an object")
            ev = Event(**item)
            if (type(ev.t) not in (int, float) or not math.isfinite(ev.t)
                    or ev.t < previous):
                raise ValueError("timestamps must be finite, non-negative and ordered")
            if ev.kind not in ("key", "move", "button", "wheel"):
                raise ValueError("unknown event kind")
            if type(ev.down) is not bool or type(ev.extended) is not bool:
                raise ValueError("down and extended must be booleans")
            for name in ("vk", "scan", "x", "y", "delta"):
                if type(getattr(ev, name)) is not int:
                    raise ValueError(f"{name} must be an integer")
            if not (-2**31 <= ev.x < 2**31 and -2**31 <= ev.y < 2**31):
                raise ValueError("coordinates are outside the Win32 LONG range")
            if ev.kind == "key" and not (1 <= ev.vk <= 254 and 0 <= ev.scan <= 65535):
                raise ValueError("invalid virtual key or scan code")
            if ev.kind == "button" and ev.button not in ("left", "right", "middle"):
                raise ValueError("unsupported mouse button")
            if ev.kind == "wheel" and not -32768 <= ev.delta <= 32767:
                raise ValueError("invalid wheel delta")
            previous = ev.t
            events.append(ev)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid event {index + 1}: {exc}") from exc
    return events


def parse_args(argv=None, *, gui=False):
    p = argparse.ArgumentParser(description="Small Mission input recorder / replayer")
    p.add_argument("--file", default="macro.json", help="macro file for save/load")
    p.add_argument("--load", action="store_true", help="load --file at startup")
    p.add_argument("--speed", type=float, default=1.0, help="playback speed multiplier")
    p.add_argument("--loop", type=int, default=1, help="repeat count, 0 = forever")
    p.add_argument("--delay", type=float, default=3 if gui else 0,
                   help="seconds before playback begins")
    p.add_argument("--loop-delay", type=float, default=0, help="seconds between repetitions")
    p.add_argument("--mouse-hz", type=int, default=250,
                   help="limit consecutive mouse moves per second; 0 = original events")
    p.add_argument("--wait", choices=["sleep", "hybrid"], default="hybrid",
                   help="timing strategy used during replay")
    p.add_argument("--timer-1ms", action="store_true",
                   help="call timeBeginPeriod(1) during replay")
    p.add_argument("--drift-log", default="drift_log.csv")
    if gui:
        p.add_argument("--self-test", metavar="REPORT_JSON",
                       help="check GUI and hooks in a hidden window, write a report, and exit")
    args = p.parse_args(argv)
    try:
        validate_settings(args)
    except ValueError as exc:
        p.error(str(exc))
    return args


def main():
    try:
        MacroCore(parse_args()).run()
    except (OSError, ValueError, RuntimeError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
