"""Applies Cura's post-processing scripts to a headless slice, the way PostProcessingPlugin does when Cura saves.

Cura keeps a slice as a list of G-code chunks (header, start code, one chunk per layer, the final retraction,
the end code) and runs every active script's ``execute(chunks)`` on it, in order. Scripts such as Search and
Replace pick their range by chunk index, so the CLI's single G-code file is split back into exactly those
chunks first (``split_like_cura``).

The scripts themselves are Cura's own files, run against a small stand-in for the parts of Uranium/Cura they
use: the setting values the Cura plugin exported, the global and extruder settings from the snapshot, and a
``cura.Snapshot`` that renders the plate with ``thumbnail.render_png`` (for Create Thumbnail). A script that
needs anything else from Cura fails the job (PostProcessError) instead of leaving its work silently undone.
"""

import ast
import importlib.util
import json
import os
import re
import sys
import tempfile
import traceback
import types

GENERATED = ";Generated with Cura_SteamEngine"
LAYER = re.compile(r"^;LAYER:", re.M)
TIME_ELAPSED = re.compile(r"^;TIME_ELAPSED:[^\n]*\n", re.M)
# One line of GCodeExport::writeRetraction: firmware retraction, BFB, or "G1 F<speed> E<amount>"
RETRACTION = re.compile(r"(?:G10(?: S1)?|M103|G1 F-?[0-9.]+ [A-Z]-?[0-9.]+)\r?\n")
PACKAGE = "_remote_slicing_ppp"

_shim_failures = []


class PostProcessError(Exception):
    """A script that cannot run outside Cura, or a snapshot that doesn't describe the scripts properly."""


class ShimError(Exception):
    """A script used a part of Cura that this stand-in doesn't provide. Recorded even if a script catches it."""

    def __init__(self, what):
        super().__init__(what)
        _shim_failures.append(what)


def _missing(owner, name):
    if name.startswith("__"):  # hasattr()/import machinery probing dunders
        return AttributeError(name)
    return ShimError("%s.%s" % (owner, name))


def split_like_cura(text):
    """Split CuraEngine's output into the chunks Cura's backend would hold for it.

    CuraEngine (5.x, one mesh group) flushes a chunk before every layer is written, once after the retraction
    that ends the mesh group (only if that wrote anything), and at the end; the header arrives on its own and
    goes first.
    """
    if text.startswith(GENERATED):
        start = 0
    else:
        start = text.find("\n" + GENERATED) + 1
        if start == 0:
            raise PostProcessError("the G-code has no '%s' line" % GENERATED)
    bounds = [start] + [m.start() for m in LAYER.finditer(text, start)]
    chunks = [text[:start]] + [text[a:b] for a, b in zip(bounds, bounds[1:])]
    if len(bounds) == 1:
        return chunks + [text[start:]]
    last = bounds[-1]
    elapsed = [m.end() for m in TIME_ELAPSED.finditer(text, last)]
    if not elapsed:
        return chunks + [text[last:]]
    cut = elapsed[-1]
    chunks.append(text[last:cut])
    m = RETRACTION.match(text, cut)
    if m and m.end() < len(text):
        chunks.append(text[cut:m.end()])
        cut = m.end()
    if cut < len(text):
        chunks.append(text[cut:])
    return chunks


# --------------------------------------------------------------------------------------------------- settings

class _SettingTypes:
    """Setting types from fdmprinter/fdmextruder, to turn the snapshot's strings back into Cura's values."""

    def __init__(self, fdmprinter_path):
        self._path = fdmprinter_path
        self._types = None

    def _load(self):
        types_ = {}

        def walk(settings):
            for key, definition in settings.items():
                if "type" in definition:
                    types_[key] = definition["type"]
                walk(definition.get("children") or {})

        folder = os.path.dirname(self._path)
        for name in ("fdmprinter.def.json", "fdmextruder.def.json"):
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    walk(json.load(f).get("settings") or {})
            except (OSError, ValueError):
                pass
        self._types = types_

    def convert(self, key, raw):
        if raw is None:
            return None
        if self._types is None:
            self._load()
        kind = self._types.get(key)
        try:
            if kind == "bool":
                return raw in ("True", "true", "1")
            if kind == "int":
                return int(float(raw))
            if kind == "float":
                return float(raw)
            if kind in ("polygon", "polygons"):
                return ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return raw
        return raw


