"""SolidWorks part -> STL, through the Cura SolidWorks plugin's own COM session code.

The plugin (Luix333/LegacyCuraSolidWorksPlugin, v1.0.0+) keeps its SolidWorks automation in
``SolidWorksSession.py`` / ``ComAutomation.py`` with vendored comtypes and no Uranium imports, so the agent
loads those modules straight from the installed plugin folder. Its package ``__init__`` imports Uranium, so the
folder is mounted as a synthetic package instead of being imported normally.

Only the worker child process imports this module.
"""

import glob
import importlib
import os
import re
import sys
import tempfile
import types

_PACKAGE = "_cura_solidworks_plugin"


class SolidWorksError(Exception):
    """A failure with a message that is fit to show to the user."""


def _version_key(path):
    match = re.search(r"[\\/]cura[\\/](\d+)\.(\d+)[\\/]", path)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def find_plugin_dir(override=""):
    """The newest installed copy of the plugin that has SolidWorksSession.py, or None."""
    if override:
        return override if os.path.isfile(os.path.join(override, "SolidWorksSession.py")) else None
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    base = os.path.join(appdata, "cura", "*", "plugins", "CuraSolidWorksPlugin")
    candidates = glob.glob(os.path.join(base, "CuraSolidWorksPlugin", "SolidWorksSession.py"))  # package install
    candidates += glob.glob(os.path.join(base, "SolidWorksSession.py"))  # manual copy
    if not candidates:
        return None
    return os.path.dirname(max(candidates, key=_version_key))


def _load_session_module(plugin_dir):
    package = sys.modules.get(_PACKAGE)
    if package is None:
        package = types.ModuleType(_PACKAGE)
        package.__path__ = [plugin_dir]
        sys.modules[_PACKAGE] = package
    return importlib.import_module(_PACKAGE + ".SolidWorksSession")


def convert(sldprt_path, stl_path, plugin_dir=""):
    """Export ``sldprt_path`` (part or assembly) to a binary STL at ``stl_path``, Z up, in millimetres.

    SolidWorks exports in its own Y-up frame; the result is turned +90 degrees about X, the same correction the
    plugin applies inside Cura, so the part lands on the bed the way it is modelled.
    """
    folder = find_plugin_dir(plugin_dir)
    if folder is None:
        raise SolidWorksError("The Cura SolidWorks plugin (v1.0.0 or newer) is not installed, "
                              "so SolidWorks parts can't be converted.")
    try:
        session_module = _load_session_module(folder)
    except Exception as e:  # missing comtypes, renamed module, ...
        raise SolidWorksError("Could not load the Cura SolidWorks plugin from {}: {}".format(folder, e))

    from . import mesh  # numpy; only ever imported in the worker

    fd, raw_path = tempfile.mkstemp(suffix=".stl", dir=os.path.dirname(os.path.abspath(stl_path)))
    os.close(fd)
    try:
        try:
            with session_module.SolidWorksSession() as session:
                document = session.openDocument(sldprt_path)
                try:
                    session.export(document, raw_path, session_module.QUALITY_FINE)
                finally:
                    session.closeDocument(document)
        except session_module.SolidWorksError as e:
            raise SolidWorksError(str(e))
        except Exception as e:
            COMError = getattr(sys.modules.get(_PACKAGE + ".ComAutomation"), "COMError", None)
            if COMError is not None and isinstance(e, COMError):
                raise SolidWorksError(session_module.describeComError(e, sldprt_path))
            raise

        tris = mesh.load_stl(raw_path)
        # +90 degrees about X: (x, y, z) -> (x, -z, y)
        turned = tris[:, :, [0, 2, 1]].copy()
        turned[:, :, 1] *= -1
        mesh.write_binary_stl(stl_path, turned)
    finally:
        try:
            os.unlink(raw_path)
        except OSError:
            pass
