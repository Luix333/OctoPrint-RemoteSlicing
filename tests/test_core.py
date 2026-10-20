# coding=utf-8
import copy
import os
import sys
import unittest

# Import core.py directly, without the package __init__ (which needs OctoPrint).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "octoprint_remoteslicing"))

from core import Core  # noqa: E402


class FakeHost(object):
    """Minimal host stub for unit testing.  Time is controllable."""

    def __init__(self):
        self.t = 1000.0
        self.changes = 0
        self.wakes = []
        self._id_counter = 0

    def now(self):
        return self.t

    def changed(self):
        self.changes += 1

    def wake_agent(self, msg):
        self.wakes.append(msg)

    def new_id(self):
        self._id_counter += 1
        return "id{}".format(self._id_counter)


class CoreTests(unittest.TestCase):
    """Tests for the Remote Slicing core state machine."""

    def setUp(self):
        self.host = FakeHost()
        self.core = Core(self.host)

    # ----------------------------------------------------------------- models

    def test_add_model(self):
        m = self.core.add_model("cube", 12345, "upload")
        self.assertEqual(m["name"], "cube")
        self.assertEqual(m["size"], 12345)
        self.assertEqual(m["source"], "upload")
        self.assertEqual(m["origin"], "")
        self.assertIn("id", m)
        self.assertIn("created", m)
        self.assertEqual(self.host.changes, 1)

    def test_add_model_with_origin(self):
        m = self.core.add_model("gear", 999, "dropbox", origin="/Dropbox/gear.stl")
        self.assertEqual(m["origin"], "/Dropbox/gear.stl")

    def test_remove_model(self):
        m = self.core.add_model("cube", 100, "upload")
        self.host.changes = 0
        self.assertTrue(self.core.remove_model(m["id"]))
        self.assertEqual(self.host.changes, 1)
        self.assertIsNone(self.core.get_model(m["id"]))

    def test_remove_unknown_model(self):
        self.assertFalse(self.core.remove_model("nope"))
        self.assertEqual(self.host.changes, 0)

    def test_remove_model_in_use_raises(self):
        m = self.core.add_model("cube", 100, "upload")
        self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                          "rotation": 0, "extruder": 0}], False)
        with self.assertRaises(ValueError) as cm:
            self.core.remove_model(m["id"])
        self.assertIn("model in use", str(cm.exception))

    def test_get_model(self):
        m = self.core.add_model("cube", 100, "upload")
        found = self.core.get_model(m["id"])
        self.assertEqual(found["name"], "cube")
        self.assertIsNone(self.core.get_model("nope"))

    def test_models_newest_first(self):
        self.host.t = 100
        m1 = self.core.add_model("old", 10, "upload")
        self.host.t = 200
        m2 = self.core.add_model("new", 20, "upload")
        result = self.core.models()
        self.assertEqual(result[0]["id"], m2["id"])
        self.assertEqual(result[1]["id"], m1["id"])

    # --------------------------------------------------- submit_slice validation

    def test_submit_slice_empty_items(self):
        with self.assertRaises(ValueError) as cm:
            self.core.submit_slice("plate", [], False)
        self.assertIn("nothing to slice", str(cm.exception))

    def test_submit_slice_unknown_model(self):
        with self.assertRaises(ValueError) as cm:
            self.core.submit_slice("plate", [{"model_id": "nope", "x": 0, "y": 0,
                                              "rotation": 0, "extruder": 0}], False)
        self.assertIn("unknown model nope", str(cm.exception))

    def test_submit_slice_non_numeric_x(self):
        m = self.core.add_model("cube", 10, "upload")
        with self.assertRaises(ValueError):
            self.core.submit_slice("plate", [{"model_id": m["id"], "x": "abc",
                                              "y": 0, "rotation": 0, "extruder": 0}], False)

    def test_submit_slice_tilt(self):
        m = self.core.add_model("cube", 10, "upload")
        base = {"model_id": m["id"], "x": 0, "y": 0, "rotation": 0, "extruder": 0}
        task = self.core.submit_slice("plate", [dict(base, tilt_x=90, tilt_y=-90), dict(base)], False)
        items = task["payload"]["items"]
        self.assertEqual((items[0]["tilt_x"], items[0]["tilt_y"]), (90.0, 270.0))  # normalised to 0..359
        self.assertEqual((items[1]["tilt_x"], items[1]["tilt_y"]), (0.0, 0.0))     # missing = flat
        for bad in ({"tilt_x": 45}, {"tilt_y": "up"}):
            with self.assertRaises(ValueError):
                self.core.submit_slice("plate", [dict(base, **bad)], False)

    def test_submit_slice_bad_extruder_negative(self):
        m = self.core.add_model("cube", 10, "upload")
        with self.assertRaises(ValueError):
            self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                              "rotation": 0, "extruder": -1}], False)

    def test_submit_slice_extruder_vs_snapshot(self):
        m = self.core.add_model("cube", 10, "upload")
        # Agent snapshot with 1 extruder (T0 only)
        self.core.heartbeat({"version": "1.0", "snapshot": {
            "created": self.host.t,
            "summary": {"extruders": [{"name": "T0"}]},
            "bed": {"width": 220, "depth": 220},
        }})
        with self.assertRaises(ValueError) as cm:
            self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                              "rotation": 0, "extruder": 1}], False)
        self.assertIn("extruder T1 does not exist", str(cm.exception))

    def test_submit_slice_name_stripped(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("  ", [{"model_id": m["id"], "x": 0, "y": 0,
                                              "rotation": 0, "extruder": 0}], False)
        self.assertEqual(task["payload"]["name"], "plate")

    def test_submit_slice_wakes_agent(self):
        m = self.core.add_model("cube", 10, "upload")
        self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                          "rotation": 0, "extruder": 0}], False)
        self.assertTrue(any(w["type"] == "agent.wake" for w in self.host.wakes))

    # ------------------------------------------------ submit_fetch / convert

    def test_submit_fetch_wakes_agent(self):
        task = self.core.submit_fetch("models/cube.stl")
        self.assertEqual(task["kind"], "fetch")
        self.assertTrue(any(w["type"] == "agent.wake" for w in self.host.wakes))

    def test_submit_fetch_bad_path_empty(self):
        with self.assertRaises(ValueError) as cm:
            self.core.submit_fetch("")
        self.assertIn("bad path", str(cm.exception))

    def test_submit_fetch_bad_path_dots(self):
        with self.assertRaises(ValueError) as cm:
            self.core.submit_fetch("../etc/passwd")
        self.assertIn("bad path", str(cm.exception))

    def test_submit_fetch_bad_path_absolute_windows(self):
        with self.assertRaises(ValueError) as cm:
            self.core.submit_fetch("C:/x")
        self.assertIn("bad path", str(cm.exception))

    def test_submit_fetch_bad_path_absolute_unix(self):
        with self.assertRaises(ValueError) as cm:
            self.core.submit_fetch("/x")
        self.assertIn("bad path", str(cm.exception))

    def test_submit_convert_wakes_agent(self):
        task = self.core.submit_convert("src1", "gear.stl")
        self.assertEqual(task["kind"], "convert")
        self.assertTrue(any(w["type"] == "agent.wake" for w in self.host.wakes))

    # --------------------------------------------------------- claim (FIFO)

    def test_claim_fifo_order(self):
        m = self.core.add_model("cube", 10, "upload")
        t1 = self.core.submit_slice("plate1", [{"model_id": m["id"], "x": 0, "y": 0,
                                                "rotation": 0, "extruder": 0}], False)
        t2 = self.core.submit_slice("plate2", [{"model_id": m["id"], "x": 0, "y": 0,
                                                "rotation": 0, "extruder": 0}], False)
        claimed = self.core.claim()
        self.assertEqual(claimed["id"], t1["id"])

    def test_claim_empty_queue_returns_none(self):
        self.host.changes = 0
        result = self.core.claim()
        self.assertIsNone(result)
        # Heartbeat flips offline -> online, so changed() is called once
        self.assertEqual(self.host.changes, 1)

    def test_claim_marks_running(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        claimed = self.core.claim()
        self.assertEqual(claimed["state"], "running")
        self.assertEqual(claimed["attempts"], 1)
        self.assertEqual(claimed["progress"], 0)
        self.assertEqual(claimed["stage"], "claimed")
        self.assertIsNotNone(claimed["lease_until"])

    # ----------------------------------------------------------- progress

    def test_progress_renews_lease(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        claimed = self.core.claim()
        old_lease = claimed["lease_until"]
        self.host.t += 10
        resp = self.core.progress(task["id"], 0.5, "slicing")
        self.assertTrue(resp["ok"])
        # Re-fetch task to check renewed lease
        for t in self.core._tasks:
            if t["id"] == task["id"]:
                self.assertGreater(t["lease_until"], old_lease)
                break

    def test_progress_reports_cancel(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        self.core.claim()
        self.core.cancel(task["id"])
        resp = self.core.progress(task["id"], 0.3, "going")
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["cancel"])

    def test_progress_unknown_task(self):
        resp = self.core.progress("nope", 0.5, "x")
        self.assertFalse(resp["ok"])
        self.assertTrue(resp["cancel"])

    def test_progress_same_no_change(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        self.core.claim()
        # claimed sets progress=0, stage="claimed"
        self.host.changes = 0
        resp = self.core.progress(task["id"], 0, "claimed")
        self.assertTrue(resp["ok"])
        self.assertEqual(self.host.changes, 0)

    # ------------------------------------------------ lease expiry

    def test_expire_requeues(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        self.core.claim()
        self.host.t += 200  # past default lease_seconds=120
        self.core.expire()
        for t in self.core._tasks:
            if t["id"] == task["id"]:
                self.assertEqual(t["state"], "queued")
                self.assertIsNone(t["lease_until"])
                self.assertEqual(t["stage"], "re-queued")
                break

    def test_expire_fails_after_max_attempts(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        # Claim 3 times (default max_attempts=3), each time expiring the lease
        for _ in range(3):
            self.core.claim()
            self.host.t += 200
            self.core.expire()
            # After the last expire the task should fail (attempts=3 >= max_attempts)
        for t in self.core._tasks:
            if t["id"] == task["id"]:
                self.assertEqual(t["state"], "failed")
                self.assertEqual(t["error"], "the PC agent stopped responding")
                break

    # ------------------------------------------------ cancel

    def test_cancel_queued(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        self.assertTrue(self.core.cancel(task["id"]))
        for t in self.core._tasks:
            if t["id"] == task["id"]:
                self.assertEqual(t["state"], "cancelled")
                break

    def test_cancel_running_then_fail_gives_cancelled(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        self.core.claim()
        self.assertTrue(self.core.cancel(task["id"]))
        result = self.core.fail(task["id"], "oops")
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["error"], "cancelled")

    def test_cancel_finished_returns_false(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        self.core.claim()
        self.core.complete(task["id"], {"file": "out.gcode"})
        self.assertFalse(self.core.cancel(task["id"]))

    def test_cancel_unknown_returns_false(self):
        self.assertFalse(self.core.cancel("nope"))

    # ------------------------------------------------ complete / note_print

    def test_complete(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        self.core.claim()
        result = self.core.complete(task["id"], {"file": "out.gcode"})
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["progress"], 1)
        self.assertEqual(result["result"]["file"], "out.gcode")
        self.assertIsNone(result["lease_until"])

    def test_note_print(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        self.core.claim()
        self.core.complete(task["id"], {"file": "out.gcode"})
        self.core.note_print(task["id"], True, "looks good")
        for t in self.core._tasks:
            if t["id"] == task["id"]:
                self.assertTrue(t["result"]["print_started"])
                self.assertEqual(t["result"]["print_note"], "looks good")
                break

    # ------------------------------------------------ clear_finished

    def test_clear_finished(self):
        m = self.core.add_model("cube", 10, "upload")
        t1 = self.core.submit_slice("p1", [{"model_id": m["id"], "x": 0, "y": 0,
                                            "rotation": 0, "extruder": 0}], False)
        t2 = self.core.submit_slice("p2", [{"model_id": m["id"], "x": 0, "y": 0,
                                            "rotation": 0, "extruder": 0}], False)
        self.core.claim()
        self.core.complete(t1["id"], {})
        self.core.claim()
        self.core.fail(t2["id"], "err")
        removed = self.core.clear_finished()
        self.assertEqual(removed, 2)
        self.assertEqual(len(self.core._tasks), 0)

    # ------------------------------------------------ trimming

    def test_trimming_to_keep_finished(self):
        self.settings = {"keep_finished": 2}
        self.host2 = FakeHost()
        core2 = Core(self.host2, lambda: self.settings)
        m = core2.add_model("cube", 10, "upload")
        for _ in range(5):
            task = core2.submit_slice("p", [{"model_id": m["id"], "x": 0, "y": 0,
                                             "rotation": 0, "extruder": 0}], False)
            core2.claim()
            core2.complete(task["id"], {})
        finished = [t for t in core2._tasks if t["state"] in ("done", "failed", "cancelled")]
        self.assertLessEqual(len(finished), 2)

    # ------------------------------------------------ restart rehydration

    def test_restart_running_task_goes_queued(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        self.core.claim()
        state = self.core.persistent_state()
        self.host2 = FakeHost()
        core2 = Core(self.host2, state=state)
        for t in core2._tasks:
            if t["id"] == task["id"]:
                self.assertEqual(t["state"], "queued")
                self.assertIsNone(t["lease_until"])
                break

    # ------------------------------------------------ heartbeat

    def test_heartbeat_stores_snapshot(self):
        resp = self.core.heartbeat({"version": "1.0", "snapshot": {
            "created": self.host.t,
            "summary": {"extruders": [{"name": "T0"}]},
            "bed": {"width": 220, "depth": 220},
        }})
        self.assertTrue(resp["ok"])
        status = self.core.agent_status()
        self.assertTrue(status["online"])
        self.assertEqual(status["version"], "1.0")
        self.assertIsNotNone(status["snapshot"])

    def test_heartbeat_flips_online_offline(self):
        self.core.heartbeat({"version": "1.0", "snapshot": None})
        self.host.t += 200  # past default agent_timeout=150
        status = self.core.agent_status()
        self.assertFalse(status["online"])

    def test_repeat_heartbeat_no_change(self):
        self.core.heartbeat({"version": "1.0", "snapshot": None})
        self.host.changes = 0
        self.core.heartbeat({"version": "1.0", "snapshot": None})
        self.assertEqual(self.host.changes, 0)

    # ------------------------------------------------ listing

    def test_listing_request_and_complete(self):
        rid = self.core.request_listing("models")
        req = self.core.complete_listing(rid)
        self.assertEqual(req["path"], "models")
        self.assertIn("created", req)

    def test_listing_timeout(self):
        rid = self.core.request_listing("models")
        self.host.t += 20  # past default listing_timeout=10
        self.assertIsNone(self.core.complete_listing(rid))

    def test_listing_bad_path_dots(self):
        with self.assertRaises(ValueError) as cm:
            self.core.request_listing("..")
        self.assertIn("bad path", str(cm.exception))

    def test_listing_bad_path_absolute_windows(self):
        with self.assertRaises(ValueError) as cm:
            self.core.request_listing("C:/x")
        self.assertIn("bad path", str(cm.exception))

    def test_listing_bad_path_absolute_unix(self):
        with self.assertRaises(ValueError) as cm:
            self.core.request_listing("/x")
        self.assertIn("bad path", str(cm.exception))

    def test_listing_root_allowed(self):
        rid = self.core.request_listing("")
        req = self.core.complete_listing(rid)
        self.assertEqual(req["path"], "")

    # ------------------------------------------------ persistent_state round-trip

    def test_persistent_state_round_trip(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        self.core.heartbeat({"version": "1.0", "snapshot": {
            "created": self.host.t,
            "summary": {"extruders": [{"name": "T0"}]},
            "bed": {"width": 220},
        }})
        state = self.core.persistent_state()
        self.host2 = FakeHost()
        core2 = Core(self.host2, state=state)
        # Models survived
        self.assertEqual(len(core2._models), 1)
        self.assertEqual(core2._models[0]["id"], m["id"])
        # Tasks survived
        self.assertEqual(len(core2._tasks), 1)
        self.assertEqual(core2._tasks[0]["id"], task["id"])
        # Agent state survived
        status = core2.agent_status()
        self.assertEqual(status["version"], "1.0")
        self.assertIsNotNone(status["snapshot"])

    # ------------------------------------------------ snapshot calls expire

    def test_snapshot_calls_expire(self):
        m = self.core.add_model("cube", 10, "upload")
        task = self.core.submit_slice("plate", [{"model_id": m["id"], "x": 0, "y": 0,
                                                 "rotation": 0, "extruder": 0}], False)
        self.core.claim()
        self.host.t += 200
        snap = self.core.snapshot()
        # The task should be expired (re-queued) by now
        for t in snap["tasks"]:
            if t["id"] == task["id"]:
                self.assertEqual(t["state"], "queued")
                break

    # ------------------------------------------------ agent_status

    def test_agent_status_initial(self):
        status = self.core.agent_status()
        self.assertFalse(status["online"])
        self.assertIsNone(status["last_seen"])
        self.assertIsNone(status["version"])
        self.assertIsNone(status["snapshot"])

    # ------------------------------------------------ deep copy safety

    def test_persistent_state_deep_copy(self):
        m = self.core.add_model("cube", 10, "upload")
        state1 = self.core.persistent_state()
        self.core.add_model("sphere", 20, "upload")
        self.assertEqual(len(state1["models"]), 1)

    # ------------------------------------------------ fail unknown task

    def test_fail_unknown(self):
        self.assertIsNone(self.core.fail("nope", "err"))

    def test_complete_unknown(self):
        self.assertIsNone(self.core.complete("nope", {}))


if __name__ == "__main__":
    unittest.main()
