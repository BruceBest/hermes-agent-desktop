"""Pinned runtime paths and bounded, allowlisted GNOME session discovery."""
import json
import os
from pathlib import Path
import platform
import subprocess

VERSION = "1.2.0"
SESSION_KEYS = frozenset({
    "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
    "XDG_RUNTIME_DIR", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE",
    "XDG_SESSION_DESKTOP", "DESKTOP_SESSION", "XDG_DATA_DIRS",
})


def driver_path(home):
    return Path(home) / "plugin-data" / "agent-desktop-gnome" / "runtime" / VERSION / "agent-desktop"


def session_environment():
    # Never pass Hermes tokens, AGENT_DESKTOP_TRACE/SESSION or arbitrary loader env to the driver.
    env = {"HOME": str(Path.home()), "PATH": "/usr/local/bin:/usr/bin:/bin"}
    for key in SESSION_KEYS | {"LANG", "LC_ALL", "LC_CTYPE"}:
        if key in os.environ:
            env[key] = os.environ[key]
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "--user", "show-environment", "--output=json"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        values = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Cannot discover the systemd user graphical session") from exc
    if not isinstance(values, dict):
        raise RuntimeError("systemd user environment is not an object")
    for key in SESSION_KEYS:
        env.pop(key, None)
        if isinstance(values.get(key), str):
            env[key] = values[key]
    if "GNOME" not in env.get("XDG_CURRENT_DESKTOP", "").upper().split(":"):
        raise RuntimeError("Only a signed-in GNOME session is supported by this plugin")
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        raise RuntimeError("No display found in the GNOME user session")
    runtime = Path(env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    if not runtime.is_absolute() or runtime.is_symlink() or runtime.stat().st_uid != os.getuid():
        raise RuntimeError("Invalid graphical runtime directory")
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    if platform.machine() not in {"x86_64", "amd64"}:
        raise RuntimeError("This release is tested only on Linux x86_64")
    return env
