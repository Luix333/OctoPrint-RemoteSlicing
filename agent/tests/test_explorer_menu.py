"""Tests for remote_slicing_agent.explorer_menu (Explorer right-click entries)."""

import sys
import unittest
import uuid

from remote_slicing_agent import explorer_menu as em


class EntryTests(unittest.TestCase):
    def test_two_verbs_per_extension_with_quoted_commands(self):
        out = dict(em.entries(r"C:\Py\pythonw.exe", r"B:\RS\agent\quick_slice.pyw", r"C:\Cura\UltiMaker-Cura.exe,0"))
        base = r"Software\Classes\SystemFileAssociations"
        for ext in (".stl", ".sldprt"):
            upload = r"%s\%s\shell\RemoteSlicing1Upload" % (base, ext)
            print_ = r"%s\%s\shell\RemoteSlicing2Print" % (base, ext)
            self.assertEqual(out[upload], {"MUIVerb": "Slice and upload", "Icon": r"C:\Cura\UltiMaker-Cura.exe,0"})
            self.assertEqual(out[print_]["MUIVerb"], "Slice, upload && print")  # && shows as &
            self.assertEqual(out[upload + r"\command"][""],
                             r'"C:\Py\pythonw.exe" "B:\RS\agent\quick_slice.pyw" "%1"')
            self.assertEqual(out[print_ + r"\command"][""],
                             r'"C:\Py\pythonw.exe" "B:\RS\agent\quick_slice.pyw" --print "%1"')
        self.assertEqual(len(out), 8)

    def test_no_icon_value_without_cura(self):
        self.assertNotIn("Icon", em.entries("p", "l", "")[0][1])

    def test_command_line_parses_back_to_the_quick_slice_arguments(self):
        from remote_slicing_agent import quick_slice
        command = dict(em.entries("pythonw", "launcher", ""))[
            r"Software\Classes\SystemFileAssociations\.stl\shell\RemoteSlicing2Print\command"][""]
        args = command.replace('"%1"', '"C:\\My Parts\\a b.stl"').split('" "', 1)[1]
        self.assertTrue(args.startswith('launcher" --print "C:\\My Parts\\a b.stl"'))
        self.assertTrue(callable(quick_slice.main))


@unittest.skipUnless(sys.platform == "win32", "Windows registry")
class RegistryTests(unittest.TestCase):
    def test_install_and_uninstall_round_trip(self):
        import winreg
        root = "Software\\RemoteSlicingTest-%s" % uuid.uuid4().hex[:8]
        classes = root + "\\Classes"
        try:
            written = em.install(classes)
            found = em.installed(classes)
            self.assertEqual(len(found), 4)
            for path, values in written:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                    for name, data in values.items():
                        self.assertEqual(winreg.QueryValueEx(key, name)[0], data)
            self.assertTrue(all(cmd.endswith('"%1"') and "quick_slice.pyw" in cmd for cmd in found.values()))
            self.assertEqual(len(em.uninstall(classes)), 8)
            self.assertEqual(em.installed(classes), {})
            self.assertEqual(em.uninstall(classes), [])  # twice is fine
        finally:
            for sub in ("\\.stl\\shell", "\\.sldprt\\shell", "\\.stl", "\\.sldprt", ""):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, classes + "\\SystemFileAssociations" + sub)
                except OSError:
                    pass
            for path in (classes, root):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
