"""Start with Windows: one value under HKCU\\...\\Run, the way Task Manager and Settings > Apps > Startup expect.

Those two never delete the value to switch an app off: they write a 12-byte flag of the same name under
``Explorer\\StartupApproved\\Run``, odd first byte = disabled. So "enabled" means the Run value exists and that flag
isn't odd, and switching on also clears a disabled flag, or nothing would start at the next logon.

Earlier agents started from a Startup-folder shortcut; ``migrate_shortcut`` turns that into the Run value, keeping
the user's choice (including one made in Task Manager).

From inside the Claude app, HKCU writes stay in the app's private copy of the registry: change the real setting from
a WMI-started process, as the deploy notes say.
"""

import os
import sys
import winreg

RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
APPROVED_FOLDER = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"
NAME = "Remote Slicing Agent"
ENABLED_FLAG = bytes([2] + [0] * 11)  # what Task Manager writes when it enables an app again
AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(AGENT_DIR, "agent.pyw")
STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs",
                           "Startup", "Remote Slicing Agent.lnk")


def pythonw():
    path = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return path if os.path.exists(path) else sys.executable


def _read(key_path, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _delete(key_path, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except OSError:
        pass


def _switched_off(flag):
    return isinstance(flag, bytes) and len(flag) > 0 and flag[0] % 2 == 1


class Startup:
    def __init__(self, name=NAME, run_key=RUN, approved_key=APPROVED, folder_approved_key=APPROVED_FOLDER,
                 shortcut=STARTUP_LNK):
        self.name, self.run_key, self.approved_key = name, run_key, approved_key
        self.folder_approved_key, self.shortcut = folder_approved_key, shortcut

    def command(self):
        """No working directory comes with a Run value, so it starts the launcher script by its full path."""
        return '"%s" "%s" --quiet' % (pythonw(), LAUNCHER)

    def enabled(self):
        return _read(self.run_key, self.name) is not None and not self.disabled_by_user()

    def disabled_by_user(self):
        """Switched off in Task Manager or Settings."""
        return _switched_off(_read(self.approved_key, self.name))

    def enable(self):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.run_key) as key:
            winreg.SetValueEx(key, self.name, 0, winreg.REG_SZ, self.command())
        if self.disabled_by_user():
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.approved_key) as key:
                winreg.SetValueEx(key, self.name, 0, winreg.REG_BINARY, ENABLED_FLAG)

    def disable(self):
        _delete(self.run_key, self.name)
        _delete(self.approved_key, self.name)

    def set(self, enabled):
        self.enable() if enabled else self.disable()

    def migrate_shortcut(self):
        """Replace the old Startup-folder shortcut with the Run value; returns what was done, or None."""
        if not os.path.exists(self.shortcut):
            return None
        on = not _switched_off(_read(self.folder_approved_key, os.path.basename(self.shortcut)))
        if on:
            self.enable()
        os.remove(self.shortcut)
        _delete(self.folder_approved_key, os.path.basename(self.shortcut))
        return "on" if on else "off"
