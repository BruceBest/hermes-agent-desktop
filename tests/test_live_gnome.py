"""Explicit opt-in GNOME smoke test. Opens/changes/closes only a test calculator.

Run with AGENT_DESKTOP_LIVE_TEST=1 in a disposable Hermes home after setup and
provider selection. Never runs on CI by default. Existing calculators are refused,
unless AGENT_DESKTOP_SMOKE_PID explicitly identifies a caller-created test instance.
"""
import json
import os
from pathlib import Path
import time
import unittest


@unittest.skipUnless(os.environ.get("AGENT_DESKTOP_LIVE_TEST") == "1", "real GNOME test is opt-in")
class LiveGnomeTests(unittest.TestCase):
    def test_provider_loader_and_calculator_loop(self):
        from plugins.computer_use import get_active_provider
        provider = get_active_provider()
        self.assertEqual(provider.name, "agent-desktop-gnome")
        b = provider.create_backend(permission_mode="standard")
        b.start()
        state_path = Path(b._state.name)
        pid = None
        closed = False
        try:
            existing = [w for w in b.list_windows() if w.get("app_name") == "gnome-calculator"]
            adopted = os.environ.get("AGENT_DESKTOP_SMOKE_PID")
            if existing:
                self.assertIsNotNone(adopted, "Refusing to touch an existing calculator")
                matches = [w for w in existing if w.get("pid") == int(adopted)]
                self.assertEqual(len(matches), 1)
                pid = matches[0]["pid"]
            else:
                launched = b._run("launch", "org.gnome.Calculator", "--no-attach")
                pid = launched["pid"]
            windows = [w for w in b.list_windows() if w.get("pid") == pid]
            self.assertEqual(len(windows), 1)
            window_id = windows[0]["id"]

            # Exercise the actual model-facing wrapper for read-only observation, without
            # patching its backend factory or bypassing its approval logic.
            from tools.computer_use.tool import handle_computer_use, release_computer_use_session
            wrapper_session = "agent-desktop-live-smoke"
            try:
                listing = json.loads(handle_computer_use({"action": "list_windows"},
                                                         session_id=wrapper_session))
                target = next(w for w in listing["windows"] if w.get("pid") == pid)
                self.assertIsInstance(target["id"], int)
                wrapped = handle_computer_use({"action": "capture", "app": "gnome-calculator",
                                               "pid": pid, "window_id": target["id"],
                                               "mode": "ax"}, session_id=wrapper_session)
                wrapped = json.loads(wrapped)
                self.assertEqual(wrapped["mode"], "ax", wrapped)
                self.assertTrue(wrapped["elements"])
                self.assertNotIn("screenshot_path", wrapped)
            finally:
                release_computer_use_session(wrapper_session)

            def capture():
                return b.capture(app="gnome-calculator", pid=pid, window_id=window_id)

            def by_native(result, name):
                return next(e for e in result.elements if e.attributes.get("native_id", {}).get("value") == name)

            def read_display(result):
                field = by_native(result, "source_view")
                value = b._run("get", field.element_token, "--property", "text")
                return str(value.get("value", value.get("text", ""))) if isinstance(value, dict) else str(value)

            snap = capture()
            clear = by_native(snap, "calc_clear_button")
            self.assertTrue(b.click(element=clear.index).ok)
            snap = capture()
            seven = by_native(snap, "calc_7_button")
            action = b.click(element=seven.index)
            self.assertTrue(action.ok, action.message)
            self.assertEqual(b.click(element=seven.index).code, "stale_ref")
            snap = capture()
            self.assertEqual(read_display(snap), "7")
            field = by_native(snap, "source_view")
            action = b.set_value("42", element=field.index)
            self.assertTrue(action.ok, action.message)
            snap = capture()
            self.assertEqual(read_display(snap), "42")
            if "focused" in by_native(snap, "source_view").attributes.get("states", []):
                action = b.type_text("3")
                self.assertTrue(action.ok, action.message)
                snap = capture()
                self.assertIn("3", read_display(snap))
            self.assertEqual(b.key("ctrl+a").code, "unsupported_action")
            snap = capture()
            close = next(e for e in snap.elements if e.role == "button" and
                         (e.label.startswith("关闭") or e.label.startswith("Close")))
            action = b.click(element=close.index)
            self.assertTrue(action.ok, action.message)
            deadline = time.monotonic() + 5
            while any(w.get("pid") == pid for w in b.list_windows()) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(any(w.get("pid") == pid for w in b.list_windows()))
            closed = True
            print(json.dumps({"real_gnome_smoke": "passed", "click_readback": "7",
                              "set_value_readback": "42", "calculator_closed": True,
                              "native_wrapper_capture": "ax, no image"}))
        finally:
            if pid and not closed:
                # Only the exact test process is eligible; no global key/focus/kill fallback.
                try:
                    snap = b.capture(app="gnome-calculator", pid=pid)
                    close = next(e for e in snap.elements if e.role == "button" and
                                 (e.label.startswith("关闭") or e.label.startswith("Close")))
                    b.click(element=close.index)
                except Exception:
                    print(f"Test calculator PID {pid} may require manual cleanup")
            b.stop()
        self.assertFalse(state_path.exists(), "Private snapshots must be removed on stop")


if __name__ == "__main__":
    unittest.main()
