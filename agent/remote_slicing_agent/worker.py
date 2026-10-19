"""The job child process: ``python -m remote_slicing_agent.worker <task.json>``.

The resident agent starts one of these per task, so numpy, mesh parsing, CuraEngine and SolidWorks never load
into the long-running process. The worker reports progress and the outcome to OctoPrint itself and exits:
code 0 when the outcome (done or failed) reached the server, 3 when even reporting the failure failed.
"""

import json
import os
import re
import shutil
import sys
import threading
import time
import traceback

from . import config, dropbox_browse, engine, mesh, winproc
from .client import ApiError, Client


class TaskFailed(Exception):
    """A failure with a message for the user; reported with client.failed()."""


class Reporter:
    """Sends progress to OctoPrint, and re-sends it every ``interval`` seconds so the task's lease stays alive
    while CuraEngine is busy and silent. A reply with ``cancel: true`` sets ``cancelled``."""

    def __init__(self, client, task_id, interval=30.0):
        self.client = client
        self.task_id = task_id
        self.interval = interval
        self.cancelled = False
        self._progress = 0.0
        self._stage = "started"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _send(self, progress, stage):
        try:
            reply = self.client.progress(self.task_id, progress, stage) or {}
        except ApiError as e:
            print("progress report failed: %s %s" % (e.status, e.body), file=sys.stderr)
            return
        if reply.get("cancel"):
            self.cancelled = True

    def report(self, progress, stage):
        with self._lock:
            self._progress, self._stage = progress, stage
        self._send(progress, stage)

    def _run(self):
        while not self._stop.wait(self.interval):
            with self._lock:
                progress, stage = self._progress, self._stage
            self._send(progress, stage)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="progress-keepalive", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def safe_name(name):
    """A file-name-safe version of a plate name: spaces to "_", only [A-Za-z0-9._-], at most 60 chars."""
    name = re.sub(r"[^A-Za-z0-9._-]", "", (name or "").replace(" ", "_"))[:60]
    return name or "plate"


def _load_snapshot():
    try:
        with open(config.snapshot_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise TaskFailed("No Cura settings snapshot on the PC: open Cura once with the "
                         "Remote Slicing Snapshot plugin installed.")


def run_slice(task, cfg, client, reporter, folder):
    payload = task["payload"]
    items = payload["items"]
    snapshot = _load_snapshot()
    engine_path = snapshot.get("engine", {}).get("path", "")
    if not engine_path or not os.path.isfile(engine_path):
        raise TaskFailed("CuraEngine not found at %s" % engine_path)

    extruders = snapshot["summary"]["extruders"]
    for item in items:
        nr = int(item["extruder"])
        if not 0 <= nr < len(extruders):
            raise TaskFailed("T%d does not exist in Cura" % nr)
        if not extruders[nr].get("enabled", True):
            raise TaskFailed("T%d is disabled in Cura" % nr)

    reporter.report(0.02, "downloading models")
    sources = {}
    for item in items:
        model_id = item["model_id"]
        if model_id not in sources:
            sources[model_id] = os.path.join(folder, model_id + ".stl")
            client.download_model(model_id, sources[model_id])

    reporter.report(0.15, "placing models")
    bed = snapshot["bed"]
    meshes = []
    for i, item in enumerate(items):
        placed = mesh.place(mesh.load_stl(sources[item["model_id"]]), float(item["x"]), float(item["y"]),
                            float(item["rotation"]), bed["width"], bed["depth"], bed["center_is_zero"])
        path = os.path.join(folder, "m%d.stl" % i)
        mesh.write_binary_stl(path, placed)
        meshes.append((path, int(item["extruder"])))

    initial = engine.initial_extruder(snapshot["initial_extruder_inputs"], [nr for _, nr in meshes])
    resolved_path = os.path.join(folder, "resolved.json")
    with open(resolved_path, "w", encoding="utf-8") as f:
        json.dump(engine.build_resolved(snapshot, initial), f)

    out = os.path.join(folder, "out.gcode")
    cmd = engine.build_command(engine_path, resolved_path, meshes, out)
    try:
        parser = engine.run(cmd, on_progress=lambda p, s: reporter.report(0.2 + 0.7 * p, "slicing: " + s),
                            cwd=folder, cancelled=lambda: reporter.cancelled)
    except engine.EngineError as e:
        raise TaskFailed("cancelled" if str(e) == "cancelled" else "CuraEngine: %s" % e)
    engine.fix_header(out, parser.header)

    filename = "%s_%s.gcode" % (safe_name(payload.get("name")), time.strftime("%Y%m%d-%H%M%S"))
    reporter.report(0.92, "uploading")
    path = client.upload_gcode(out, filename)
    client.done(task["id"], {"path": path, "print_time_s": parser.print_time_s,
                             "filament_mm3": parser.filament_mm3, "initial_extruder": initial})


def _convert(src, folder, name, reporter, cfg):
    from . import solidworks  # COM + SolidWorks only when a part really has to be converted
    reporter.report(0.1, "converting in SolidWorks")
    stl = os.path.join(folder, safe_name(name) + ".stl")
    try:
        solidworks.convert(src, stl, cfg.get("solidworks_plugin_dir", ""))
    except solidworks.SolidWorksError as e:
        raise TaskFailed(str(e))
    return stl


def run_fetch(task, cfg, client, reporter, folder):
    try:
        src = dropbox_browse.resolve_file(cfg["dropbox_root"], task["payload"]["path"])
    except ValueError as e:
        raise TaskFailed(str(e))
    name, ext = os.path.splitext(os.path.basename(src))
    if ext.lower() == ".sldprt":
        src = _convert(src, folder, name, reporter, cfg)
    reporter.report(0.8, "uploading")
    client.upload_model(task["id"], src, name + ".stl")  # the server completes the task


def run_convert(task, cfg, client, reporter, folder):
    payload = task["payload"]
    reporter.report(0.02, "downloading")
    src = os.path.join(folder, "source.sldprt")
    client.download_source(payload["source_id"], src)
    stl = _convert(src, folder, payload["name"], reporter, cfg)
    reporter.report(0.8, "uploading")
    client.upload_model(task["id"], stl, payload["name"] + ".stl")


KINDS = {"slice": run_slice, "fetch": run_fetch, "convert": run_convert}


def run_task(task, cfg, client):
    """Run one task and report its outcome; returns the process exit code."""
    folder = os.path.join(config.work_dir(), task["id"])
    os.makedirs(folder, exist_ok=True)
    reporter = Reporter(client, task["id"])
    reporter.start()
    try:
        try:
            handler = KINDS.get(task.get("kind"))
            if handler is None:
                raise TaskFailed("unknown task kind")
            handler(task, cfg, client, reporter, folder)
            return 0
        except TaskFailed as e:
            message = str(e)
        except Exception as e:
            traceback.print_exc()
            message = "%s: %s" % (type(e).__name__, e)
        try:
            client.failed(task["id"], message)
            return 0
        except Exception:
            traceback.print_exc()
            return 3
    finally:
        reporter.stop()
        shutil.rmtree(folder, ignore_errors=True)


def main(argv=None):
    argv = sys.argv if argv is None else argv
    with open(argv[1], encoding="utf-8") as f:
        task = json.load(f)
    cfg = config.load()
    winproc.set_below_normal()
    return run_task(task, cfg, Client(cfg["octoprint_url"], cfg["api_key"]))


if __name__ == "__main__":
    sys.exit(main())