class _Stack:
    def __init__(self, values, types_, name="", metadata=None, extruders=None):
        self._values = values
        self._types = types_
        self._name = name
        self._metadata = metadata or {}
        self.extruderList = extruders or []

    def getProperty(self, key, property_name):
        if property_name != "value":
            raise ShimError("setting property '%s'" % property_name)
        return self._types.convert(key, self._values.get(key))

    def getName(self):
        return self._name

    def getMetaDataEntry(self, key, default=None):
        if key not in self._metadata:
            raise ShimError("stack metadata '%s'" % key)
        return self._metadata[key]

    def __getattr__(self, name):
        raise _missing("ContainerStack", name)


class _ExtruderManager:
    def __init__(self, stacks):
        self._stacks = stacks

    def getActiveExtruderStacks(self):
        return list(self._stacks)

    def getExtruderStack(self, index):
        return self._stacks[int(index)]

    def __getattr__(self, name):
        raise _missing("ExtruderManager", name)


class _App:
    def __init__(self, global_stack, version):
        self._global = global_stack
        self._version = version
        self._extruders = _ExtruderManager(global_stack.extruderList)

    def getGlobalContainerStack(self):
        return self._global

    def getVersion(self):
        return self._version

    def getExtruderManager(self):
        return self._extruders

    def __getattr__(self, name):
        raise _missing("CuraApplication", name)


# --------------------------------------------------------------------------------------- Uranium / Cura / Qt

class _Logger:
    @staticmethod
    def log(level, message, *args):
        print("[script %s] %s" % (level, message % args if args else message), file=sys.stderr)

    @staticmethod
    def logException(level, message, *args):
        _Logger.log(level, message, *args)
        traceback.print_exc()

    debug = staticmethod(lambda message, *args: _Logger.log("d", message, *args))
    info = staticmethod(lambda message, *args: _Logger.log("i", message, *args))
    warning = staticmethod(lambda message, *args: _Logger.log("w", message, *args))
    error = staticmethod(lambda message, *args: _Logger.log("e", message, *args))
    critical = staticmethod(lambda message, *args: _Logger.log("c", message, *args))


class _Message:
    class MessageType:
        POSITIVE, NEUTRAL, WARNING, ERROR = range(4)

    def __init__(self, text="", *args, **kwargs):
        self._text = text
        self._title = kwargs.get("title") or ""

    def show(self):
        print("[script message] %s %s" % (self._title, self._text), file=sys.stderr)

    def hide(self, *args):
        pass

    def addAction(self, *args, **kwargs):
        pass


class _Signal:
    def __init__(self, *args, **kwargs):
        pass

    def connect(self, *args):
        pass

    def disconnect(self, *args):
        pass

    def emit(self, *args):
        pass


class _Catalog:
    def __init__(self, name=None):
        pass

    def i18n(self, text, *args):
        return text.format(*args) if args else text

    def i18nc(self, context, text, *args):
        return text.format(*args) if args else text

    def i18np(self, single, multiple, counter, *args):
        text = single if counter == 1 else multiple
        return text.format(counter, *args)

    def i18ncp(self, context, single, multiple, counter, *args):
        return self.i18np(single, multiple, counter, *args)


def _unavailable(what):
    class Unavailable:
        def __init__(self, *args, **kwargs):
            raise ShimError(what)
    Unavailable.__name__ = what.rsplit(".", 1)[-1]
    return Unavailable


class _Image:
    """What Snapshot.snapshot returns; CreateThumbnail only saves it as PNG into a QBuffer."""

    def __init__(self, png):
        self._png = png

    def save(self, buffer, fmt=None, *args):
        if str(fmt).upper() != "PNG":
            raise ShimError("saving a snapshot as %s" % fmt)
        buffer.write(self._png)
        return True


class _QBuffer:
    class OpenModeFlag:
        ReadOnly, WriteOnly, ReadWrite = 1, 2, 3

    def __init__(self, *args):
        self._data = b""

    def open(self, mode):
        return True

    def write(self, data):
        self._data += bytes(data)
        return len(data)

    def data(self):
        return self._data

    def close(self):
        pass


def _module(name, attributes, package=False):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    if package:
        module.__path__ = []

    def missing(attr):
        raise _missing(name, attr)
    module.__getattr__ = missing
    return module


