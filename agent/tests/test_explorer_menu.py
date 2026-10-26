"""Tests for remote_slicing_agent.explorer_menu (Explorer's "Slice..." submenu)."""

import sys
import unittest
import uuid

from remote_slicing_agent import explorer_menu as em

BASE = r"Software\Classes\SystemFileAssociations"


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.out = dict(em.entries(r"C:\Py\pythonw.exe", r"B:\RS\agent\quick_slice.pyw", r"C:\Cura\UltiMaker-Cura.exe,0"))

    def test_one_cascading_menu_per_extension(self):
        for ext in (".stl", ".sldprt"):
            menu = r"%s\%s\shell\RemoteSlicing" % (BASE, ext)
            self.assertEqual(self.out[menu], {"MUIVerb": "Slice...", "SubCommands": "",
                                              "Icon": r"C:\Cura\UltiMaker-Cura.exe,0"})
        self.assertEqual(len(self.out), 2 * (1 + 2 * 11))

    def test_submenu_entries_in_order(self):
        prefix = r"%s\.stl\shell\RemoteSlicing\shell" % BASE
        keys = sorted(k for k in self.out if k.startswith(prefix) and not k.endswith("command"))
        labels = [self.out[k]["MUIVerb"] for k in keys]
        self.assertEqual(labels, ["and upload (center)"] + ["upload and print (%s)" % p for p in (
            "top-left", "top-mid", "top-right", "mid-left", "center", "mid-right", "btm-left", "btm-mid",
            "btm-right")] + ["more options"])
        self.assertEqual(self.out[keys[0]]["CommandFlags"], 0x40)    # separator after "and upload"
        self.assertEqual(self.out[keys[-1]]["CommandFlags"], 0x20)   # separator before "more options"
        self.assertNotIn("CommandFlags", self.out[keys[1]])

    def test_commands(self):
        prefix = r"%s\.sldprt\shell\RemoteSlicing\shell" % BASE
        launch = r'"C:\Py\pythonw.exe" "B:\RS\agent\quick_slice.pyw" --gather '
        self.assertEqual(self.out[prefix + r"\01upload\command"][""], launch + '--place center "%1"')
        self.assertEqual(self.out[prefix + r"\02print-top-left\command"][""],
                         launch + '--print --place top-left "%1"')
        self.assertEqual(self.out[prefix + r"\10print-btm-right\command"][""],
                         launch + '--print --place btm-right "%1"')
        self.assertEqual(self.out[prefix + r"\11options\command"][""], launch + '--options "%1"')

    def test_commands_parse_as_quick_slice_arguments(self):
        import argparse
        import shlex
        from remote_slicing_agent import plate_layout
        parser = argparse.ArgumentParser()
        parser.add_argument("--print", dest="start_print", action="store_true")
        parser.add_argument("--place", choices=plate_layout.POSITIONS, default="center")
        parser.add_argument("--options", action="store_true")
        parser.add_argument("--gather", action="store_true")
        parser.add_argument("files", nargs="+")
        for path, values in self.out.items():
            if path.endswith("command"):
                args = shlex.split(values[""].replace("%1", r"C:\My Parts\a b.stl"), posix=False)[2:]
                parsed = parser.parse_args([a.strip('"') for a in args])
                self.assertTrue(parsed.gather)
                self.assertEqual(parsed.files, [r"C:\My Parts\a b.stl"])

    def test_no_icon_value_without_cura(self):
        self.assertNotIn("Icon", em.entries("p", "l", "")[0][1])


@unittest.skipUnless(sys.platform == "win32", "Windows registry")
class RegistryTests(unittest.TestCase):
    def setUp(self):
        import winreg
        self.winreg = winreg
        self.root = "Software\\RemoteSlicingTest-%s" % uuid.uuid4().hex[:8]
        self.classes = self.root + "\\Classes"
        self.addCleanup(em._delete_tree, winreg, self.root)

    def test_install_replaces_the_old_flat_entries_and_uninstall_removes_everything(self):
        winreg = self.winreg
        old = "%s\\SystemFileAssociations\\.stl\\shell\\RemoteSlicing1Upload\\command" % self.classes
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, old, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "old")
        written = em.install(self.classes)
        self.assertEqual(len(em.installed(self.classes)), 22)
        with self.assertRaises(FileNotFoundError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, old)
        for path, values in written:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                for name, data in values.items():
                    value, kind = winreg.QueryValueEx(key, name)
                    self.assertEqual(value, data)
                    self.assertEqual(kind, winreg.REG_DWORD if isinstance(data, int) else winreg.REG_SZ)
        self.assertEqual(em.uninstall(self.classes), 48)  # with each menu's "shell" container key
        self.assertEqual(em.installed(self.classes), {})
        self.assertEqual(em.uninstall(self.classes), 0)


if __name__ == "__main__":
    unittest.main()
