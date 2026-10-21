import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

from remote_slicing_agent import config, engine, mesh, worker
from remote_slicing_agent.client import ApiError


def _box():
    v = np.array([[x, y, z] for x in (0, 10) for y in (0, 20) for z in (0, 5)], dtype=np.float64)
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return v[np.array(faces)]


class FakeClient:
    def __init__(self, cancel=False, failed_raises=False):
        self.calls = []
        self.cancel = cancel
        self.failed_raises = failed_raises
        self.downloads = []

    def progress(self, task_id, progress, stage):
        self.calls.append(("progress", progress, stage))
        return {"ok": True, "cancel": self.cancel}

    def download_model(self, model_id, dest):
        self.downloads.append(model_id)
        mesh.write_binary_stl(dest, _box())

    def download_source(self, source_id, dest):
        open(dest, "wb").write(b"sldprt")

    def upload_gcode(self, local_path, filename):
        self.calls.append(("upload_gcode", open(local_path).read(), filename))
        return "RemoteSlicing/" + filename

    def upload_model(self, task_id, file_path, name):
        self.calls.append(("upload_model", task_id, file_path, name))

    def done(self, task_id, result):
        self.calls.append(("done", task_id, result))

    def failed(self, task_id, error):
        if self.failed_raises:
            raise ApiError(0, "offline")
        self.calls.append(("failed", task_id, error))

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class FakeParser:
    header = [";FLAVOR:X", ";TIME:42"]
    print_time_s = 42
    filament_mm3 = 3.5


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._env = os.environ.get("REMOTE_SLICING_HOME")
        os.environ["REMOTE_SLICING_HOME"] = self.home
        none = {k: -1 for k in ("wall_0_extruder_nr", "wall_x_extruder_nr", "roofing_extruder_nr",
                                "flooring_extruder_nr", "top_bottom_extruder_nr", "infill_extruder_nr")}
        none.update({"adhesion_type": "none", "support_enable": False, "support_structure": "normal",
                     "support_brim_enable": False, "default_extruder": 0})
        self.snapshot = {
            "engine": {"path": sys.executable},
            "bed": {"width": 350.0, "depth": 350.0, "center_is_zero": False},
            "summary": {"extruders": [{"position": 0, "enabled": True}, {"position": 1, "enabled": False}]},
            "initial_extruder_inputs": none,
            "resolved": {"global": {"machine_start_gcode": "OLD"}, "extruder.0": {}, "extruder.1": {}},
            "gcode_by_initial_extruder": {"0": {"machine_start_gcode": "START0", "machine_end_gcode": "END0"}},
        }
        self.write_snapshot()
        self.cmds = []
        self.resolved = []

    def tearDown(self):
        if self._env is None:
            os.environ.pop("REMOTE_SLICING_HOME", None)
        else:
            os.environ["REMOTE_SLICING_HOME"] = self._env
        shutil.rmtree(self.home, ignore_errors=True)

    def write_snapshot(self):
        with open(config.snapshot_path(), "w") as f:
            json.dump(self.snapshot, f)

    def fake_run(self, cmd, on_progress=None, cwd=None, cancelled=None):
        self.cmds.append(cmd)
        self.resolved.append(json.load(open(cmd[cmd.index("-r") + 1])))
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w", newline="") as f:
            f.write(";FLAVOR:X\n;TIME:6666\n\n;Generated with Cura_SteamEngine\nG1 X1\n")
        on_progress(0.5, "process")
        return FakeParser()

    def slice_task(self, extruders=(0, 0), name="plate"):
        items = [{"model_id": "abc123", "x": 50 + 30 * i, "y": 60, "rotation": 0, "extruder": e}
                 for i, e in enumerate(extruders)]
        return {"id": "t1", "kind": "slice", "payload": {"name": name, "items": items, "start_print": False}}

    def run_task(self, task, client, cfg=None):
        return worker.run_task(task, cfg or {"dropbox_root": self.home}, client)

    def test_slice_uploads_and_reports_done(self):
        client = FakeClient()
        with mock.patch.object(engine, "run", self.fake_run):
            self.assertEqual(self.run_task(self.slice_task(), client), 0)
        done = client.named("done")
        self.assertEqual(len(done), 1)
        result = done[0][2]
        self.assertTrue(result["path"].startswith("RemoteSlicing/plate_"))
        self.assertEqual(result["print_time_s"], 42)
        self.assertEqual(result["initial_extruder"], 0)
        gcode = client.named("upload_gcode")[0][1]
        self.assertTrue(gcode.startswith(";FLAVOR:X\n;TIME:42\n;Generated with"))
        cmd = self.cmds[0]
        self.assertEqual(cmd.count("-e0"), 2)
        self.assertEqual(sum(1 for a in cmd if a.endswith(".stl")), 2)
        self.assertEqual(self.resolved[0]["global"]["machine_start_gcode"], "START0")
        self.assertEqual(client.downloads, ["abc123"])  # one download for two copies
        self.assertFalse(os.path.exists(os.path.join(config.work_dir(), "t1")))
        progress = [c[1] for c in client.named("progress")]
        self.assertEqual(progress, sorted(progress))

    def test_tilt_is_passed_to_placement(self):
        task = self.slice_task()
        task["payload"]["items"][0].update({"tilt_x": 90, "tilt_y": 270})  # second item has no tilt keys
        calls = []
        real_place = mesh.place

        def spy(*args, **kwargs):
            calls.append((kwargs.get("tilt_x"), kwargs.get("tilt_y")))
            return real_place(*args, **kwargs)

        with mock.patch.object(engine, "run", self.fake_run), mock.patch.object(mesh, "place", spy):
            self.assertEqual(self.run_task(task, FakeClient()), 0)
        self.assertEqual(calls, [(90.0, 270.0), (0.0, 0.0)])

    def test_disabled_and_missing_extruders(self):
        for extruders, message in (((1,), "T1 is disabled in Cura"), ((5,), "T5 does not exist in Cura")):
            client = FakeClient()
            with mock.patch.object(engine, "run", self.fake_run):
                self.run_task(self.slice_task(extruders), client)
            self.assertEqual(client.named("failed")[0][2], message)

    def test_missing_snapshot(self):
        os.remove(config.snapshot_path())
        client = FakeClient()
        self.run_task(self.slice_task(), client)
        self.assertTrue(client.named("failed")[0][2].startswith("No Cura settings snapshot"))

    def test_cancel_and_engine_errors(self):
        for error, message in ((engine.EngineError("cancelled"), "cancelled"),
                               (engine.EngineError("boom"), "CuraEngine: boom"),
                               (RuntimeError("boom"), "RuntimeError: boom")):
            client = FakeClient()
            with mock.patch.object(engine, "run", side_effect=error), mock.patch("traceback.print_exc"):
                self.assertEqual(self.run_task(self.slice_task(), client), 0)
            self.assertEqual(client.named("failed")[0][2], message)

    def test_failed_report_failing_gives_exit_3(self):
        os.remove(config.snapshot_path())
        with mock.patch("traceback.print_exc"):
            self.assertEqual(self.run_task(self.slice_task(), FakeClient(failed_raises=True)), 3)

    def test_fetch_stl_and_bad_path(self):
        os.makedirs(os.path.join(self.home, "a"))
        mesh.write_binary_stl(os.path.join(self.home, "a", "b.STL"), _box())
        client = FakeClient()
        self.assertEqual(self.run_task({"id": "t2", "kind": "fetch", "payload": {"path": "a/b.STL"}}, client), 0)
        upload = client.named("upload_model")[0]
        self.assertEqual((upload[1], upload[3]), ("t2", "b.stl"))
        client = FakeClient()
        self.run_task({"id": "t3", "kind": "fetch", "payload": {"path": "../x.stl"}}, client)
        self.assertEqual(client.named("failed")[0][2], "bad path")

    def test_preview_uploads_like_fetch(self):
        os.makedirs(os.path.join(self.home, "Print"))
        mesh.write_binary_stl(os.path.join(self.home, "Print", "knight.STL"), _box())
        client = FakeClient()
        self.assertEqual(self.run_task({"id": "t9", "kind": "preview", "payload": {"path": "Print/knight.STL"}}, client), 0)
        upload = client.named("upload_model")[0]
        self.assertEqual((upload[1], upload[3]), ("t9", "knight.stl"))
        self.assertEqual(client.named("failed"), [])

    def test_unknown_kind(self):
        client = FakeClient()
        self.run_task({"id": "t4", "kind": "nope", "payload": {}}, client)
        self.assertEqual(client.named("failed")[0][2], "unknown task kind")

    def test_reporter_cancel_and_keepalive(self):
        client = FakeClient(cancel=True)
        reporter = worker.Reporter(client, "t1", interval=0.05)
        reporter.report(0.3, "x")
        self.assertTrue(reporter.cancelled)
        reporter.start()
        time.sleep(0.25)
        reporter.stop()
        sent = len(client.named("progress"))
        self.assertGreaterEqual(sent, 3)
        time.sleep(0.15)
        self.assertEqual(len(client.named("progress")), sent)

    def test_safe_name(self):
        self.assertEqual(worker.safe_name("my plate!"), "my_plate")
        self.assertEqual(worker.safe_name(""), "plate")
        self.assertEqual(len(worker.safe_name("x" * 100)), 60)


if __name__ == "__main__":
    unittest.main()
