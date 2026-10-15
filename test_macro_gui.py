"""Hidden-window GUI tests. No hooks or real input injection are used."""

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import macrocore as mc
from macro_gui import MacroApp


class MacroGuiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tcl/Tk unavailable in this environment: {exc}")
        self.root.withdraw()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        args = mc.parse_args([], gui=True)
        args.file = str(Path(self.directory.name) / "macro.json")
        args.drift_log = str(Path(self.directory.name) / "drift.csv")
        self.core = mc.MacroCore(args, output=None)
        self.app = MacroApp(self.root, self.core)
        self.app.minimize.set(False)
        self.enterContext(patch.object(mc.user32, "SendInput", return_value=1))
        self.enterContext(patch.object(self.app, "_restore"))

    def tearDown(self):
        if not hasattr(self, "app"):
            return
        self.app.closed = True
        self.core.stop()
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()

    def test_empty_state_disables_play_and_stop(self):
        self.assertTrue(self.app.play_button.instate(["disabled"]))
        self.assertTrue(self.app.stop_button.instate(["disabled"]))
        self.assertFalse(self.app.record_button.instate(["disabled"]))
        self.assertEqual(self.core.args.delay, 3)

    def test_record_hotkey_and_emergency_stop_update_state(self):
        self.core.commands.put("record")
        self.app.poll()
        self.assertTrue(self.core.recording)
        self.assertTrue(self.app.dirty)
        self.assertTrue(self.app.open_button.instate(["disabled"]))
        self.core.commands.put("stop")
        self.app.poll()
        self.assertFalse(self.core.recording)
        self.assertFalse(self.app.open_button.instate(["disabled"]))

    def test_cancel_discard_keeps_current_recording(self):
        original = [mc.Event(0, "move", x=123)]
        self.core.events = original
        self.app.dirty = True
        with patch("macro_gui.messagebox.askyesnocancel", return_value=None):
            self.app.record()
        self.assertIs(self.core.events, original)
        self.assertFalse(self.core.recording)

    def test_invalid_settings_do_not_partially_update_core(self):
        original = self.core.args
        self.app.speed.set("2")
        self.app.delay.set("nan")
        with self.assertRaises(ValueError):
            self.app._settings()
        self.assertIs(self.core.args, original)
        self.assertEqual(self.core.args.speed, 1)

    def test_infinite_loop_setting_uses_zero_and_disables_count(self):
        self.app.forever.set(True)
        self.app._settings()
        self.app.poll()
        self.assertEqual(self.core.args.loop, 0)
        self.assertTrue(self.app.loop_input.instate(["disabled"]))

    def test_save_dialog_cancel_preserves_unsaved_state(self):
        self.app.dirty = True
        with patch("macro_gui.filedialog.asksaveasfilename", return_value=""):
            self.assertFalse(self.app.save_file())
        self.assertTrue(self.app.dirty)
        self.assertFalse(Path(self.core.args.file).exists())

    def test_window_restores_even_when_play_finishes_between_polls(self):
        self.app.restore_when_idle = True
        self.core.player = Mock()
        self.core.player.is_alive.return_value = False
        self.app.poll()
        self.app._restore.assert_called_once()
        self.assertFalse(self.app.restore_when_idle)

    def test_invalid_file_error_keeps_loaded_events(self):
        original = [mc.Event(0, "move")]
        self.core.events = original
        path = Path(self.directory.name) / "invalid.json"
        path.write_text('{"bad": true}', encoding="utf-8")
        with patch("macro_gui.filedialog.askopenfilename", return_value=str(path)):
            with patch("macro_gui.messagebox.showerror") as error:
                self.app.open_file()
        error.assert_called_once()
        self.assertIs(self.core.events, original)


if __name__ == "__main__":
    unittest.main()
