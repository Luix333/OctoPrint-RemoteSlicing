"""Tests for remote_slicing_agent.postprocess (Cura's post-processing scripts, run headlessly)."""

import base64
import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest

import numpy as np

from remote_slicing_agent import postprocess

HEADER = ";FLAVOR:RepRap\n;TIME:799\n;TARGET_MACHINE.NAME:VORON2 Custom\n"
START = ";Generated with Cura_SteamEngine 5.13.0\nT0\nM104 T0 S230\nPRINT_START\nG1 F1800 E-1\n;LAYER_COUNT:3\n"
LAYERS = [";LAYER:%d\nG1 X1 Y1 E1\nT0\n;TIME_ELAPSED:%d.5\n" % (i, i) for i in range(3)]
RETRACT = "G1 F1800 E-1\n"
END = "M140 S0\nM107\nPRINT_END\n;End of Gcode\n;SETTING_3 {}\n"
GCODE = HEADER + START + "".join(LAYERS) + RETRACT + END

CURA_PP = r"C:\Program Files\UltiMaker Cura 5.13.0\share\cura\plugins\PostProcessingPlugin"

# Imports what Cura's own Script.py imports, so the stand-in modules are exercised the same way.
SCRIPT_BASE = '''
from UM.Signal import Signal, signalemitter
from UM.i18n import i18nCatalog
from UM.Application import Application
from UM.Settings.ContainerFormatError import ContainerFormatError
from UM.Settings.ContainerStack import ContainerStack
from UM.Settings.InstanceContainer import InstanceContainer
from UM.Settings.DefinitionContainer import DefinitionContainer
from UM.Settings.ContainerRegistry import ContainerRegistry
import json, collections

@signalemitter
class Script:
    settingsLoaded = Signal()

    def __init__(self):
        super().__init__()
        self._stack = None
        self._instance = None

    def initialize(self):
        self._stack = ContainerStack(stack_id=str(id(self)))  # Uranium only; replaced by the agent

    def getSettingData(self):
        return json.loads(self.getSettingDataString(), object_pairs_hook=collections.OrderedDict)

    def getSettingValueByKey(self, key):
        return self._stack.getProperty(key, "value")
'''

UPPER = '''
from ..Script import Script
from UM.Application import Application
from UM.Logger import Logger

class Upper(Script):
    def getSettingDataString(self):
        return '{"name": "Upper", "key": "Upper", "metadata": {}, "version": 2, "settings": {"word": {"type": "str"}}}'

    def initialize(self):
        super().initialize()
        self._instance.setProperty("word", "value", "default")

    def execute(self, data):
        stack = Application.getInstance().getGlobalContainerStack()
        retract = stack.extruderList[0].getProperty("retraction_enable", "value")
        height = stack.getProperty("layer_height", "value")
        Logger.log("d", "upper %s", self.getSettingValueByKey("word"))
        word = self.getSettingValueByKey("word")
        out = [data[0]] + [c.replace(word, word.upper()) for c in data[1:]]
        out[-1] += ";retract=%r height=%r count=%d\\n" % (retract, height, len(data))
        return out
'''

NEEDS_PRINT_INFO = '''
from ..Script import Script
from UM.Application import Application

class PrintInfo(Script):
    def getSettingDataString(self):
        return '{"name": "Info", "key": "PrintInfo", "metadata": {}, "version": 2, "settings": {}}'

    def execute(self, data):
        try:
            Application.getInstance().getPrintInformation()
        except Exception:
            pass  # swallowed, but the agent must still notice
        return data
'''

NEEDS_QT = '''
from ..Script import Script
from UM.Qt.Duration import DurationFormat

class NeedsQt(Script):
    def getSettingDataString(self):
        return '{"name": "Qt", "key": "NeedsQt", "metadata": {}, "version": 2, "settings": {}}'

    def execute(self, data):
        return data
'''

BROKEN = '''
from ..Script import Script

class Broken(Script):
    def getSettingDataString(self):
        return '{"name": "Broken", "key": "Broken", "metadata": {}, "version": 2, "settings": {}}'

    def execute(self, data):
        data[1] += ";broken was here\\n"
        raise IndexError("like Search and Replace running past the last chunk")
'''


