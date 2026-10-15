"""Regression tests. Win32 input injection is mocked; no hooks are installed."""

import ctypes
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import macrocore as mc


class MacroCoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.args = mc.parse_args([])
        self.args.file = str(Path(self.directory.name) / "macro.json")
        self.args.drift_log = str(Path(self.directory.name) / "drift.csv")
        self.core = mc.MacroCore(self.args, output=None)
        self.inject = self.enterContext(patch.object(mc.user32, "SendInput", return_value=1))

    def test_wait_can_be_interrupted_in_both_modes(self):
        for strategy in ("sleep", "hybrid"):
            with self.subTest(strategy=strategy):
                self.args.wait = strategy
                self.core.stop_play.clear()
                result = []
                started = threading.Event()

                def wait():
                    started.set()
                    result.append(self.core._wait_until(time.perf_counter() + 30))

                thread = threading.Thread(target=wait, daemon=True)
                thread.start()
                self.assertTrue(started.wait(1))
                self.core.stop_play.set()
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(result, [False])

    def test_no_input_is_sent_if_cancelled_during_wait(self):
        def cancel(target, strategy=None):
            self.core.stop_play.set()
            return False

        with patch.object(self.core, "_wait_until", side_effect=cancel):
            self.core._play([mc.Event(1, "key", down=True, vk=65)])
        self.inject.assert_not_called()

    def test_cancel_releases_a_held_key(self):
        events = [mc.Event(0, "key", down=True, vk=65), mc.Event(1, "key", vk=65)]
        with patch.object(self.core, "_wait_until", side_effect=[True, False]):
            with patch.object(self.core, "_send", return_value=True) as send:
                self.core._play(events)
        self.assertEqual([call.args[0].down for call in send.call_args_list], [True, False])

    def test_cancel_releases_a_mouse_button_without_moving_cursor(self):
        flags = []

        def capture(count, pointer, size):
            inp = ctypes.cast(pointer, ctypes.POINTER(mc.INPUT)).contents
            flags.append(inp.mi.dwFlags)
            return 1

        self.inject.side_effect = capture
        with patch.object(self.core, "_wait_until", side_effect=[True, False]):
            with patch.object(self.core, "_to_absolute", return_value=(123, 456)):
                self.core._play([mc.Event(0, "button", button="left", down=True),
                                 mc.Event(1, "move")])
        self.assertEqual(flags[-1], mc.MOUSE_BUTTON_FLAG[("left", False)])
        self.assertFalse(flags[-1] & mc.MOUSEEVENTF_MOVE)

    def test_held_keys_are_released_between_loops(self):
        self.args.loop = 2
        with patch.object(self.core, "_wait_until", return_value=True):
            with patch.object(self.core, "_send", return_value=True) as send:
                self.core._play([mc.Event(0, "key", down=True, vk=65)])
        self.assertEqual([call.args[0].down for call in send.call_args_list], [True, False, True, False])

    def test_send_failure_stops_playback_and_releases_previous_key(self):
        events = [mc.Event(0, "key", down=True, vk=65),
                  mc.Event(0, "key", down=True, vk=66), mc.Event(0, "key", vk=66)]
        with patch.object(self.core, "_wait_until", return_value=True):
            with patch.object(self.core, "_send", side_effect=[True, False, True]) as send:
                self.core._play(events)
        self.assertEqual(len(send.call_args_list), 3)
        self.assertEqual(send.call_args_list[-1].args[0], mc.Event(0, "key", vk=65))

    def test_start_countdown_is_cancellable(self):
        self.args.delay = 30
        self.core.stop_play.set()
        self.core._play([mc.Event(0, "key", vk=65)])
        self.inject.assert_not_called()

    def test_hotkey_autorepeat_is_debounced_and_reserved(self):
        keyboard = mc.KBDLLHOOKSTRUCT(vkCode=0x78)
        with patch.object(mc.user32, "CallNextHookEx", return_value=0) as next_hook:
            for msg in (mc.WM_KEYDOWN, mc.WM_KEYDOWN, mc.WM_KEYUP, mc.WM_KEYDOWN):
                self.assertEqual(self.core._keyboard_hook(0, msg, ctypes.addressof(keyboard)), 1)
        self.assertEqual(self.core.commands.qsize(), 2)
        next_hook.assert_not_called()

    def test_injected_hotkey_cannot_trigger_commands(self):
        keyboard = mc.KBDLLHOOKSTRUCT(vkCode=0x78, flags=mc.LLKHF_INJECTED)
        with patch.object(mc.user32, "CallNextHookEx", return_value=7):
            result = self.core._keyboard_hook(0, mc.WM_KEYDOWN, ctypes.addressof(keyboard))
        self.assertEqual(result, 7)
        self.assertTrue(self.core.commands.empty())

    def test_mouse_coalescing_preserves_clicks_keys_and_final_position(self):
        events = [mc.Event(0, "move", x=1), mc.Event(0.001, "move", x=2),
                  mc.Event(0.002, "button", down=True, button="left", x=2),
                  mc.Event(0.0025, "move", x=3), mc.Event(0.003, "move", x=4),
                  mc.Event(0.0035, "key", down=True, vk=65),
                  mc.Event(0.004, "move", x=5)]
        prepared = self.core.prepare_events(events, 1, 250)
        self.assertEqual(prepared, [events[1], events[2], events[4], events[5], events[6]])
        self.assertEqual(self.core.prepare_events(events, 1, 0), events)
        self.assertEqual(len(events), 7)

    def test_invalid_files_leave_current_macro_intact(self):
        original = [mc.Event(0, "move", x=123)]
        self.core.events = original
        for data in ({}, [{"t": -1, "kind": "move"}],
                     [{"t": float("nan"), "kind": "move"}],
                     [{"t": 1, "kind": "move"}, {"t": 0, "kind": "move"}],
                     [{"t": 0, "kind": "button", "button": "unknown"}],
                     [{"t": 0, "kind": "key", "vk": 999}],
                     [{"t": 0, "kind": "move", "x": True}],
                     [{"t": 0, "kind": "move", "x": 2**40}]):
            with self.subTest(data=data):
                Path(self.args.file).write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.core.load()
                self.assertIs(self.core.events, original)

    def test_save_and_load_preserve_legacy_format(self):
        events = [mc.Event(0, "move", x=-100, y=150), mc.Event(1, "key", down=True, vk=65)]
        self.core.events = events
        self.core.save()
        self.assertEqual(json.loads(Path(self.args.file).read_text()), [asdict(e) for e in events])
        self.core.events = []
        self.core.load()
        self.assertEqual(self.core.events, events)

    def test_failed_save_keeps_previous_file_and_cleans_temporary_file(self):
        path = Path(self.args.file)
        path.write_text("previous content", encoding="utf-8")
        self.core.events = [mc.Event(0, "move")]
        with patch.object(mc.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                self.core.save()
        self.assertEqual(path.read_text(), "previous content")
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_invalid_cli_values_are_rejected(self):
        for option, value in (("--speed", "0"), ("--speed", "nan"), ("--speed", "inf"),
                              ("--loop", "-1"), ("--delay", "-1"),
                              ("--loop-delay", "-1"), ("--mouse-hz", "-1")):
            with self.subTest(option=option, value=value), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    mc.parse_args([option, value])
                self.assertEqual(error.exception.code, 2)

    def test_macro_and_log_cannot_share_a_path(self):
        self.args.drift_log = self.args.file
        with self.assertRaises(ValueError):
            mc.validate_settings(self.args)

    def test_partial_hook_installation_is_cleaned_up(self):
        ready = threading.Event()
        with patch.object(mc.user32, "SetWindowsHookExW", side_effect=[123, None]):
            with patch.object(mc.user32, "UnhookWindowsHookEx") as unhook:
                self.core._hook_loop(ready)
        self.assertTrue(ready.is_set())
        self.assertIsNotNone(self.core.hook_error)
        unhook.assert_called_once_with(123)

    def test_log_memory_is_bounded_and_p95_uses_nearest_rank(self):
        events = [mc.Event(0, "key", vk=65) for _ in range(5)]
        with patch.object(mc, "MAX_LOG_EVENTS", 2):
            with patch.object(self.core, "_wait_until", return_value=True):
                with patch.object(self.core, "_report") as report:
                    self.core._play(events)
        log, failed, total, settings = report.call_args.args
        self.assertEqual(len(log), 2)
        self.assertEqual(total, 5)
        self.core._report([("move", 0, 1), ("move", 0, 9)], 0, 2, self.args)
        messages = []
        while not self.core.messages.empty():
            messages.append(self.core.messages.get_nowait())
        self.assertTrue(any("p95=9.000" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
