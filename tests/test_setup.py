"""Archive-verification tests use intentionally synthetic archives, not release evidence."""
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
setup = importlib.import_module(ROOT.name + ".setup_runtime")


def fixture(name="agent-desktop", kind=None):
    content = b"synthetic fixture, not a real executable"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        if kind:
            member.type = kind
            member.linkname = "/etc/passwd"
        archive.addfile(member, io.BytesIO(content))
    payload = output.getvalue()
    lock = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "files": {"agent-desktop": hashlib.sha256(content).hexdigest()}}
    return payload, lock


class SetupTests(unittest.TestCase):
    def test_valid_archive(self):
        payload, lock = fixture()
        self.assertEqual(set(setup.checked_members(payload, lock)), {"agent-desktop"})

    def test_wrong_archive_digest(self):
        payload, lock = fixture()
        lock["sha256"] = "0" * 64
        with self.assertRaises(RuntimeError):
            setup.checked_members(payload, lock)

    def test_path_traversal_refused(self):
        payload, lock = fixture("../agent-desktop")
        with self.assertRaises(RuntimeError):
            setup.checked_members(payload, lock)

    def test_symlink_refused(self):
        payload, lock = fixture(kind=tarfile.SYMTYPE)
        with self.assertRaises(RuntimeError):
            setup.checked_members(payload, lock)

    def test_wrong_member_digest(self):
        payload, lock = fixture()
        lock["files"]["agent-desktop"] = "f" * 64
        with self.assertRaises(RuntimeError):
            setup.checked_members(payload, lock)

    def test_missing_helper_refused(self):
        payload, lock = fixture()
        lock["files"]["agent-desktop-linux-clipboard-owner"] = "f" * 64
        with self.assertRaises(RuntimeError):
            setup.checked_members(payload, lock)

    def test_symlink_destination_refused(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "plugin-data").symlink_to(Path(root))
            with self.assertRaises(RuntimeError):
                setup.safe_directory(root, "plugin-data/agent-desktop-gnome/runtime")

    def test_group_writable_destination_refused(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "plugin-data"
            target.mkdir()
            target.chmod(0o770)
            with self.assertRaises(RuntimeError):
                setup.safe_directory(root, "plugin-data/agent-desktop-gnome/runtime")

    def test_lock_matches_supported_version_and_official_source(self):
        lock = json.loads(setup.LOCK_PATH.read_text())
        self.assertEqual(lock["version"], "1.2.0")
        self.assertTrue(lock["url"].startswith("https://github.com/vercel-labs/agent-desktop/releases/"))
        self.assertEqual(len(lock["source_commit"]), 40)
        self.assertEqual(set(lock["files"]), {"agent-desktop", "agent-desktop-linux-clipboard-owner"})


if __name__ == "__main__":
    unittest.main()
