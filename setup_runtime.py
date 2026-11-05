#!/usr/bin/env python3
"""Explicit, one-version upstream runtime setup; never called by register/start.

No shell installer, package manager, self-updater or Hermes config write.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import tarfile
import tempfile
import urllib.request

LOCK_PATH = Path(__file__).with_name("runtime-lock.json")
MAX_DOWNLOAD = 8 * 1024 * 1024
MAX_MEMBER = 16 * 1024 * 1024


def checked_members(payload, lock):
    if len(payload) != lock["size"] or hashlib.sha256(payload).hexdigest() != lock["sha256"]:
        raise RuntimeError("Upstream archive size or SHA256 mismatch; nothing installed")
    result = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive:
            if member.name not in lock["files"] or member.name in result or not member.isfile():
                raise RuntimeError("Archive has an unexpected, duplicate or non-regular member")
            if member.size > MAX_MEMBER or member.size < 1:
                raise RuntimeError("Archive member exceeds the permitted size")
            stream = archive.extractfile(member)
            content = stream.read(MAX_MEMBER + 1)
            if len(content) != member.size or hashlib.sha256(content).hexdigest() != lock["files"][member.name]:
                raise RuntimeError("Upstream archive member SHA256 mismatch")
            result[member.name] = content
    if set(result) != set(lock["files"]):
        raise RuntimeError("Archive does not contain both the executable and its matching helper")
    return result


def safe_directory(home, relative):
    home = Path(home).expanduser().resolve()
    if not home.is_dir() or home.stat().st_uid != os.getuid():
        raise RuntimeError("Hermes home must already exist and belong to the current user")
    current = home
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError("Refusing a symlink in the runtime destination")
        current.mkdir(mode=0o700, exist_ok=True)
        st = current.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise RuntimeError("Runtime destination must be user-owned and not group/world writable")
    return current


def install(home, lock):
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise RuntimeError("This release supports only Linux x86_64")
    parent = safe_directory(home, "plugin-data/agent-desktop-gnome/runtime")
    destination = parent / lock["version"]
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise RuntimeError("Unsafe existing runtime destination")
        for name, digest in lock["files"].items():
            path = destination / name
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError("Existing runtime differs from pin; refusing to overwrite it")
            if path.stat().st_mode & 0o022 or not os.access(path, os.X_OK):
                raise RuntimeError("Existing runtime permissions are unsafe or not executable")
        return destination
    expected = "https://github.com/vercel-labs/agent-desktop/releases/download/v1.2.0/agent-desktop-v1.2.0-x86_64-unknown-linux-gnu.tar.gz"
    if lock["url"] != expected:
        raise RuntimeError("Unrecognized runtime source; only the reviewed official release is allowed")
    with urllib.request.urlopen(lock["url"], timeout=60) as response:
        if not response.geturl().startswith("https://"):
            raise RuntimeError("Refusing a non-HTTPS release redirect")
        payload = response.read(MAX_DOWNLOAD + 1)
    if len(payload) > MAX_DOWNLOAD:
        raise RuntimeError("Release archive exceeds the download limit")
    members = checked_members(payload, lock)
    staging = Path(tempfile.mkdtemp(prefix=".setup-", dir=parent))
    try:
        for name, content in members.items():
            target = staging / name
            with target.open("xb") as output:
                output.write(content)
            target.chmod(0o700)
        # Publish only a completely verified pair, and never replace a populated install.
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path,
                        default=Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")),
                        help="Target profile home; no profile is created and no config is modified")
    args = parser.parse_args()
    try:
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        destination = install(args.hermes_home, lock)
    except (OSError, ValueError, RuntimeError, tarfile.TarError) as exc:
        parser.exit(1, f"Runtime setup failed: {exc}\n")
    print(f"Verified pinned agent-desktop {lock['version']} at {destination}")
    print("Backend selection remains unchanged. Follow README activation steps when ready.")


if __name__ == "__main__":
    main()
