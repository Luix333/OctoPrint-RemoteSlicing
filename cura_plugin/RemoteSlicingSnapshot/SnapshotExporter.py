# Copyright (c) 2026 Luix333
# Released under the terms of the AGPLv3 or higher.

"""Keeps %LOCALAPPDATA%\\RemoteSlicing\\snapshot.json equal to what Cura would send CuraEngine.

The PC agent slices with a headless CuraEngine.exe from this snapshot, so the settings, the
expanded start/end g-code and the per-extruder (MMU) values are computed by Cura itself, through
StartSliceJob's own message builders, instead of being re-implemented outside Cura.
"""

import datetime
import importlib
import json
import os
import sys
import tempfile
import threading
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QTimer

from UM.Extension import Extension
from UM.Job import Job
from UM.JobQueue import JobQueue
from UM.Logger import Logger
from UM.Message import Message
from UM.PluginRegistry import PluginRegistry
from UM.Resources import Resources

from cura.CuraApplication import CuraApplication

from .recorder import Recorder, toResolvedJson

SNAPSHOT_FORMAT = 1
DEBOUNCE_MS = 2000


def snapshotDir() -> str:
    """REMOTE_SLICING_HOME overrides the folder (tests); the agent uses the same rule."""
    override = os.environ.get("REMOTE_SLICING_HOME")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "RemoteSlicing")


def snapshotPath() -> str:
    return os.path.join(snapshotDir(), "snapshot.json")


def _startSliceJobClass():
    """StartSliceJob lives in the bundled CuraEngineBackend plugin; import it through the module
    the backend object was loaded from, since plugin module names are Uranium's business."""
    backend = PluginRegistry.getInstance().getPluginObject("CuraEngineBackend")
    package = type(backend).__module__.rsplit(".", 1)[0]
    return importlib.import_module(package + ".StartSliceJob").StartSliceJob


def _prop(stack, key: str) -> Any:
    return stack.getProperty(key, "value")


