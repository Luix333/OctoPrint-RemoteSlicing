"""Tests for remote_slicing_agent.explorer_batch (one job for a multi-file Explorer selection)."""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from remote_slicing_agent import explorer_batch as eb


@unittest.skipUnless(sys.platform == "win32", "named mutex")
class GatherTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"REMOTE_SLICING_HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.home, True)
        for name, value in (("FIRST_WAIT_S", 0.4), ("QUIET_S", 0.25), ("MAX_WAIT_S", 3.0)):
            p = mock.patch.object(eb, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_all(self, starts):
        """starts: [(file, action, delay)]; each runs gather() on its own thread, like Explorer's processes."""
        results = {}

        def one(file, action, delay):
            time.sleep(delay)
            results[file] = eb.gather(file, action)
        threads = [threading.Thread(target=one, args=s) for s in starts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        return results

    def test_one_collector_gets_every_file_in_order(self):
        files = [r"C:\parts\%d.stl" % i for i in range(6)]
        results = self.run_all([(f, "print-top-left", 0.03 * i) for i, f in enumerate(files)])
        collected = [r for r in results.values() if r]
        self.assertEqual(collected, [files])
        self.assertEqual(sum(r is None for r in results.values()), 5)
        self.assertEqual(os.listdir(eb.spool_dir()), [])

    def test_different_menu_entries_are_separate_jobs(self):
        results = self.run_all([("a.stl", "upload-center", 0), ("b.stl", "options", 0.02),
                                ("c.stl", "upload-center", 0.04)])
        collected = sorted(r for r in results.values() if r)
        self.assertEqual(collected, [["a.stl", "c.stl"], ["b.stl"]])

    def test_a_single_file_waits_only_briefly(self):
        start = time.monotonic()
        self.assertEqual(eb.gather("only.stl", "upload-center"), ["only.stl"])
        self.assertLess(time.monotonic() - start, 1.5)

    def test_stale_requests_are_dropped(self):
        os.makedirs(eb.spool_dir())
        old = os.path.join(eb.spool_dir(), "00000000000000000001.dead.upload-center.req")
        with open(old, "w") as f:
            f.write('{"file": "ghost.stl"}')
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        self.assertEqual(eb.gather("new.stl", "upload-center"), ["new.stl"])
        self.assertFalse(os.path.exists(old))


if __name__ == "__main__":
    unittest.main()