class SplitTests(unittest.TestCase):
    def test_chunks_like_curas_backend(self):
        chunks = postprocess.split_like_cura(GCODE)
        self.assertEqual(chunks, [HEADER, START] + LAYERS + [RETRACT, END])

    def test_no_final_retraction_chunk_when_none_was_written(self):
        text = HEADER + START + "".join(LAYERS) + END
        self.assertEqual(postprocess.split_like_cura(text), [HEADER, START] + LAYERS + [END])

    def test_firmware_retraction_and_crlf_inside_user_code(self):
        start = START.replace("PRINT_START\n", "PRINT_START\r\n")
        text = HEADER + start + "".join(LAYERS) + "G10\n" + END
        self.assertEqual(postprocess.split_like_cura(text), [HEADER, start] + LAYERS + ["G10\n", END])

    def test_retraction_with_more_on_the_line_is_not_split_off(self):
        text = HEADER + START + "".join(LAYERS) + "G1 F1800 E-1 X5\n" + END
        self.assertEqual(postprocess.split_like_cura(text)[-1], "G1 F1800 E-1 X5\n" + END)

    def test_raft_layers_and_no_generated_line(self):
        layers = [";LAYER:-2\n;TIME_ELAPSED:1\n", ";LAYER:-1\n;TIME_ELAPSED:2\n", ";LAYER:0\n;TIME_ELAPSED:3\n"]
        text = HEADER + START + "".join(layers) + END
        self.assertEqual(postprocess.split_like_cura(text), [HEADER, START] + layers + [END])
        with self.assertRaises(postprocess.PostProcessError):
            postprocess.split_like_cura("G1 X0\n")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "scripts"))
        self.base = self.write("Script.py", SCRIPT_BASE)
        self.definitions = os.path.join(self.dir, "fdmprinter.def.json")
        with open(self.definitions, "w") as f:
            f.write('{"settings": {"resolution": {"type": "category", "children": {'
                    '"layer_height": {"type": "float"}, "retraction_enable": {"type": "bool"}}}}}')
        self.modules_before = set(sys.modules)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        leaked = {m for m in set(sys.modules) - self.modules_before if not m.startswith("remote_slicing_agent")}
        self.assertEqual(leaked, set())
        self.assertNotIn("UM", sys.modules)

    def write(self, name, source):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(textwrap.dedent(source))
        return path

    def snapshot(self, scripts, metadata=None, complete=True):
        return {
            "cura_version": "5.13.0",
            "engine": {"fdmprinter": self.definitions},
            "summary": {"machine": "VORON", "extruders": [{"position": 0, "enabled": True, "color": "#ff0000"}]},
            "resolved": {"global": {"layer_height": "0.2"}, "extruder.0": {"retraction_enable": "True"}},
            "post_processing": {
                "metadata": metadata if metadata is not None else "\n".join(
                    "[%s]\\\\\\nword = x\\\\\\n" % s["key"] for s in scripts),
                "script_base": self.base, "scripts": scripts, "complete": complete,
            },
        }

    def entry(self, key, source, settings=None):
        return {"key": key, "class": key, "file": self.write(os.path.join("scripts", key + ".py"), source),
                "settings": settings or {}}

    def test_runs_scripts_in_order_with_typed_settings_and_marks_the_header(self):
        snap = self.snapshot([self.entry("Upper", UPPER, {"word": "print_end"}),
                              self.entry("Upper", UPPER, {"word": "PRINT"})])
        out, ran = postprocess.apply(GCODE.replace("PRINT_END", "print_end"), snap, [])
        self.assertEqual(ran, ["Upper", "Upper"])
        self.assertIn("PRINT_END\n", out)
        self.assertIn(";retract=True height=0.2 count=7\n;retract=True height=0.2 count=7\n", out)
        self.assertTrue(out.startswith(HEADER + ";POSTPROCESSED\n;  [Upper]\n;  [Upper]\n" + START))

    def test_script_initialize_defaults_are_overridden_by_curas_values(self):
        snap = self.snapshot([self.entry("Upper", UPPER, {})])
        out, _ = postprocess.apply(GCODE.replace("M140", "default"), snap, [])
        self.assertIn("DEFAULT S0", out)  # value from Upper.initialize, since Cura exported none
        snap = self.snapshot([self.entry("Upper", UPPER, {"word": "print_end"})])
        out, _ = postprocess.apply(GCODE.replace("M140", "default").replace("PRINT_END", "print_end"), snap, [])
        self.assertIn("default S0", out)
        self.assertIn("PRINT_END", out)

    def test_failing_script_is_logged_and_skipped_like_cura(self):
        snap = self.snapshot([self.entry("Broken", BROKEN), self.entry("Upper", UPPER, {"word": "print_start"})])
        out, ran = postprocess.apply(GCODE.replace("PRINT_START", "print_start"), snap, [])
        self.assertIn(";broken was here\n", out)  # in-place changes before the exception stay, as in Cura
        self.assertIn("PRINT_START", out)
        self.assertEqual(ran, ["Broken", "Upper"])

    def test_script_needing_more_of_cura_fails_the_job_even_if_it_swallows_the_error(self):
        snap = self.snapshot([self.entry("PrintInfo", NEEDS_PRINT_INFO)])
        with self.assertRaisesRegex(postprocess.PostProcessError, "PrintInfo.*getPrintInformation"):
            postprocess.apply(GCODE, snap, [])

    def test_script_importing_more_of_cura_fails_the_job(self):
        snap = self.snapshot([self.entry("NeedsQt", NEEDS_QT)])
        with self.assertRaisesRegex(postprocess.PostProcessError, "NeedsQt.*UM.Qt"):
            postprocess.apply(GCODE, snap, [])

    def test_missing_files_and_incomplete_exports_fail_with_a_hint(self):
        entry = self.entry("Upper", UPPER)
        entry["file"] = os.path.join(self.dir, "gone.py")
        with self.assertRaisesRegex(postprocess.PostProcessError, "refreshed"):
            postprocess.apply(GCODE, self.snapshot([entry]), [])
        with self.assertRaisesRegex(postprocess.PostProcessError, "Export settings now"):
            postprocess.apply(GCODE, self.snapshot([self.entry("Upper", UPPER)], complete=False), [])

    def test_nothing_to_do(self):
        self.assertEqual(postprocess.apply(GCODE, {}, []), (GCODE, []))
        self.assertEqual(postprocess.apply(GCODE, self.snapshot([]), []), (GCODE, []))

    def test_file_line_endings_like_cura(self):
        snap = self.snapshot([self.entry("Upper", UPPER, {"word": "print_end"})])
        path = os.path.join(self.dir, "out.gcode")
        # CuraEngine's text-mode file on Windows: every \n became \r\n, a \r\n in user code \r\r\n
        on_disk = GCODE.replace("PRINT_START\n", "PRINT_START\r\n").replace("PRINT_END", "print_end")
        with open(path, "wb") as f:
            f.write(on_disk.replace("\n", "\r\n").encode("utf-8"))
        self.assertEqual(postprocess.apply_to_file(path, snap, []), ["Upper"])
        with open(path, "rb") as f:
            result = f.read().decode("utf-8")
        self.assertIn("PRINT_START\r\nG1 F1800", result)
        self.assertNotIn("\r\r", result)
        self.assertEqual(result.count("\r\n"), 1)
        self.assertIn("PRINT_END", result)


