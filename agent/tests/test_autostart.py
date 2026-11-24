import os
import sys
import tempfile
import unittest
import uuid

if sys.platform == "win32":
    import winreg

    from remote_slicing_agent import autostart

DISABLED = bytes([3] + [0] * 11)


@unittest.skipUnless(sys.platform == "win32", "Windows registry")
class StartupTests(unittest.TestCase):
    """Against a throwaway HKCU key, never the real Run key."""

    def setUp(self):
        self.root = r"Software\RemoteSlicingTests\%s" % uuid.uuid4().hex
        self.folder = tempfile.mkdtemp()
        self.lnk = os.path.join(self.folder, "Remote Slicing Agent.lnk")
        self.startup = autostart.Startup(run_key=self.root + r"\Run", approved_key=self.root + r"\Approved",
                                         folder_approved_key=self.root + r"\Folder", shortcut=self.lnk)

    def tearDown(self):
        def delete(path):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                    names = []
                    i = 0
                    while True:
                        try:
                            names.append(winreg.EnumKey(key, i))
                        except OSError:
                            break
                        i += 1
                for name in names:
                    delete(path + "\\" + name)
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except OSError:
                pass
        delete(self.root)
        if os.path.exists(self.lnk):
            os.remove(self.lnk)
        os.rmdir(self.folder)

    def put(self, sub, name, value, kind=None):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.root + "\\" + sub) as key:
            winreg.SetValueEx(key, name, 0, kind or winreg.REG_BINARY, value)

    def get(self, sub, name):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.root + "\\" + sub) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    def test_fresh_is_off(self):
        self.assertFalse(self.startup.enabled())
        self.assertFalse(self.startup.disabled_by_user())

    def test_on_writes_the_run_value_only(self):
        self.startup.enable()
        self.assertTrue(self.startup.enabled())
        self.assertEqual(self.get("Run", "Remote Slicing Agent"), self.startup.command())
        self.assertIsNone(self.get("Approved", "Remote Slicing Agent"))

    def test_the_command_starts_the_launcher_by_full_path(self):
        command = self.startup.command()
        self.assertTrue(command.startswith('"') and command.endswith('agent.pyw" --quiet'), command)
        self.assertTrue(os.path.isfile(autostart.LAUNCHER))

    def test_switched_off_in_task_manager_reads_as_off(self):
        self.startup.enable()
        self.put("Approved", "Remote Slicing Agent", DISABLED)
        self.assertFalse(self.startup.enabled())
        self.assertTrue(self.startup.disabled_by_user())

    def test_turning_on_again_clears_task_managers_flag(self):
        self.startup.enable()
        self.put("Approved", "Remote Slicing Agent", DISABLED)
        self.startup.enable()
        self.assertTrue(self.startup.enabled())
        self.assertEqual(self.get("Approved", "Remote Slicing Agent"), autostart.ENABLED_FLAG)

    def test_an_enabled_flag_is_left_alone(self):
        enabled = bytes([6] + [1] * 11)
        self.put("Approved", "Remote Slicing Agent", enabled)
        self.startup.enable()
        self.assertTrue(self.startup.enabled())
        self.assertEqual(self.get("Approved", "Remote Slicing Agent"), enabled)

    def test_off_removes_both_values_and_nothing_else(self):
        self.put("Run", "Other App", "other.exe", winreg.REG_SZ)
        self.put("Approved", "Other App", DISABLED)
        self.startup.enable()
        self.put("Approved", "Remote Slicing Agent", DISABLED)
        self.startup.disable()
        self.assertIsNone(self.get("Run", "Remote Slicing Agent"))
        self.assertIsNone(self.get("Approved", "Remote Slicing Agent"))
        self.assertEqual(self.get("Run", "Other App"), "other.exe")
        self.assertEqual(self.get("Approved", "Other App"), DISABLED)

    def test_old_shortcut_becomes_the_run_value(self):
        open(self.lnk, "wb").close()
        self.assertEqual(self.startup.migrate_shortcut(), "on")
        self.assertFalse(os.path.exists(self.lnk))
        self.assertTrue(self.startup.enabled())

    def test_old_shortcut_switched_off_in_task_manager_stays_off(self):
        open(self.lnk, "wb").close()
        self.put("Folder", "Remote Slicing Agent.lnk", DISABLED)
        self.assertEqual(self.startup.migrate_shortcut(), "off")
        self.assertFalse(os.path.exists(self.lnk))
        self.assertFalse(self.startup.enabled())
        self.assertIsNone(self.get("Run", "Remote Slicing Agent"))
        self.assertIsNone(self.get("Folder", "Remote Slicing Agent.lnk"))

    def test_nothing_to_migrate(self):
        self.assertIsNone(self.startup.migrate_shortcut())
        self.assertFalse(self.startup.enabled())


if __name__ == "__main__":
    unittest.main()
