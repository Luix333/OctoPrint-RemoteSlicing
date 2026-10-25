"""Explorer's right-click entries for .stl and .sldprt files: "Slice and upload" and "Slice, upload & print".

They are written per user under HKCU\\Software\\Classes\\SystemFileAssociations\\<ext>\\shell, so no admin rights
are needed and they show up whichever program opens those files (Cura, SolidWorks). Each runs
``pythonw agent\\quick_slice.pyw [--print] "<file>"`` (quick_slice.py). On Windows 11 they are in the classic menu,
which is the whole menu when it has been restored, and otherwise under "Show more options".
"""

import ctypes
import json
import os
import sys

from . import config

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(AGENT_DIR, "quick_slice.pyw")
CLASSES = "Software\\Classes"
EXTENSIONS = (".stl", ".sldprt")
# Explorer orders static verbs by key name; "&&" is a literal "&" in a menu label.
VERBS = (("RemoteSlicing1Upload", "Slice and upload", ""),
         ("RemoteSlicing2Print", "Slice, upload && print", "--print "))


def pythonw():
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return exe if os.path.exists(exe) else sys.executable


def cura_icon():
    """Cura's own icon, from the Cura that wrote the settings snapshot; "" when it can't be found."""
    try:
        with open(config.snapshot_path(), encoding="utf-8") as f:
            engine = json.load(f).get("engine", {}).get("path", "")
    except (OSError, ValueError):
        return ""
    exe = os.path.join(os.path.dirname(engine), "UltiMaker-Cura.exe")
    return exe + ",0" if os.path.isfile(exe) else ""


def entries(python_exe, launcher, icon="", classes=CLASSES):
    """[(key path under HKCU, {value name: string})], verb keys before their command keys."""
    out = []
    for ext in EXTENSIONS:
        for key, label, flag in VERBS:
            verb = "%s\\SystemFileAssociations\\%s\\shell\\%s" % (classes, ext, key)
            values = {"MUIVerb": label}
            if icon:
                values["Icon"] = icon
            out.append((verb, values))
            out.append((verb + "\\command", {"": '"%s" "%s" %s"%%1"' % (python_exe, launcher, flag)}))
    return out


def install(classes=CLASSES):
    import winreg
    written = entries(pythonw(), LAUNCHER, cura_icon(), classes)
    for path, values in written:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            for name, data in values.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, data)
    _refresh_explorer()
    return written


def uninstall(classes=CLASSES):
    """Remove the entries (command keys first: a key with subkeys can't be deleted). Returns the keys removed."""
    import winreg
    removed = []
    for path, _values in reversed(entries("", "", "", classes)):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            removed.append(path)
        except FileNotFoundError:
            pass
    _refresh_explorer()
    return removed


def installed(classes=CLASSES):
    """The command lines currently registered, keyed by key path (empty when not installed)."""
    import winreg
    found = {}
    for path, values in entries("", "", "", classes):
        if "" not in values:
            continue
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                found[path] = winreg.QueryValueEx(key, "")[0]
        except FileNotFoundError:
            pass
    return found


def _refresh_explorer():
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
    except (AttributeError, OSError):
        pass
