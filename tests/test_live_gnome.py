"""Opt-in GNOME smoke test using a child-owned GTK 4 window.

Run with AGENT_DESKTOP_LIVE_TEST=1 in a disposable Hermes home after setup and
provider selection. The system /usr/bin/python3 needs PyGObject and GTK 4.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid


SET_BUTTON = "Set test value to 7"
CLOSE_BUTTON = "Close Hermes test window"
DISPLAY_PREFIX = "Test display: "


def run_fixture(title):
    import gi  # pyright: ignore[reportMissingImports]  # The fixture runs with system Python.
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk  # pyright: ignore[reportMissingImports]

    Gtk.init()
    loop = GLib.MainLoop()
    window = Gtk.Window(title=title, default_width=320, default_height=160)
    window.connect("close-request", lambda *_: loop.quit() or False)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    entry = Gtk.Entry()
    entry.set_text("0")
    # An accessible button has a qualified ref; GTK static labels may not.
    display = Gtk.Button(label=DISPLAY_PREFIX + entry.get_text())
    entry.connect("changed", lambda widget: display.set_label(DISPLAY_PREFIX + widget.get_text()))
    button = Gtk.Button(label=SET_BUTTON)
    button.connect("clicked", lambda *_: entry.set_text("7"))
    close = Gtk.Button(label=CLOSE_BUTTON)
    close.connect("clicked", lambda *_: window.close())
    for widget in (entry, display, button, close):
        box.append(widget)
    window.set_child(box)
    window.present()
    loop.run()


@unittest.skipUnless(os.environ.get("AGENT_DESKTOP_LIVE_TEST") == "1", "real GNOME test is opt-in")
class LiveGnomeTests(unittest.TestCase):
    def test_provider_loader_and_gtk_loop(self):
        from plugins.computer_use import get_active_provider
        from tools.computer_use.tool import handle_computer_use, release_computer_use_session

        provider = get_active_provider()
        self.assertEqual(provider.name, "agent-desktop-gnome")
        backend = provider.create_backend(permission_mode="standard")
        backend.start()
        self.addCleanup(backend.stop)
        state = getattr(backend, "_state", None)
        backend_env = getattr(backend, "_env", None)
        if state is None or not isinstance(backend_env, dict):
            self.fail("Provider start did not create private state and a GUI environment")
        state_path = Path(state.name)

        marker = "Hermes GNOME smoke " + uuid.uuid4().hex
        env = {k: v for k, v in backend_env.items() if k != "AGENT_DESKTOP_HOME"}
        with subprocess.Popen(["/usr/bin/python3", str(Path(__file__).resolve()), "--fixture", marker],
                              env=env, stderr=subprocess.PIPE, text=True) as fixture:
            def fixture_windows(windows):
                return [w for w in windows if w.get("pid") == fixture.pid and w.get("title") == marker]

            try:
                deadline = time.monotonic() + 8
                while True:
                    if fixture.poll() is not None:
                        stderr = fixture.stderr.read() if fixture.stderr is not None else ""
                        self.fail("GTK fixture exited before it was visible: " + stderr)
                    windows = fixture_windows(backend.list_windows())
                    if len(windows) == 1:
                        break
                    if time.monotonic() > deadline:
                        self.fail("GTK fixture did not appear as one scoped AT-SPI window")
                    time.sleep(0.1)
                window = windows[0]
                app, window_id = window["app_name"], window["id"]

                # Exercise real model-facing read-only observation and backend discovery.
                wrapper_session = "agent-desktop-live-smoke-" + str(fixture.pid)
                try:
                    listing = json.loads(handle_computer_use({"action": "list_windows"},
                                                             session_id=wrapper_session))
                    targets = fixture_windows(listing["windows"])
                    self.assertEqual(len(targets), 1)
                    self.assertIsInstance(targets[0]["id"], int)
                    wrapped = json.loads(handle_computer_use({"action": "capture", "app": app,
                                                             "pid": fixture.pid, "window_id": targets[0]["id"],
                                                             "mode": "ax"}, session_id=wrapper_session))
                    self.assertEqual(wrapped["mode"], "ax", wrapped)
                    self.assertTrue(wrapped["elements"])
                    self.assertNotIn("screenshot_path", wrapped)
                finally:
                    release_computer_use_session(wrapper_session)

                def capture():
                    return backend.capture(app=app, pid=fixture.pid, window_id=window_id)

                def named(snapshot, label):
                    matches = [e for e in snapshot.elements if e.label.split(" | ", 1)[0] == label]
                    self.assertEqual(len(matches), 1, label)
                    return matches[0]

                def observe_display(value):
                    deadline = time.monotonic() + 5
                    target = DISPLAY_PREFIX + value
                    while True:
                        snap = capture()
                        if any(e.label.split(" | ", 1)[0] == target for e in snap.elements):
                            return snap
                        if time.monotonic() >= deadline:
                            self.fail(f"GTK fixture display did not change to {value!r}")
                        time.sleep(0.1)

                snap = observe_display("0")
                button = named(snap, SET_BUTTON)
                action = backend.click(element=button.index)
                self.assertTrue(action.ok, action.message)
                self.assertEqual(backend.click(element=button.index).code, "stale_ref")

                snap = observe_display("7")
                fields = [e for e in snap.elements if e.role == "textfield" and
                          "SetValue" in e.attributes.get("available_actions", ())]
                self.assertEqual(len(fields), 1)
                action = backend.set_value("42", element=fields[0].index)
                self.assertTrue(action.ok, action.message)
                snap = observe_display("42")

                self.assertEqual(backend.key("ctrl+a").code, "unsupported_action")
                action = backend.click(element=named(snap, CLOSE_BUTTON).index)
                self.assertTrue(action.ok, action.message)
                self.assertEqual(fixture.wait(timeout=5), 0)
                self.assertFalse(fixture_windows(backend.list_windows()))
            finally:
                # Only this test's own child process is eligible for fallback cleanup.
                if fixture.poll() is None:
                    fixture.terminate()
                    try:
                        fixture.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        fixture.kill()
                        fixture.wait(timeout=5)

        backend.stop()
        self.assertFalse(state_path.exists(), "Private snapshots must be removed on stop")
        print(json.dumps({"real_gnome_smoke": "passed", "click_readback": "7",
                          "set_value_readback": "42", "fixture_closed": True,
                          "native_wrapper_capture": "ax, no image"}))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fixture"] and len(sys.argv) == 3:
        run_fixture(sys.argv[2])
    else:
        unittest.main()
