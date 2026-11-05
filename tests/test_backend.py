"""Contract/safety tests. All UI fixtures here are synthetic, not smoke-test evidence."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
backend = importlib.import_module(ROOT.name + ".backend")
runtime = importlib.import_module(ROOT.name + ".runtime")


def envelope(data):
    return subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": data}), "")


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.b = backend.AgentDesktopBackend(binary="/unused/agent-desktop", home=self.temp.name)
        self.b._env = {"PATH": "/usr/bin"}
        seat = patch.object(self.b, "_check_host_seat")
        seat.start()
        self.addCleanup(seat.stop)
        self.win = {"id": "w-opaque-id", "app_name": "calculator", "pid": 123, "title": "Calculator"}
        self.tree = {"role": "window", "children": [
            {"role": "button", "name": "7", "ref_id": "@snapshot:e1", "available_actions": ["Click"]},
            {"role": "textfield", "value": "42", "ref_id": "@snapshot:e2",
             "available_actions": ["SetValue", "TypeText"], "states": ["focused"]},
        ]}

    def capture(self):
        with patch.object(self.b, "_run", side_effect=[[self.win], {
            "window": {"id": "w-opaque-id"}, "tree": self.tree, "complete": True,
        }]):
            return self.b.capture(app="calculator")

    def test_ax_only_capture_preserves_value_and_qualified_ref(self):
        result = self.capture()
        self.assertEqual(result.mode, "ax")
        self.assertIsNone(result.png_b64)
        self.assertEqual(result.elements[1].label, "value=42 | states=focused")
        self.assertEqual(result.elements[0].element_token, "@snapshot:e1")

    def test_indices_never_reused(self):
        first = self.capture().elements[0].index
        second = self.capture().elements[0].index
        self.assertNotEqual(first, second)
        self.assertEqual(self.b.click(element=first).code, "stale_ref")

    def test_capture_requires_explicit_app_and_forgets_old_refs(self):
        index = self.capture().elements[0].index
        with self.assertRaises(backend.DriverError):
            self.b.capture()
        self.assertEqual(self.b.click(element=index).code, "stale_ref")

    def test_vision_never_opens_screenshot_consent(self):
        with patch.object(self.b, "_run") as run:
            with self.assertRaises(backend.DriverError):
                self.b.capture(mode="vision", app="calculator")
            run.assert_not_called()

    def test_ambiguous_window_refuses(self):
        with patch.object(self.b, "_run", return_value=[self.win, dict(self.win, id="other")]):
            with self.assertRaises(backend.DriverError) as caught:
                self.b.capture(app="calculator")
            self.assertEqual(caught.exception.code, "ambiguous_target")

    def test_wrong_window_snapshot_refuses(self):
        with patch.object(self.b, "_run", side_effect=[[self.win], {"window": {"id": "wrong"}, "tree": self.tree}]):
            with self.assertRaises(backend.DriverError):
                self.b.capture(app="calculator")
        self.assertFalse(self.b._elements)

    def test_click_delivered_unverified_is_not_confirmed_and_consumes_refs(self):
        index = self.capture().elements[0].index
        with patch.object(self.b, "_run", return_value={"disposition": {"delivery": "delivered_unverified", "retry": "never"}}) as run:
            result = self.b.click(element=index)
            run.assert_called_once_with("click", "@snapshot:e1", stdin=None)
        self.assertTrue(result.ok)
        self.assertFalse(result.verified)
        self.assertEqual(result.effect, "unverifiable")
        self.assertEqual(self.b.click(element=index).code, "stale_ref")

    def test_reject_pointer_foreground_doubleclick_and_modifiers(self):
        index = self.capture().elements[0].index
        for extra in ({"x": 1}, {"y": 1}, {"click_count": 2}, {"bring_to_front": True},
                      {"modifiers": ["ctrl"]}, {"delivery_mode": "foreground"}, {"button": "middle"}):
            with self.subTest(extra=extra), patch.object(self.b, "_run") as run:
                self.assertEqual(self.b.click(element=index, **extra).code, "unsupported_action")
                run.assert_not_called()

    def test_reject_key_drag_scroll_focus(self):
        for action in (lambda: self.b.key("ctrl+a"), self.b.drag,
                       lambda: self.b.scroll(direction="down"), lambda: self.b.focus_app("calculator")):
            with patch.object(self.b, "_run") as run:
                self.assertEqual(action().code, "unsupported_action")
                run.assert_not_called()

    def test_text_passed_by_stdin_not_argv(self):
        self.capture()
        with patch("subprocess.run", return_value=envelope({"disposition": {"delivery": "delivered_unverified"}})) as run:
            self.assertTrue(self.b.type_text("private text").ok)
            args, kwargs = run.call_args
            self.assertNotIn("private text", args[0])
            self.assertIn("--stdin", args[0])
            self.assertEqual(kwargs["input"], "private text")
            self.assertNotIn("shell", kwargs)

    def test_type_without_unique_focused_field_refuses(self):
        self.capture()
        self.tree["children"][1]["states"] = []
        with patch.object(self.b, "_run") as run:
            self.assertFalse(self.b.type_text("x").ok)
            run.assert_not_called()

    def test_password_refused(self):
        self.tree["children"][1]["role"] = "password"
        field = self.capture().elements[1].index
        with patch.object(self.b, "_run") as run:
            self.assertFalse(self.b.set_value("secret", element=field).ok)
            run.assert_not_called()

    def test_upstream_secure_textfield_refuses_type_and_set_value(self):
        self.tree["children"][1]["states"] = ["focused", "secure"]
        field = self.capture().elements[1].index
        with patch.object(self.b, "_run") as run:
            self.assertFalse(self.b.set_value("synthetic secret", element=field).ok)
            self.assertFalse(self.b.type_text("synthetic secret").ok)
            run.assert_not_called()

    def test_secure_field_capture_withholds_content(self):
        node = self.tree["children"][1]
        node.update(states=["secure"], name="sensitive-name", value="sensitive-value",
                    description="sensitive-description")
        field = self.capture().elements[1]
        self.assertNotIn("sensitive", field.label)
        self.assertIn("content withheld", field.label)

    def test_same_process_multiwindow_uses_integer_handles(self):
        second = dict(self.win, id="second-native-window")
        with patch.object(self.b, "_run", return_value=[self.win, second]):
            rows = self.b.list_windows()
        handle = rows[1]["id"]
        self.assertIsInstance(handle, int)
        with patch.object(self.b, "_run", side_effect=[[self.win, second], {
            "window": {"id": "second-native-window"}, "tree": self.tree, "complete": True,
        }]) as run:
            self.b.capture(app="calculator", pid=123, window_id=handle)
            self.assertIn("second-native-window", run.call_args.args)

    def test_window_handles_stable_then_never_reused(self):
        with patch.object(self.b, "_run", side_effect=[[self.win], [self.win], [], [self.win]]):
            first = self.b.list_windows()[0]["id"]
            self.assertEqual(self.b.list_windows()[0]["id"], first)
            self.assertEqual(self.b.list_windows(), [])
            self.assertNotEqual(self.b.list_windows()[0]["id"], first)

    def test_opaque_or_boolean_window_target_refused(self):
        for target in ("w-opaque-id", True):
            with self.subTest(target=target), patch.object(self.b, "_run", return_value=[self.win]):
                with self.assertRaises(backend.DriverError):
                    self.b.capture(app="calculator", window_id=target)

    def test_timeout_is_unknown_and_no_retry(self):
        index = self.capture().elements[0].index
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("agent-desktop", 20)):
            result = self.b.click(element=index)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "delivery_unknown")
        self.assertEqual(result.meta["retry"], "never")
        self.assertFalse(self.b._elements)

    def test_driver_refusal_preserves_disposition(self):
        index = self.capture().elements[0].index
        reply = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "error": {
            "code": "STALE_REF", "message": "gone", "disposition": {"retry": "safe"}}}), "")
        with patch("subprocess.run", return_value=reply):
            result = self.b.click(element=index)
        self.assertEqual(result.code, "STALE_REF")
        self.assertEqual(result.meta["disposition"]["retry"], "safe")

    def test_invalid_json_does_not_leak_stdout(self):
        reply = subprocess.CompletedProcess([], 1, "sensitive UI debug", "private stderr")
        with patch("subprocess.run", return_value=reply):
            with self.assertRaises(backend.DriverError) as caught:
                self.b._run("click", "@snapshot:e1")
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.b = backend.AgentDesktopBackend(binary="/unused", home=self.temp.name)

    def check(self, where, published):
        placement = SimpleNamespace(GATEWAY="gateway", resolve=lambda: SimpleNamespace(where=where))
        runtime = SimpleNamespace(published_env=lambda: published)
        return self.b._check_host_seat(placement=placement, runtime=runtime)

    def test_human_host_allowed(self):
        self.check("gateway", {})

    def test_terminal_sandbox_refused(self):
        with self.assertRaises(backend.DriverError):
            self.check("terminal", {})

    def test_policy_refusal_not_host_fallback(self):
        with self.assertRaises(backend.DriverError):
            self.check("refused", {})

    def test_running_bot_desktop_refused(self):
        with self.assertRaises(backend.DriverError):
            self.check("gateway", {"DISPLAY": ":99"})

    def test_persisted_unknown_owner_refuses_empty_published_environment(self):
        directory = Path(self.temp.name) / "bot-desktop"
        directory.mkdir()
        for marker in ("sandbox.json", "launcher.pid", "env"):
            path = directory / marker
            path.write_text("opaque ownership evidence")
            with self.subTest(marker=marker), self.assertRaises(backend.DriverError):
                self.check("gateway", {})
            path.unlink()

    def test_unreadable_ownership_state_refuses(self):
        with patch.object(Path, "lstat", side_effect=PermissionError("denied")):
            with self.assertRaises(backend.DriverError):
                self.check("gateway", {})

    def test_tampered_runtime_refused_before_execution(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / "agent-desktop"
            file.write_bytes(b"not the pinned runtime")
            file.chmod(0o700)
            b = backend.AgentDesktopBackend(binary=file, home=root)
            with patch("subprocess.run") as run, self.assertRaises(backend.DriverError):
                b.start()
            run.assert_not_called()


class EnvironmentTests(unittest.TestCase):
    def env(self, desktop="GNOME"):
        return {"XDG_CURRENT_DESKTOP": desktop, "WAYLAND_DISPLAY": "wayland-0",
                "XDG_RUNTIME_DIR": self.temp.name, "UNRELATED_SECRET": "never-copy"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_allowlist_no_eval_no_secret_no_trace(self):
        proc = subprocess.CompletedProcess([], 0, json.dumps(self.env()), "")
        with patch.dict(os.environ, {"API_KEY": "secret", "AGENT_DESKTOP_TRACE": "/wrong"}), patch("subprocess.run", return_value=proc):
            env = runtime.session_environment()
        self.assertNotIn("API_KEY", env)
        self.assertNotIn("UNRELATED_SECRET", env)
        self.assertNotIn("AGENT_DESKTOP_TRACE", env)
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")

    def test_non_gnome_refuses(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(self.env("KDE")), "")):
            with self.assertRaises(RuntimeError):
                runtime.session_environment()

    def test_missing_display_refuses(self):
        data = self.env()
        data.pop("WAYLAND_DISPLAY")
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(data), "")):
            with self.assertRaises(RuntimeError):
                runtime.session_environment()


if __name__ == "__main__":
    unittest.main()
