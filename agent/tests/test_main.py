import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from remote_slicing_agent import config, main


class FakeClient:
    def __init__(self, tasks=()):
        self.tasks = list(tasks)
        self.heartbeats, self.claims, self.failures, self.listings = [], 0, [], []

    def heartbeat(self, info):
        self.heartbeats.append(info)
        return {"ok": True, "queued": len(self.tasks)}

    def claim(self):
        self.claims += 1
        return self.tasks.pop(0) if self.tasks else None

    def failed(self, task_id, error):
        self.failures.append((task_id, error))

    def listing(self, request_id, data):
        self.listings.append((request_id, data))


class FakeSock:
    """Plays a script: dicts are messages, None a timeout, exceptions are raised; then ConnectionError."""

    def __init__(self, script, clock):
        self.script = list(script)
        self.clock = clock
        self.pings = 0

    def connect(self):
        pass

    def next_message(self, timeout):
        self.clock.t += timeout
        if not self.script:
            raise ConnectionError("end of script")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def ping(self):
        self.pings += 1

    def close(self):
        pass


class FakeProc:
    def __init__(self, results):
        self.results = list(results)

    def poll(self):
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._env = os.environ.get("REMOTE_SLICING_HOME")
        os.environ["REMOTE_SLICING_HOME"] = self.home
        self.clock = Clock()
        self.spawned = []
        self.procs = []
        self.patches = [mock.patch.object(config, "snapshot_stat", return_value=(1, 2)),
                        mock.patch.object(config, "read_snapshot_summary", return_value={"summary": {}}),
                        mock.patch("remote_slicing_agent.winproc.empty_working_set")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        if self._env is None:
            os.environ.pop("REMOTE_SLICING_HOME", None)
        else:
            os.environ["REMOTE_SLICING_HOME"] = self._env
        shutil.rmtree(self.home, ignore_errors=True)

    def agent(self, client, script, sleeps=None):
        sock = FakeSock(script, self.clock)

        def spawn(task, path):
            self.spawned.append((task, path, os.path.exists(path)))
            return self.procs.pop(0)

        agent = main.Agent({"octoprint_url": "http://x", "api_key": "k", "dropbox_root": self.home,
                            "heartbeat_seconds": 60}, client_obj=client, push_factory=lambda: sock,
                           spawn=spawn, clock=self.clock, sleep=(sleeps.append if sleeps is not None else lambda s: None))
        return agent, sock

    def run_script(self, agent):
        with self.assertRaises(ConnectionError):
            agent.run_once_connected()

    def test_connect_sends_forced_snapshot_and_claims(self):
        client = FakeClient()
        agent, _ = self.agent(client, [])
        self.run_script(agent)
        self.assertEqual(client.heartbeats[0]["snapshot"], {"summary": {}})
        self.assertEqual(client.claims, 1)

    def test_wake_spawns_and_second_wake_is_ignored_while_running(self):
        client = FakeClient([{"id": "t1", "kind": "slice"}])
        self.procs = [FakeProc([None])]
        agent, _ = self.agent(client, [None, {"type": "agent.wake"}, {"type": "agent.wake"}])
        self.run_script(agent)
        # the connect-time claim got the task; later wakes don't claim while it runs
        self.assertEqual(len(self.spawned), 1)
        task, path, existed = self.spawned[0]
        self.assertEqual(task["id"], "t1")
        self.assertTrue(existed)
        self.assertEqual(client.claims, 1)

    def test_finished_job_claims_next(self):
        client = FakeClient([{"id": "t1", "kind": "slice"}, {"id": "t2", "kind": "fetch"}])
        self.procs = [FakeProc([None, 0]), FakeProc([None])]
        agent, _ = self.agent(client, [None, None])
        self.run_script(agent)
        self.assertEqual([t["id"] for t, _, _ in self.spawned], ["t1", "t2"])
        self.assertFalse(os.path.exists(os.path.join(config.work_dir(), "t1.json")))

    def test_crashed_worker_reports_failure_with_log_tail(self):
        client = FakeClient([{"id": "t1", "kind": "slice"}])
        self.procs = [FakeProc([None, 1])]
        agent, _ = self.agent(client, [None, None])
        os.makedirs(config.work_dir(), exist_ok=True)
        with open(os.path.join(config.work_dir(), "t1.log"), "w") as f:
            f.write("a\nTraceback b\n")
        with self.assertLogs("remote_slicing_agent", "WARNING"):
            self.run_script(agent)
        self.assertEqual(client.failures[0][0], "t1")
        self.assertIn("exit code 1", client.failures[0][1])
        self.assertIn("Traceback b", client.failures[0][1])
        self.assertIsNone(agent.job)

    def test_listing(self):
        open(os.path.join(self.home, "part.stl"), "w").close()
        client = FakeClient()
        agent, _ = self.agent(client, [{"type": "agent.list", "request_id": "r1", "path": ""},
                                       {"type": "agent.list", "request_id": "r2", "path": "../x"}])
        self.run_script(agent)
        (rid1, ok), (rid2, bad) = client.listings
        self.assertEqual((rid1, ok["error"], [f["name"] for f in ok["files"]]), ("r1", None, ["part.stl"]))
        self.assertEqual((rid2, bad["error"]), ("r2", "bad path"))

    def test_heartbeat_sends_snapshot_only_when_it_changed(self):
        client = FakeClient()
        agent, _ = self.agent(client, [])
        agent.heartbeat(force_snapshot=True)
        agent.heartbeat()
        with mock.patch.object(config, "snapshot_stat", return_value=(9, 9)):
            agent.heartbeat()
        self.assertEqual([h["snapshot"] is not None for h in client.heartbeats], [True, False, True])

    def test_periodic_heartbeat_pings_and_claims(self):
        client = FakeClient()
        agent, sock = self.agent(client, [None])  # one 60 s timeout
        self.run_script(agent)
        self.assertEqual(sock.pings, 1)
        self.assertEqual(len(client.heartbeats), 2)
        self.assertEqual(client.claims, 2)

    def test_backoff_and_single_warning(self):
        sleeps = []
        agent, _ = self.agent(FakeClient(), [], sleeps)

        def failing():
            raise ConnectionError("refused")

        agent.push_factory = failing
        with self.assertLogs("remote_slicing_agent", "WARNING") as logs:
            agent.run_forever(max_rounds=5)
        self.assertEqual(sleeps, [5, 15, 60, 300, 300])
        self.assertEqual(len(logs.records), 1)

    def test_startup_shortcut_quotes(self):
        script = main.startup_shortcut_script(r"C:\O'Brien\a.lnk", "pythonw.exe", "-m x run", r"C:\w")
        self.assertIn("CreateShortcut('C:\\O''Brien\\a.lnk')", script)
        self.assertIn("$s.Arguments = '-m x run'", script)


class ImportTests(unittest.TestCase):
    def test_resident_process_does_not_import_heavy_modules(self):
        code = ("import sys; sys.path.insert(0, r'%s'); import remote_slicing_agent.main; "
                "print(sorted(m for m in ('numpy', 'comtypes', 'remote_slicing_agent.mesh', "
                "'remote_slicing_agent.engine', 'remote_slicing_agent.worker', 'remote_slicing_agent.solidworks') "
                "if m in sys.modules))" % main.AGENT_DIR)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "[]", out.stderr)


if __name__ == "__main__":
    unittest.main()
