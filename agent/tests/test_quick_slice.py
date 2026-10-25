"""Tests for remote_slicing_agent.quick_slice (Explorer's right-click "Slice and upload")."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np

from remote_slicing_agent import mesh, quick_slice as qs
from remote_slicing_agent.client import ApiError

BED = {"width": 350.0, "depth": 340.0, "height": 330.0, "center_is_zero": False, "edge_disallowed_size": 3.0}


def snapshot(default=0, enabled=(True, True, True, True), bed=BED):
    return {"bed": dict(bed),
            "summary": {"extruders": [{"position": i, "enabled": e} for i, e in enumerate(enabled)]},
            "initial_extruder_inputs": {"default_extruder": default}}


def box_stl(path, sx, sy, sz):
    v = np.array([[0, 0, 0], [sx, 0, 0], [sx, sy, 0], [0, sy, 0],
                  [0, 0, sz], [sx, 0, sz], [sx, sy, sz], [0, sy, sz]], dtype=float) + [100, 50, 7]
    idx = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 4, 7], [0, 7, 3],
           [1, 2, 6], [1, 6, 5], [0, 1, 5], [0, 5, 4], [3, 7, 6], [3, 6, 2]]
    mesh.write_binary_stl(path, v[np.array(idx)])


class DecisionTests(unittest.TestCase):
    def test_bed_center(self):
        self.assertEqual(qs.bed_center(BED), (175.0, 170.0))
        self.assertEqual(qs.bed_center(dict(BED, center_is_zero=True)), (0.0, 0.0))

    def test_center_lands_in_the_middle_of_the_engine_bed(self):
        # what the agent does with the item: the part's centre ends at the engine's origin (bed centre)
        tris = np.array([[[0, 0, 5], [20, 0, 5], [20, 10, 9]]], dtype=float)
        x, y = qs.bed_center(BED)
        placed = mesh.place(tris, x, y, 0, BED["width"], BED["depth"], False)
        pts = placed.reshape(-1, 3)
        np.testing.assert_allclose((pts.min(0) + pts.max(0))[:2] / 2, (0, 0))
        self.assertEqual(pts[:, 2].min(), 0)

    def test_choose_extruder(self):
        self.assertEqual(qs.choose_extruder(snapshot(default=2)), 2)
        self.assertEqual(qs.choose_extruder(snapshot(default=0, enabled=(False, True, True, True))), 1)
        snap = snapshot()
        del snap["initial_extruder_inputs"]
        self.assertEqual(qs.choose_extruder(snap), 0)
        with self.assertRaises(qs.QuickSliceError):
            qs.choose_extruder(snapshot(enabled=(False, False)))

    def test_check_fits_leaves_room_for_curas_border(self):
        qs.check_fits((344, 334, 330), BED)
        for size in ((345, 10, 10), (10, 335, 10), (10, 10, 331)):
            with self.assertRaisesRegex(qs.QuickSliceError, "printable space is 344 x 334 x 330"):
                qs.check_fits(size, BED)

    def test_slice_item(self):
        item = qs.slice_item("m1", snapshot(default=3))
        self.assertEqual(item, {"model_id": "m1", "x": 175.0, "y": 170.0, "rotation": 0, "tilt_x": 0, "tilt_y": 0,
                                "extruder": 3})

    def test_describe(self):
        self.assertEqual(qs.describe({"state": "queued"}, False)[2:], (False, False))
        progress, text, finished, ok = qs.describe({"state": "running", "progress": 0.4, "stage": "slicing: export"},
                                                   False)
        self.assertEqual((progress, text, finished), (0.4, "Slicing: export…", False))
        done = {"state": "done", "result": {"path": "RemoteSlicing/a_1.gcode"}}
        self.assertEqual(qs.describe(done, False)[1:], ("Uploaded to OctoPrint:\nRemoteSlicing/a_1.gcode", True, True))
        self.assertEqual(qs.describe({"state": "running", "stage": "claimed"}, False)[1], "The PC agent picked it up…")
        self.assertEqual(qs.describe(done, True)[2:], (False, False))  # waiting for PrintStarted
        self.assertEqual(qs.describe(done, True, qs.PRINT_CONFIRM_S + 1)[2:], (True, False))
        started = {"state": "done", "result": {"path": "p.gcode", "print_started": True}}
        self.assertEqual(qs.describe(started, True)[1:], ("Printing:\np.gcode", True, True))
        busy = {"state": "done", "result": {"path": "p.gcode", "print_started": False,
                                            "print_note": "printer is Printing, the file was only uploaded"}}
        self.assertIn("didn't start: printer is Printing", qs.describe(busy, True)[1])
        self.assertEqual(qs.describe({"state": "failed", "error": "CuraEngine: boom"}, True)[1:],
                         ("Slicing failed: CuraEngine: boom", True, False))
        self.assertEqual(qs.describe(None, False)[2:], (True, False))


class FakeClient:
    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def add_library_model(self, path, filename):
        self.calls.append(("add", os.path.basename(path), filename, os.path.getsize(path)))
        return {"id": "m1", "name": filename}

    def submit_slice(self, name, items, start_print):
        self.calls.append(("slice", name, items, start_print))
        return {"id": "t1", "state": "queued"}

    def state(self):
        self.calls.append(("state",))
        current = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(current, Exception):
            raise current
        return {"tasks": [dict(current, id="t1")] if current else []}

    def cancel_task(self, task_id):
        self.calls.append(("cancel", task_id))


class JobTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"REMOTE_SLICING_HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.home, True)
        with open(os.path.join(self.home, "snapshot.json"), "w") as f:
            json.dump(snapshot(default=1), f)
        self.cfg = {"octoprint_url": "http://printer", "api_key": "k", "solidworks_plugin_dir": ""}
        self.reports = []
        for name in ("POLL_S",):
            p = mock.patch.object(qs, name, 0.001)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(qs.winproc, "instance_running", return_value=True)
        self.running = p.start()
        self.addCleanup(p.stop)

    def report(self, *args):
        self.reports.append(args)

    def job(self, path, start_print, client):
        return qs.QuickSlice(path, start_print, self.cfg, client, self.report)

    def test_stl_upload_slice_and_follow(self):
        stl = os.path.join(self.home, "Bracket v2.stl")
        box_stl(stl, 40, 20, 10)
        client = FakeClient([{"state": "queued"}, {"state": "running", "progress": 0.5, "stage": "slicing: export"},
                             {"state": "done", "result": {"path": "RemoteSlicing/Bracket_v2_1.gcode"}}])
        self.job(stl, False, client).run()
        self.assertEqual(client.calls[0][:3], ("add", "Bracket v2.stl", "Bracket v2.stl"))
        self.assertEqual(client.calls[1], ("slice", "Bracket v2", [
            {"model_id": "m1", "x": 175.0, "y": 170.0, "rotation": 0, "tilt_x": 0, "tilt_y": 0, "extruder": 1}],
            False))
        self.assertEqual(self.reports[-1], (1.0, "Uploaded to OctoPrint:\nRemoteSlicing/Bracket_v2_1.gcode",
                                            True, True))
        self.assertIn((0.5, "Slicing: export…", False, False), self.reports)
        self.assertEqual(os.listdir(os.path.join(self.home, "work")), [])  # temp folder removed

    def test_print_waits_for_the_print_to_start(self):
        stl = os.path.join(self.home, "a.stl")
        box_stl(stl, 10, 10, 10)
        done = {"state": "done", "result": {"path": "p.gcode"}}
        client = FakeClient([done, done, dict(done, result={"path": "p.gcode", "print_started": True})])
        self.job(stl, True, client).run()
        self.assertTrue(client.calls[1][3])
        self.assertEqual(self.reports[-1], (1.0, "Printing:\np.gcode", True, True))

    def test_too_big_fails_before_anything_is_sent(self):
        stl = os.path.join(self.home, "big.stl")
        box_stl(stl, 400, 10, 10)
        client = FakeClient([None])
        self.job(stl, False, client).run()
        self.assertEqual(client.calls, [])
        self.assertIn("400 x 10 x 10 mm", self.reports[-1][1])
        self.assertEqual(self.reports[-1][2:], (True, False))

    def test_sldprt_is_converted_on_this_pc(self):
        part = os.path.join(self.home, "Cover.SLDPRT")
        open(part, "wb").close()

        def convert(src, stl, plugin_dir):
            self.assertEqual(src, part)
            box_stl(stl, 30, 30, 5)
        client = FakeClient([{"state": "done", "result": {"path": "x.gcode"}}])
        with mock.patch("remote_slicing_agent.solidworks.convert", side_effect=convert):
            self.job(part, False, client).run()
        self.assertEqual(client.calls[0][2], "Cover.stl")
        self.assertEqual(client.calls[1][1], "Cover")
        self.assertIn((None, "Converting in SolidWorks…", False, False), [r + (False, False)[len(r) - 2:]
                                                                         for r in self.reports if len(r) == 2])
        self.assertEqual(self.reports[-1][2:], (True, True))

    def test_starts_the_agent_when_it_is_not_running(self):
        stl = os.path.join(self.home, "a.stl")
        box_stl(stl, 10, 10, 10)
        self.running.return_value = False
        client = FakeClient([{"state": "done", "result": {"path": "p.gcode"}}])
        with mock.patch.object(qs, "start_agent") as start:
            self.job(stl, False, client).run()
        start.assert_called_once_with()

    def test_cancel_cancels_the_queued_job(self):
        stl = os.path.join(self.home, "a.stl")
        box_stl(stl, 10, 10, 10)
        client = FakeClient([{"state": "queued"}, {"state": "cancelled"}])
        job = self.job(stl, False, client)
        original = client.submit_slice

        def submit(*args):
            job.cancel.set()  # the user presses Cancel job right after it was queued
            return original(*args)
        client.submit_slice = submit
        job.run()
        self.assertIn(("cancel", "t1"), client.calls)
        self.assertEqual(self.reports[-1][1:], ("The job was cancelled.", True, False))

    def test_closing_before_upload_sends_nothing(self):
        stl = os.path.join(self.home, "a.stl")
        box_stl(stl, 10, 10, 10)
        client = FakeClient([None])
        job = self.job(stl, False, client)
        job.stop.set()
        job.run()
        self.assertEqual(client.calls, [])

    def test_errors_are_shown(self):
        stl = os.path.join(self.home, "a.stl")
        box_stl(stl, 10, 10, 10)
        client = FakeClient([None])
        client.add_library_model = mock.Mock(side_effect=ApiError(0, "timed out"))
        self.job(stl, False, client).run()
        self.assertEqual(self.reports[-1][1:], ("Can't reach OctoPrint at http://printer: timed out", True, False))
        client.add_library_model = mock.Mock(side_effect=ApiError(409, '{"error": "only .stl and .sldprt files"}'))
        self.job(stl, False, client).run()
        self.assertEqual(self.reports[-1][1], "OctoPrint refused it: only .stl and .sldprt files")
        self.job(os.path.join(self.home, "a.obj"), False, client).run()
        self.assertIn("Only .stl and .sldprt", self.reports[-1][1])
        self.cfg["api_key"] = ""
        self.job(stl, False, client).run()
        self.assertIn("setup", self.reports[-1][1])
        os.remove(os.path.join(self.home, "snapshot.json"))
        self.cfg["api_key"] = "k"
        self.job(stl, False, client).run()
        self.assertIn("open Cura once", self.reports[-1][1])

    def test_rides_out_a_short_network_drop(self):
        stl = os.path.join(self.home, "a.stl")
        box_stl(stl, 10, 10, 10)
        client = FakeClient([ApiError(0, "refused"), ApiError(0, "refused"),
                             {"state": "done", "result": {"path": "p.gcode"}}])
        self.job(stl, False, client).run()
        self.assertEqual(self.reports[-1][2:], (True, True))


if __name__ == "__main__":
    unittest.main()
