"""Tests for remote_slicing_agent.dropbox_browse."""

import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from remote_slicing_agent import dropbox_browse
from remote_slicing_agent.dropbox_browse import (
    normalize, _resolve, list_folder, resolve_file, ALLOWED, MAX_ENTRIES,
)


class TestNormalize(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(normalize(""), "")

    def test_slashes_only(self):
        self.assertEqual(normalize("/"), "")
        with self.assertRaises(ValueError):
            normalize("///")

    def test_simple(self):
        self.assertEqual(normalize("a/b"), "a/b")
        self.assertEqual(normalize("a\\b"), "a/b")

    def test_leading_slash(self):
        self.assertEqual(normalize("/a/b"), "a/b")
        self.assertEqual(normalize("\\a\\b"), "a/b")

    def test_dot_parts(self):
        self.assertEqual(normalize("a/./b/c"), "a/b/c")
        with self.assertRaises(ValueError):
            normalize("a/./b/../c")

    def test_unc_raises(self):
        with self.assertRaises(ValueError):
            normalize("//x/y")
        with self.assertRaises(ValueError):
            normalize("\\\\server\\share")

    def test_drive_letter_raises(self):
        with self.assertRaises(ValueError):
            normalize("C:/Windows")

    def test_dotdot_raises(self):
        with self.assertRaises(ValueError):
            normalize("..")
        with self.assertRaises(ValueError):
            normalize("a/../..")


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_root(self):
        self.assertEqual(_resolve(self.root, ""), os.path.realpath(self.root))

    def test_subdir(self):
        subdir = os.path.join(self.root, "sub")
        os.makedirs(subdir)
        self.assertEqual(_resolve(self.root, "sub"), os.path.realpath(subdir))


class TestListFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        # Create directory tree
        os.makedirs(os.path.join(self.root, "a"))
        os.makedirs(os.path.join(self.root, "B"))
        os.makedirs(os.path.join(self.root, ".hidden"))
        # Create files
        self._touch("x.STL", 100)
        self._touch("y.sldprt", 200)
        self._touch("z.txt", 300)
        self._touch("~$lock.sldprt", 10)
        self._touch("v.3mf", 400)
        self._touch("w.3mf", 500)

    def _touch(self, name, size):
        path = os.path.join(self.root, name)
        with open(path, "wb") as f:
            f.write(b"\x00" * size)

    def tearDown(self):
        self.tmp.cleanup()

    def test_root_listing(self):
        result = list_folder(self.root, "")
        self.assertEqual(result["path"], "")
        dir_names = [d["name"] for d in result["dirs"]]
        # Case-insensitive order: a < B
        self.assertEqual(dir_names, ["a", "B"])
        file_names = [f["name"] for f in result["files"]]
        self.assertEqual(file_names, ["x.STL", "y.sldprt"])
        self.assertFalse(result["truncated"])
        # Check sizes
        sizes = {f["name"]: f["size"] for f in result["files"]}
        self.assertEqual(sizes["x.STL"], 100)
        self.assertEqual(sizes["y.sldprt"], 200)

    def test_listing_subdir_variants(self):
        for rel in ("a", "a/", "\\a"):
            with self.subTest(rel=rel):
                result = list_folder(self.root, rel)
                self.assertEqual(result["path"], "a")

    def test_dotdot_raises(self):
        with self.assertRaises(ValueError):
            list_folder(self.root, "..")

    def test_a_slash_dotdot_raises(self):
        with self.assertRaises(ValueError):
            list_folder(self.root, "a/../..")

    def test_c_slash_windows_raises(self):
        with self.assertRaises(ValueError):
            list_folder(self.root, "C:/Windows")

    def test_unc_raises(self):
        with self.assertRaises(ValueError):
            list_folder(self.root, "\\\\server\\share")

    def test_file_path_raises(self):
        with self.assertRaises(ValueError) as cm:
            list_folder(self.root, "x.STL")
        self.assertIn("not a folder", str(cm.exception))

    def test_truncation(self):
        # Create many allowed files
        for i in range(20):
            self._touch(f"model_{i:03d}.stl", 10)
        with patch.object(dropbox_browse, "MAX_ENTRIES", 3):
            result = list_folder(self.root, "")
            self.assertTrue(result["truncated"])
            total = len(result["dirs"]) + len(result["files"])
            self.assertLessEqual(total, 3)


class TestResolveFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        path = os.path.join(self.root, "x.STL")
        with open(path, "wb") as f:
            f.write(b"\x00" * 100)
        path2 = os.path.join(self.root, "z.txt")
        with open(path2, "wb") as f:
            f.write(b"\x00" * 50)

    def tearDown(self):
        self.tmp.cleanup()

    def test_accepts_stl(self):
        result = resolve_file(self.root, "x.STL")
        self.assertEqual(result, os.path.realpath(os.path.join(self.root, "x.STL")))

    def test_rejects_txt(self):
        with self.assertRaises(ValueError) as cm:
            resolve_file(self.root, "z.txt")
        self.assertIn("not a model file", str(cm.exception))

    def test_rejects_missing(self):
        with self.assertRaises(ValueError) as cm:
            resolve_file(self.root, "missing.stl")
        self.assertIn("not a model file", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