def buildSnapshot() -> Optional[Dict[str, Any]]:
    """Runs on a JobQueue worker, like StartSliceJob itself does."""
    app = CuraApplication.getInstance()
    global_stack = app.getGlobalContainerStack()
    if global_stack is None:
        return None

    recorder = Recorder()
    job = _startSliceJobClass()(recorder)
    extruder_manager = app.getExtruderManager()
    original_initial = extruder_manager.getInitialExtruderNr

    def safeInitialExtruderNr() -> int:
        # Cura asks the scene which extruders are used; with an empty plate (the normal case
        # here) that raises IndexError. Same answer as Cura whenever Cura has one.
        try:
            return original_initial()
        except IndexError:
            return int(app.getMachineManager().defaultExtruderPosition)

    extruder_manager.getInitialExtruderNr = safeInitialExtruderNr
    try:
        job._buildGlobalSettingsMessage(global_stack)
        job._buildGlobalInheritsStackMessage(global_stack)
        for extruder_stack in global_stack.extruderList:
            job._buildExtruderMessage(extruder_stack)

        # The start/end g-code take the *initial* extruder's values (temperatures, material),
        # and which extruder starts depends on the job's models. Expand them once per possible
        # initial extruder; the agent picks the right pair per job.
        raw_start = global_stack.getProperty("machine_start_gcode", "value")
        raw_end = global_stack.getProperty("machine_end_gcode", "value")
        gcode_by_initial: Dict[str, Dict[str, str]] = {}
        for extruder_stack in global_stack.extruderList:
            if not extruder_stack.isEnabled:
                continue
            nr = int(extruder_stack.getMetaDataEntry("position"))
            for tokens in job._all_extruders_settings.values():
                tokens["initial_extruder_nr"] = nr
            gcode_by_initial[str(nr)] = {
                "machine_start_gcode": job._expandGcodeTokens(raw_start, nr),
                "machine_end_gcode": job._expandGcodeTokens(raw_end, nr),
            }
    finally:
        del extruder_manager.getInitialExtruderNr
    resolved = toResolvedJson(recorder)

    # Inputs of Cura's ExtruderManager.getInitialExtruderNr()/getUsedExtruderStacks(), so the
    # agent can repeat that choice for the job's models.
    initial_inputs = {key: _prop(global_stack, key) for key in (
        "adhesion_type", "skirt_brim_extruder_nr", "raft_base_extruder_nr", "raft_interface_extruder_nr",
        "raft_surface_extruder_nr", "raft_interface_layers", "raft_surface_layers", "skirt_line_count",
        "skirt_brim_minimal_length", "brim_line_count", "prime_tower_brim_enable", "support_enable",
        "support_structure", "support_brim_enable", "support_bottom_enable", "support_roof_enable",
        "support_infill_extruder_nr", "support_extruder_nr_layer_0", "support_bottom_extruder_nr",
        "support_roof_extruder_nr", "wall_0_extruder_nr", "wall_x_extruder_nr", "roofing_extruder_nr",
        "flooring_extruder_nr", "top_bottom_extruder_nr", "infill_extruder_nr")}
    initial_inputs["default_extruder"] = int(app.getMachineManager().defaultExtruderPosition)

    machine_manager = app.getMachineManager()
    extruders: List[Dict[str, Any]] = []
    for extruder_stack in global_stack.extruderList:
        material = extruder_stack.material
        extruders.append({
            "position": int(extruder_stack.getMetaDataEntry("position")),
            "enabled": bool(extruder_stack.isEnabled),
            "material": material.getMetaDataEntry("name", ""),
            "brand": material.getMetaDataEntry("brand", ""),
            "type": material.getMetaDataEntry("material", ""),
            "color": material.getMetaDataEntry("color_code", ""),
            "nozzle": extruder_stack.variant.getName(),
        })

    try:
        edge = float(app.getBuildVolume().getEdgeDisallowedSize())
    except Exception:
        edge = 0.0

    return {
        "format": SNAPSHOT_FORMAT,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "cura_version": app.getVersion(),
        "engine": {
            "path": app.getPreferences().getValue("backend/location")
                    or os.path.join(os.path.dirname(sys.executable), "CuraEngine.exe"),
            "fdmprinter": Resources.getPath(Resources.DefinitionContainers, "fdmprinter.def.json"),
        },
        "bed": {
            "width": float(_prop(global_stack, "machine_width")),
            "depth": float(_prop(global_stack, "machine_depth")),
            "height": float(_prop(global_stack, "machine_height")),
            "shape": _prop(global_stack, "machine_shape"),
            "center_is_zero": bool(_prop(global_stack, "machine_center_is_zero")),
            "disallowed_areas": _prop(global_stack, "machine_disallowed_areas") or [],
            "nozzle_disallowed_areas": _prop(global_stack, "nozzle_disallowed_areas") or [],
            "edge_disallowed_size": edge,
        },
        "summary": {
            "machine": global_stack.getName(),
            "definition": global_stack.definition.getId(),
            "profile": machine_manager.activeQualityOrQualityChangesName,
            "intent": machine_manager.activeIntentCategory,
            "layer_height": float(_prop(global_stack, "layer_height")),
            "infill": float(_prop(global_stack, "infill_sparse_density")),
            "adhesion": _prop(global_stack, "adhesion_type"),
            "support": bool(_prop(global_stack, "support_enable")),
            "print_sequence": _prop(global_stack, "print_sequence"),
            "extruders": extruders,
        },
        "resolved": resolved,
        "gcode_by_initial_extruder": gcode_by_initial,
        "initial_extruder_inputs": initial_inputs,
    }


