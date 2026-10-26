"""Explorer's right-click menu for .stl and .sldprt files: "Slice..." with a submenu.

    Slice...
        and upload (center)
        ------------------------------
        upload and print (top-left) ... upload and print (btm-right)   (nine spots on the bed)
        ------------------------------
        more options

The entries are written per user under HKCU\\Software\\Classes\\SystemFileAssociations\\<ext>\\shell, so no admin
rights are needed and they show up whichever program opens those files (Cura, SolidWorks). Each runs
``pythonw agent\\quick_slice.pyw --gather ... "<file>"`` (quick_slice.py); Explorer starts it once per selected
file and explorer_batch.py joins a multi-file selection into one job. On Windows 11 they are in the classic menu,
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
MENU_KEY = "RemoteSlicing"
MENU_LABEL = "Slice..."
SEPARATOR_BEFORE, SEPARATOR_AFTER = 0x20, 0x40  # EXPCMDFLAGS, as the CommandFlags value of a submenu entry
_PLACES = ("top-left", "top-mid", "top-right", "mid-left", "center", "mid-right", "btm-left", "btm-mid", "btm-right")
# (key, label, arguments, CommandFlags); Explorer orders the entries by key name
SUBCOMMANDS = ([("01upload", "and upload (center)", "--place center", SEPARATOR_AFTER)]
               + [("%02dprint-%s" % (i + 2, place), "upload and print (%s)" % place, "--print --place %s" % place, 0)
                  for i, place in enumerate(_PLACES)]
               + [("11options", "more options", "--options", SEPARATOR_BEFORE)])
OLD_KEYS = ("RemoteSlicing1Upload", "RemoteSlicing2Print")  # the first, flat version of the menu


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


def _shell(ext, classes):
    return "%s\\SystemFileAssociations\\%s\\shell" % (classes, ext)


def entries(python_exe, launcher, icon="", classes=CLASSES):
    """[(key path under HKCU, {value name: str or int})], every key before its subkeys. Integers are DWORDs."""
    out = []
    for ext in EXTENSIONS:
        menu = "%s\\%s" % (_shell(ext, classes), MENU_KEY)
        values = {"MUIVerb": MENU_LABEL, "SubCommands": ""}
        if icon:
            values["Icon"] = icon
        out.append((menu, values))
        for key, label, args, flags in SUBCOMMANDS:
            entry = "%s\\shell\\%s" % (menu, key)
            values = {"MUIVerb": label}
            if flags:
                values["CommandFlags"] = flags
            out.append((entry, values))
            out.append((entry + "\\command", {"": '"%s" "%s" --gather %s "%%1"' % (python_exe, launcher, args)}))
    return out


def _delete_tree(winreg, path):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            children = []
            while True:
                try:
                    children.append(winreg.EnumKey(key, len(children)))
                except OSError:
                    break
    except FileNotFoundError:
        return 0
    removed = sum(_delete_tree(winreg, path + "\\" + child) for child in children)
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    return removed + 1


def install(classes=CLASSES):
    """Write the menu (replacing any earlier version of it). Returns what was written."""
    import winreg
    uninstall(classes, refresh=False)
    written = entries(pythonw(), LAUNCHER, cura_icon(), classes)
    for path, values in written:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            for name, data in values.items():
                kind = winreg.REG_DWORD if isinstance(data, int) else winreg.REG_SZ
                winreg.SetValueEx(key, name, 0, kind, data)
    _refresh_explorer()
    return written


def uninstall(classes=CLASSES, refresh=True):
    """Remove the menu, including the first, flat version of it. Returns how many keys were removed."""
    import winreg
    removed = 0
    for ext in EXTENSIONS:
        for key in (MENU_KEY,) + OLD_KEYS:
            removed += _delete_tree(winreg, "%s\\%s" % (_shell(ext, classes), key))
    if refresh:
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