@unittest.skipUnless(os.path.isdir(CURA_PP), "Cura 5.13 is not installed")
class RealCuraScriptTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def snapshot(self, scripts):
        names = "\n".join("[%s]\\\\\\n" % s["key"] for s in scripts)
        return {
            "cura_version": "5.13.0",
            "engine": {"fdmprinter": os.path.join(self.dir, "none.def.json")},
            "summary": {"machine": "VORON", "extruders": [{"position": 0, "enabled": True, "color": "#c0ff00"}]},
            "resolved": {"global": {}, "extruder.0": {"retraction_enable": "True"}},
            "post_processing": {"metadata": names, "script_base": os.path.join(CURA_PP, "Script.py"),
                                "scripts": scripts, "complete": True},
        }

    def test_search_and_replace_like_luis_uses_it(self):
        settings = {"search": "T0", "replace": "", "is_regex": False, "enable_layer_search": False,
                    "search_start": 1, "search_end": 2, "first_instance_only": False,
                    "ignore_start": False, "ignore_end": False}
        entry = {"key": "SearchAndReplace", "class": "SearchAndReplace", "settings": settings,
                 "file": os.path.join(CURA_PP, "scripts", "SearchAndReplace.py")}
        out, _ = postprocess.apply(GCODE + "T0\n", self.snapshot([entry]), [])
        chunks = out.split(";Generated with")
        self.assertNotIn("T0", chunks[1])  # start, layers, retraction and end chunks
        self.assertIn(";FLAVOR", chunks[0])

    def test_create_thumbnail_draws_the_plate(self):
        entry = {"key": "CreateThumbnail", "class": "CreateThumbnail",
                 "settings": {"width": 64, "height": 48, "use_thumbnail": True, "use_star": False},
                 "file": os.path.join(CURA_PP, "scripts", "CreateThumbnail.py")}
        cube = np.array([[[0, 0, 0], [10, 0, 0], [10, 10, 0]], [[0, 0, 0], [10, 10, 0], [0, 10, 0]],
                         [[0, 0, 10], [10, 10, 10], [10, 0, 10]], [[0, 0, 10], [0, 10, 10], [10, 10, 10]]], float)
        out, _ = postprocess.apply(GCODE, self.snapshot([entry]), [(cube, "#c0ff00")])
        m = re.search(r";Generated with Cura_SteamEngine 5.13.0\n;\n; thumbnail begin 64x48 (\d+)\n(.*?)"
                      r"; thumbnail end\n;\n\n", out, re.S)
        self.assertIsNotNone(m)
        data = "".join(line[2:] for line in m.group(2).split("\n") if line)
        self.assertEqual(len(data), int(m.group(1)))
        png = base64.b64decode(data)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(png[16:24], (64).to_bytes(4, "big") + (48).to_bytes(4, "big"))
        out, _ = postprocess.apply(GCODE, self.snapshot([entry]), [])  # empty plate: no snapshot, like Cura
        self.assertNotIn("thumbnail", out)


if __name__ == "__main__":
    unittest.main()