class Environment:
    """Puts the stand-in modules into sys.modules (the worker is a throwaway process) and loads scripts."""

    def __init__(self, snapshot, models):
        resolved = snapshot.get("resolved") or {}
        types_ = _SettingTypes((snapshot.get("engine") or {}).get("fdmprinter", ""))
        summary = snapshot.get("summary") or {}
        extruders = []
        for info in summary.get("extruders") or []:
            nr = int(info.get("position", len(extruders)))
            stack = _Stack(resolved.get("extruder.%d" % nr) or {}, types_, metadata={"position": str(nr)})
            stack.isEnabled = bool(info.get("enabled", True))
            extruders.append(stack)
        metadata = {"post_processing_scripts": (snapshot.get("post_processing") or {}).get("metadata", "")}
        global_stack = _Stack(resolved.get("global") or {}, types_, name=summary.get("machine", ""),
                              metadata=metadata, extruders=extruders)
        self.app = _App(global_stack, snapshot.get("cura_version", ""))
        self.models = models
        self._install()

    def _snapshot(self, width=300, height=300, *args):
        from . import thumbnail
        png = thumbnail.render_png([(tris, thumbnail.parse_color(color)) for tris, color in self.models],
                                   int(width), int(height))
        return _Image(png) if png else None

    def _install(self):
        app = self.app

        class Application:
            @staticmethod
            def getInstance(*args):
                return app

        class ExtruderManager:
            @staticmethod
            def getInstance(*args):
                return app.getExtruderManager()

        class Snapshot:
            snapshot = staticmethod(self._snapshot)

        modules = [
            _module("UM", {}, package=True),
            _module("UM.Application", {"Application": Application}),
            _module("UM.Logger", {"Logger": _Logger}),
            _module("UM.Message", {"Message": _Message}),
            _module("UM.Signal", {"Signal": _Signal, "signalemitter": lambda cls: cls}),
            _module("UM.i18n", {"i18nCatalog": _Catalog}),
            _module("UM.Settings", {}, package=True),
            _module("UM.Settings.ContainerFormatError", {"ContainerFormatError": type("ContainerFormatError",
                                                                                      (Exception,), {})}),
            _module("UM.Settings.ContainerStack", {"ContainerStack": _unavailable("UM.Settings.ContainerStack")}),
            _module("UM.Settings.InstanceContainer",
                    {"InstanceContainer": _unavailable("UM.Settings.InstanceContainer")}),
            _module("UM.Settings.DefinitionContainer",
                    {"DefinitionContainer": _unavailable("UM.Settings.DefinitionContainer")}),
            _module("UM.Settings.ContainerRegistry",
                    {"ContainerRegistry": _unavailable("UM.Settings.ContainerRegistry")}),
            _module("cura", {}, package=True),
            _module("cura.CuraApplication", {"CuraApplication": Application}),
            _module("cura.Snapshot", {"Snapshot": Snapshot}),
            _module("cura.Settings", {}, package=True),
            _module("cura.Settings.ExtruderManager", {"ExtruderManager": ExtruderManager}),
            _module("PyQt6", {}, package=True),
            _module("PyQt6.QtCore", {"QBuffer": _QBuffer, "QByteArray": bytes, "QIODevice": _QBuffer}),
        ]
        modules += [_module(PACKAGE, {}, package=True), _module(PACKAGE + ".scripts", {}, package=True)]
        self._saved = {module.__name__: sys.modules.get(module.__name__) for module in modules}
        for module in modules:
            sys.modules[module.__name__] = module

    def close(self):
        """Put sys.modules back the way it was."""
        for name in [n for n in sys.modules if n.startswith(PACKAGE + ".")]:
            del sys.modules[name]
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def load_base(self, path):
        """Cura's own Script.py, with the two methods that need Uranium's settings machinery replaced."""
        name = PACKAGE + ".Script"
        if name in sys.modules:
            return
        module = _load_file(name, path)

        def initialize(script):
            script._rs_values = getattr(script, "_rs_values", {})
            script._stack = script._instance = _ScriptSettings(script._rs_values)

        module.Script.initialize = initialize
        module.Script.getSettingValueByKey = lambda script, key: getattr(script, "_rs_values", {}).get(key)
        sys.modules[name] = module

    def create(self, entry):
        """Instantiate one exported script the way PostProcessingPlugin restores it from the stack."""
        path = entry.get("file") or ""
        if not os.path.isfile(path):
            raise PostProcessError("the post-processing script %s is not at %s any more; open Cura once so the "
                                   "Remote Slicing snapshot is refreshed" % (entry.get("key"), path))
        stem = os.path.splitext(os.path.basename(path))[0]
        module = _load_file(PACKAGE + ".scripts." + stem, path)
        script = getattr(module, entry.get("class") or stem)()
        try:
            script.initialize()
        except ShimError:
            raise
        except Exception:  # its own defaults are about to be replaced by Cura's values anyway
            traceback.print_exc()
        if not hasattr(script, "_rs_values"):
            raise ShimError("%s.initialize did not call Script.initialize" % entry.get("key"))
        script._rs_values.update(entry.get("settings") or {})
        return script


