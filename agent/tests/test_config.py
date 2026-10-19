"""Tests for remote_slicing_agent.config."""

import json
import os
import tempfile
import unittest

from remote_slicing_agent import config


class TestPaths(unittest.TestCase):
    """Path helpers respect REMOTE_SLICING_HOME."""

    def setUp(self):
        self._orig = os.environ.get("REMOTE_SLICING_HOME")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["REMOTE_SLICING_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        if self._orig is None:
            os.environ.pop("REMOTE_SLICING_HOME", None)
        else:
            os.environ["REMOTE_SLICING_HOME"] = self._orig

    def test_home(self):
        self.assertEqual(config.home(), self.tmp.name)

    def test_snapshot_path(self):
        self.assertEqual(config.snapshot_path(), os.path.join(self.tmp.name, "snapshot.json"))

    def test_config_path(self):
        self.assertEqual(config.config_path(), os.path.join(self.tmp.name, "agent.json"))

    def test_log_path(self):
        self.assertEqual(config.log_path(), os.path.join(self.tmp.name, "agent.log"))

    def test_work_dir(self):
        self.assertEqual(config.work_dir(), os.path.join(self.tmp.name, "work"))


class TestDefaults(unittest.TestCase):

    def setUp(self):
        self._orig = os.environ.get("REMOTE_SLICING_HOME")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["REMOTE_SLICING_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        if self._orig is None:
            os.environ.pop("REMOTE_SLICING_HOME", None)
        else:
            os.environ["REMOTE_SLICING_HOME"] = self._orig

    def test_load_without_file_returns_defaults(self):
        result = config.load()
        self.assertEqual(result["octoprint_url"], "")
        self.assertEqual(result["api_key"], "")
        self.assertEqual(result["heartbeat_seconds"], 60)
        # dropbox_root should be filled with auto-detected value
        self.assertTrue(len(result["dropbox_root"]) > 0)

    def test_save_load_roundtrip(self):
        cfg = {
            "octoprint_url": "http://localhost:8080",
            "api_key": "abc123",
            "dropbox_root": "/my/dropbox",
            "heartbeat_seconds": 30,
        }
        config.save(cfg)
        result = config.load()
        self.assertEqual(result["octoprint_url"], "http://localhost:8080")
        self.assertEqual(result["api_key"], "abc123")
        self.assertEqual(result["dropbox_root"], "/my/dropbox")
        self.assertEqual(result["heartbeat_seconds"], 30)

    def test_trailing_slash_stripped(self):
        cfg = {
            "octoprint_url": "http://localhost:8080/",
            "api_key": "",
            "dropbox_root": "",
            "heartbeat_seconds": 60,
        }
        config.save(cfg)
        result = config.load()
        self.assertEqual(result["octoprint_url"], "http://localhost:8080")

    def test_corrupt_json_returns_defaults(self):
        path = config.config_path()
        with open(path, "w") as f:
            f.write("not json at all {{{")
        result = config.load()
        self.assertEqual(result["octoprint_url"], "")
        self.assertEqual(result["api_key"], "")
        self.assertEqual(result["heartbeat_seconds"], 60)


class TestDefaultDropboxRoot(unittest.TestCase):
    """default_dropbox_root reads info.json from LOCALAPPDATA."""

    def setUp(self):
        self._orig_local = os.environ.get("LOCALAPPDATA")
        self._orig_appdata = os.environ.get("APPDATA")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LOCALAPPDATA"] = self.tmp.name
        # Clear APPDATA so it does not interfere
        os.environ.pop("APPDATA", None)

    def tearDown(self):
        self.tmp.cleanup()
        if self._orig_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._orig_local
        if self._orig_appdata is not None:
            os.environ["APPDATA"] = self._orig_appdata

    def test_personal_path(self):
        """Personal path takes precedence."""
        dropbox_dir = os.path.join(self.tmp.name, "Dropbox")
        os.makedirs(dropbox_dir)
        info = {
            "personal": {"path": "/Users/alice/Dropbox"},
            "business": {"path": "/Network/BusinessDropbox"},
        }
        with open(os.path.join(dropbox_dir, "info.json"), "w") as f:
            json.dump(info, f)
        self.assertEqual(config.default_dropbox_root(), "/Users/alice/Dropbox")

    def test_business_path_when_personal_missing(self):
        """Business path is used when personal key is absent."""
        dropbox_dir = os.path.join(self.tmp.name, "Dropbox")
        os.makedirs(dropbox_dir)
        info = {"business": {"path": "/Network/BusinessDropbox"}}
        with open(os.path.join(dropbox_dir, "info.json"), "w") as f:
            json.dump(info, f)
        self.assertEqual(config.default_dropbox_root(), "/Network/BusinessDropbox")


class TestSnapshotHelpers(unittest.TestCase):

    def setUp(self):
        self._orig = os.environ.get("REMOTE_SLICING_HOME")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["REMOTE_SLICING_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        if self._orig is None:
            os.environ.pop("REMOTE_SLICING_HOME", None)
        else:
            os.environ["REMOTE_SLICING_HOME"] = self._orig

    def test_snapshot_stat_missing(self):
        self.assertIsNone(config.snapshot_stat())

    def test_snapshot_stat_present(self):
        snap = config.snapshot_path()
        with open(snap, "w") as f:
            json.dump({"created": "2024-01-01"}, f)
        result = config.snapshot_stat()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_read_snapshot_summary(self):
        snap = config.snapshot_path()
        data = {
            "created": "2024-01-01T12:00:00",
            "cura_version": "5.0.0",
            "summary": "Test print",
            "bed": "Bambu P1S",
            "resolved": "x" * 1_000_000,  # big payload that must not leak
        }
        with open(snap, "w") as f:
            json.dump(data, f)
        result = config.read_snapshot_summary()
        self.assertEqual(len(result), 4)
        self.assertEqual(result["created"], "2024-01-01T12:00:00")
        self.assertEqual(result["cura_version"], "5.0.0")
        self.assertEqual(result["summary"], "Test print")
        self.assertEqual(result["bed"], "Bambu P1S")
        self.assertNotIn("resolved", result)

    def test_read_snapshot_summary_missing(self):
        self.assertIsNone(config.read_snapshot_summary())

    def test_read_snapshot_summary_corrupt(self):
        snap = config.snapshot_path()
        with open(snap, "w") as f:
            f.write("{corrupt")
        self.assertIsNone(config.read_snapshot_summary())


if __name__ == "__main__":
    unittest.main()