def writeSnapshot(snapshot: Dict[str, Any]) -> str:
    folder = snapshotDir()
    os.makedirs(folder, exist_ok=True)
    target = snapshotPath()
    fd, tmp = tempfile.mkstemp(prefix="snapshot.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, separators=(",", ":"))
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


class _ExportJob(Job):
    def __init__(self, exporter: "SnapshotExporter", announce: bool) -> None:
        super().__init__()
        self._exporter = exporter
        self._announce = announce

    def run(self) -> None:
        try:
            snapshot = buildSnapshot()
            if snapshot is None:
                self._exporter._jobDone(None, "no printer is active", self._announce)
                return
            path = writeSnapshot(snapshot)
            Logger.log("i", "Remote Slicing snapshot written to %s (%d extruders)",
                       path, len(snapshot["summary"]["extruders"]))
            self._exporter._jobDone(snapshot, None, self._announce)
        except Exception as e:
            Logger.logException("e", "Remote Slicing snapshot failed")
            self._exporter._jobDone(None, str(e), self._announce)


class SnapshotExporter(Extension):
    def __init__(self) -> None:
        super().__init__()
        self._app = CuraApplication.getInstance()
        self._lock = threading.Lock()
        self._running = False
        self._announce_next = False
        self._last_created: Optional[str] = None
        self._stacks: List[Any] = []

        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._export)

        self.setMenuName("Remote Slicing")
        self.addMenuItem("Export settings now", self._exportNow)
        self.addMenuItem("Show snapshot folder", self._showFolder)

        self._app.engineCreatedSignal.connect(self._onEngineCreated)

    # Change tracking ------------------------------------------------------------------------

    def _onEngineCreated(self) -> None:
        self._app.getMachineManager().globalContainerChanged.connect(self._onGlobalChanged)
        self._onGlobalChanged()

    def _onGlobalChanged(self) -> None:
        for stack in self._stacks:
            stack.propertyChanged.disconnect(self._onPropertyChanged)
            stack.containersChanged.disconnect(self._onSomethingChanged)
            stack.metaDataChanged.disconnect(self._onSomethingChanged)
        self._stacks = []
        global_stack = self._app.getGlobalContainerStack()
        if global_stack is not None:
            self._stacks = [global_stack] + list(global_stack.extruderList)
            for stack in self._stacks:
                stack.propertyChanged.connect(self._onPropertyChanged)
                stack.containersChanged.connect(self._onSomethingChanged)
                stack.metaDataChanged.connect(self._onSomethingChanged)
        self._schedule()

    def _onPropertyChanged(self, key: str, property_name: str) -> None:
        if property_name == "value":
            self._schedule()

    def _onSomethingChanged(self, *args) -> None:
        self._schedule()

    def _schedule(self) -> None:
        # Signals can arrive from worker threads; QTimer.start must run on the GUI thread.
        if threading.current_thread() is threading.main_thread():
            self._timer.start()
        else:
            self._app.callLater(self._timer.start)

    # Export ---------------------------------------------------------------------------------

    def _export(self) -> None:
        with self._lock:
            if self._running:
                self._timer.start()  # try again once the running export has finished
                return
            self._running = True
            announce, self._announce_next = self._announce_next, False
        JobQueue.getInstance().add(_ExportJob(self, announce))

    def _jobDone(self, snapshot: Optional[Dict[str, Any]], error: Optional[str], announce: bool) -> None:
        with self._lock:
            self._running = False
        if snapshot is not None:
            self._last_created = snapshot["created"]
        if announce:
            self._app.callLater(self._announce, error)

    def _announce(self, error: Optional[str]) -> None:
        if error:
            text, kind = "Couldn't export the settings: %s" % error, Message.MessageType.ERROR
        else:
            text, kind = "Settings exported for Remote Slicing (%s)." % self._last_created, Message.MessageType.POSITIVE
        Message(text, title="Remote Slicing", message_type=kind).show()

    def _exportNow(self) -> None:
        self._announce_next = True
        self._timer.stop()
        self._export()

    def _showFolder(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        os.makedirs(snapshotDir(), exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(snapshotDir()))