class _ScriptSettings:
    """Stands in for a script's own settings stack and instance container."""

    def __init__(self, values):
        self._values = values

    def getProperty(self, key, property_name):
        if property_name != "value":
            raise ShimError("script setting property '%s'" % property_name)
        return self._values.get(key)

    def setProperty(self, key, property_name, value, *args):
        if property_name != "value":
            raise ShimError("script setting property '%s'" % property_name)
        self._values[key] = value

    def __getattr__(self, name):
        raise _missing("script settings", name)


def _load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PostProcessError("can't load %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


# ----------------------------------------------------------------------------------------------------- apply

def apply(text, snapshot, models):
    """Run the snapshot's post-processing scripts on ``text`` (the whole G-code) like Cura does on save.

    ``models`` is a list of (triangles, "#rrggbb") for the plate as sliced, used by Create Thumbnail.
    Returns (new_text, [script keys run]); the text is unchanged when Cura has no scripts active.
    """
    pp = snapshot.get("post_processing")
    if not pp or not pp.get("scripts"):
        return text, []
    if not pp.get("complete", True):
        raise PostProcessError("Cura's post-processing scripts weren't loaded when the settings were exported; "
                               "use Extensions > Remote Slicing > Export settings now in Cura")
    base = pp.get("script_base") or ""
    if not os.path.isfile(base):
        raise PostProcessError("Cura's post-processing plugin is not at %s any more; open Cura once so the "
                               "Remote Slicing snapshot is refreshed" % os.path.dirname(base))
    data = split_like_cura(text)
    if ";POSTPROCESSED" in data[0]:
        return text, []
    env = Environment(snapshot, models)
    try:
        return _run(env, base, pp, data)
    finally:
        env.close()


def _run(env, base, pp, data):
    env.load_base(base)
    unavailable = ("the post-processing script %s needs %s, which only exists inside Cura; "
                   "slice this plate in Cura or turn the script off")
    ran = []
    del _shim_failures[:]
    for entry in pp["scripts"]:
        key = entry.get("key", "?")
        try:
            data = env.create(entry).execute(data)
        except ImportError as e:
            raise PostProcessError(unavailable % (key, e.name or e))
        except PostProcessError:
            raise
        except Exception:
            # Cura logs a failing script and carries on with the next one; so does this, unless the
            # failure was this stand-in falling short of Cura.
            if not _shim_failures:
                # e.g. Search and Replace indexing past the end when the engine wrote no final retraction
                # chunk; Cura logs the same exception and keeps the replacements already made
                error = traceback.format_exception_only(*sys.exc_info()[:2])[-1].strip()
                print("post-processing script %s raised %s (Cura carries on the same way)" % (key, error),
                      file=sys.stderr)
        if _shim_failures:  # also when the script caught it itself
            raise PostProcessError(unavailable % (key, _shim_failures[0]))
        ran.append(key)
    data[0] += ";POSTPROCESSED\n"
    for name in pp.get("metadata", "").split("\n"):
        data[0] += ";  " + str(name.split("]")[0]) + "]\n"
    return "".join(data), ran


def apply_to_file(path, snapshot, models):
    """apply() on a G-code file, rewritten in place. Returns the keys of the scripts run.

    CuraEngine on Windows writes its file in text mode, so every "\\n" it produced is "\\r\\n" on disk (and a
    "\\r\\n" inside the user's start/tool-change code is "\\r\\r\\n"). Undoing that gives the exact text Cura
    holds and hands to the scripts; the result is written with "\\n", as Cura sends G-code to OctoPrint.
    """
    pp = snapshot.get("post_processing")
    if not pp or not pp.get("scripts"):
        return []
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", "replace").replace("\r\n", "\n")
    text, ran = apply(text, snapshot, models)
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(suffix=".gcode", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return ran
