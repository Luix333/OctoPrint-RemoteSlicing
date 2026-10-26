"""Tests for remote_slicing_agent.quick_slice (Explorer's right-click "Slice...")."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np

from remote_slicing_agent import mesh, plate_layout as pl, quick_slice as qs
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
    def test_centre_item_lands_in_the_middle_of_the_engine_bed(self):
        # what the agent does with the item: the part's centre ends at the engine's origin (the bed centre)
        tris = np.array([[[0, 0, 5], [20, 0, 5], [20, 10, 9]]], dtype=float)
        part = pl.Part("p", tris)
        pl.place_group([part], BED, "center")
        placed = mesh.place(tris, part.x, part.y, 0, BED["width"], BED["depth"], False)
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

    def test_titles(self):
        self.assertEqual(qs.files_title([r"C:\a\Bracket.stl"]), "Bracket.stl")
        self.assertEqual(qs.files_title(["a.stl", "b.SLDPRT"]), "a.stl and b.SLDPRT")
        self.assertEqual(qs.files_title(["a", "b", "c"]), "a, b and c")
        self.assertEqual(qs.files_title(["a", "b", "c", "d", "e"]), "a, b, c and 2 more")
        self.assertEqual(qs.action_text(False, "center"), "Slice and upload (center)")
        self.assertEqual(qs.action_text(True, "top-left"), "Slice, upload and print (top-left)")
        self.assertEqual(qs.action_text(True, edited=True), "Slice, upload and print (your layout)")
        self.assertEqual(qs.action_key(True, "btm-mid", False), "print-btm-mid")
        self.assertEqual(qs.action_key(False, "center", True), "options")

    def test_plate_name_lists_each_part_once(self):
        parts = [pl.Part(n, np.zeros((0, 3, 3))) for n in ("Bracket", "Lid", "Bracket")]
        self.assertEqual(qs.plate_name(parts), "Bracket, Lid")

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
        self.models = 0

    def add_library_model(self, path, filename):
        self.models += 1
        self.calls.append(("add", os.path.basename(path), filename, os.path.getsize(path)))
        return {"id": "m%d" % self.models, "name": filename}

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

    def slices(self):
        return [c for c in self.calls if c[0] == "slice"]


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
        p = mock.patch.object(qs, "POLL_S", 0.001)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(qs.winproc, "instance_running", return_value=True)
        self.running = p.start()
        self.addCleanup(p.stop)

    def report(self, *args):
        self.reports.append(args)

    def job(self, paths, start_print, client, **kw):
        return qs.QuickSlice(paths if isinstance(paths, list) else [paths], start_print, self.cfg, client,
                             self.report, **kw)

    def stl(self, name, sx, sy, sz):
        path = os.path.join(self.home, name)
        box_stl(path, sx, sy, sz)
        return path

    def test_one_stl_in_the_middle(self):
        stl = self.stl("Bracket v2.stl", 40, 20, 10)
        client = FakeClient([{"state": "queued"}, {"state": "running", "progress": 0.5, "stage": "slicing: export"},
                             {"state": "done", "result": {"path": "RemoteSlicing/Bracket_v2_1.gcode"}}])
        self.job(stl, False, client).run()
        self.assertEqual(client.calls[0][:3], ("add", "Bracket v2.stl", "Bracket v2.stl"))
        self.assertEqual(client.slices(), [("slice", "Bracket v2", [
            {"model_id": "m1", "x": 175.0, "y": 170.0, "rotation": 0.0, "tilt_x": 0, "tilt_y": 0, "extruder": 1}],
            False)])
        self.assertEqual(self.reports[-1], (1.0, "Uploaded to OctoPrint:\nRemoteSlicing/Bracket_v2_1.gcode",
                                            True, True))
        self.assertIn((0.5, "Slicing: export…", False, False), self.reports)
        self.assertEqual(os.listdir(os.path.join(self.home, "work")), [])  # temp folder removed

    def test_several_files_make_one_job_in_the_top_left_corner(self):
        paths = [self.stl("A.stl", 40, 20, 10), self.stl("B.stl", 30, 30, 5)]
        client = FakeClient([{"state": "done", "result": {"path": "p.gcode", "print_started": True}}])
        self.job(paths, True, client, place="top-left").run()
        (_, name, items, start_print), = client.slices()
        self.assertEqual((name, start_print), ("A, B", True))
        self.assertEqual([i["model_id"] for i in items], ["m1", "m2"])
        # border 3 + margin 10 on the left/back; A (40 wide) then a 10 mm gap, then B; tops of the row aligned
        self.assertEqual((items[0]["x"], items[1]["x"]), (13 + 20, 13 + 40 + 10 + 15))
        self.assertEqual(items[1]["y"], 340 - 13 - 15)
        self.assertEqual(items[0]["y"], items[1]["y"])  # one row, centred on the row's middle line
        self.assertEqual(self.reports[-1], (1.0, "Printing:\np.gcode", True, True))

    def test_print_waits_for_the_print_to_start(self):
        stl = self.stl("a.stl", 10, 10, 10)
        done = {"state": "done", "result": {"path": "p.gcode"}}
        client = FakeClient([done, done, dict(done, result={"path": "p.gcode", "print_started": True})])
        self.job(stl, True, client).run()
        self.assertTrue(client.slices()[0][3])
        self.assertEqual(self.reports[-1], (1.0, "Printing:\np.gcode", True, True))

    def test_too_big_fails_before_anything_is_sent(self):
        stl = self.stl("big.stl", 400, 10, 10)
        client = FakeClient([None])
        self.job(stl, False, client).run()
        self.assertEqual(client.calls, [])
        self.assertIn("400 x 10 mm", self.reports[-1][1])
        self.assertIn("More options", self.reports[-1][1])
        self.assertEqual(self.reports[-1][2:], (True, False))

    def test_too_many_together_fails_and_says_so(self):
        paths = [self.stl("p%d.stl" % i, 150, 150, 5) for i in range(5)]
        client = FakeClient([None])
        self.job(paths, False, client).run()
        self.assertEqual(client.calls, [])
        self.assertIn("don't fit on the bed together", self.reports[-1][1])

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
        self.assertEqual(client.slices()[0][1], "Cover")
        self.assertIn((None, "Converting Cover.SLDPRT in SolidWorks…"), self.reports)
        self.assertEqual(self.reports[-1][2:], (True, True))

    def test_editor_layout_is_sent_and_copies_share_one_upload(self):
        paths = [self.stl("A.stl", 40, 20, 10), self.stl("B.stl", 30, 30, 5)]
        seen = {}

        def edit(parts, snap):
            seen["start"] = [(p.name, p.x, p.y) for p in parts]  # handed over already arranged in the middle
            a, b = parts
            a.x, a.y, a.rotation, a.tilt_x, a.extruder = 60, 70, 30, 90, 3
            twin = b.copy()
            twin.x += 50
            return [a, b, twin], True
        client = FakeClient([{"state": "done", "result": {"path": "p.gcode", "print_started": True}}])
        self.job(paths, False, client, edit=edit).run()
        self.assertTrue(all(abs(y - 170) < 1e-6 for _n, _x, y in seen["start"]))
        self.assertEqual([c[2] for c in client.calls if c[0] == "add"], ["A.stl", "B.stl"])  # B uploaded once
        (_, name, items, start_print), = client.slices()
        self.assertTrue(start_print)  # the editor's button decides
        self.assertEqual(items[0], {"model_id": "m1", "x": 60, "y": 70, "rotation": 30, "tilt_x": 90, "tilt_y": 0,
                                    "extruder": 3})
        self.assertEqual([i["model_id"] for i in items], ["m1", "m2", "m2"])
        self.assertEqual(items[2]["x"], items[1]["x"] + 50)

    def test_cancelled_editor_sends_nothing(self):
        client = FakeClient([None])
        self.job(self.stl("a.stl", 10, 10, 10), False, client, edit=lambda parts, snap: None).run()
        self.assertEqual(client.calls, [])
        self.assertEqual(self.reports[-1][1:], ("Cancelled; nothing was sent to OctoPrint.", True, False))

    def test_centre_zero_beds_get_centre_origin_items(self):
        with open(os.path.join(self.home, "snapshot.json"), "w") as f:
            json.dump(snapshot(bed=dict(BED, center_is_zero=True)), f)
        client = FakeClient([{"state": "done", "result": {"path": "p.gcode"}}])
        self.job(self.stl("a.stl", 10, 10, 10), False, client).run()
        item = client.slices()[0][2][0]
        self.assertEqual((item["x"], item["y"]), (0.0, 0.0))

    def test_starts_the_agent_when_it_is_not_running(self):
        self.running.return_value = False
        client = FakeClient([{"state": "done", "result": {"path": "p.gcode"}}])
        with mock.patch.object(qs, "start_agent") as start:
            self.job(self.stl("a.stl", 10, 10, 10), False, client).run()
        start.assert_called_once_with()

    def test_cancel_cancels_the_queued_job(self):
        client = FakeClient([{"state": "queued"}, {"state": "cancelled"}])
        job = self.job(self.stl("a.stl", 10, 10, 10), False, client)
        original = client.submit_slice

        def submit(*args):
            job.cancel.set()  # the user presses Cancel job right after it was queued
            return original(*args)
        client.submit_slice = submit
        job.run()
        self.assertIn(("cancel", "t1"), client.calls)
        self.assertEqual(self.reports[-1][1:], ("The job was cancelled.", True, False))

    def test_closing_before_upload_sends_nothing(self):
        client = FakeClient([None])
        job = self.job(self.stl("a.stl", 10, 10, 10), False, client)
        job.stop.set()
        job.run()
        self.assertEqual(client.calls, [])

    def test_errors_are_shown(self):
        stl = self.stl("a.stl", 10, 10, 10)
        client = FakeClient([None])
        client.add_library_model = mock.Mock(side_effect=ApiError(0, "timed out"))
        self.job(stl, False, client).run()
        self.assertEqual(self.reports[-1][1:], ("Can't reach OctoPrint at http://printer: timed out", True, False))
        client.add_library_model = mock.Mock(side_effect=ApiError(409, '{"error": "only .stl and .sldprt files"}'))
        self.job(stl, False, client).run()
        self.assertEqual(self.reports[-1][1], "OctoPrint refused it: only .stl and .sldprt files")
        self.job([stl, os.path.join(self.home, "a.obj")], False, client).run()
        self.assertIn("a.obj isn't one", self.reports[-1][1])
        self.cfg["api_key"] = ""
        self.job(stl, False, client).run()
        self.assertIn("setup", self.reports[-1][1])
        os.remove(os.path.join(self.home, "snapshot.json"))
        self.cfg["api_key"] = "k"
        self.job(stl, False, client).run()
        self.assertIn("open Cura once", self.reports[-1][1])

    def test_rides_out_a_short_network_drop(self):
        client = FakeClient([ApiError(0, "refused"), ApiError(0, "refused"),
                             {"state": "done", "result": {"path": "p.gcode"}}])
        self.job(self.stl("a.stl", 10, 10, 10), False, client).run()
        self.assertEqual(self.reports[-1][2:], (True, True))


if __name__ == "__main__":
    unittest.main()
