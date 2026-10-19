"""Tests for remote_slicing_agent.winproc."""

import subprocess
import sys
import tempfile
import time
import unittest

from remote_slicing_agent import winproc


class TestBasicFunctions(unittest.TestCase):
    """Tests that run on Windows; skipped elsewhere."""

    @unittest.skipUnless(winproc.IS_WINDOWS, "Windows only")
    def test_set_below_normal(self):
        self.assertTrue(winproc.set_below_normal())

    @unittest.skipUnless(winproc.IS_WINDOWS, "Windows only")
    def test_set_eco_qos(self):
        self.assertTrue(winproc.set_eco_qos(True))

    @unittest.skipUnless(winproc.IS_WINDOWS, "Windows only")
    def test_empty_working_set(self):
        self.assertTrue(winproc.empty_working_set())

    @unittest.skipUnless(winproc.IS_WINDOWS, "Windows only")
    def test_process_memory(self):
        mem = winproc.process_memory()
        self.assertIsInstance(mem, int)
        self.assertGreater(mem, 0)


class TestKillOnCloseJob(unittest.TestCase):

    @unittest.skipUnless(winproc.IS_WINDOWS, "Windows only")
    def test_job_kills_child(self):
        popen = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        job = winproc.KillOnCloseJob()
        self.assertTrue(job.ok)
        self.assertTrue(job.assign(popen))
        job.close()
        # Child should exit quickly after the job handle closes
        popen.wait(timeout=5)


class TestSingleInstance(unittest.TestCase):

    @unittest.skipUnless(winproc.IS_WINDOWS, "Windows only")
    def test_single_instance_mutex(self):
        """First call returns a handle; child process gets None."""
        handle = winproc.single_instance("Local\\TestRemoteSlicingAgent")
        self.assertIsNotNone(handle)

        # Launch a child that also tries to grab the mutex
        script = (
            "import sys, os; "
            "sys.path.insert(0, {pkg!r}); "
            "from remote_slicing_agent import winproc; "
            "h = winproc.single_instance('Local\\\\TestRemoteSlicingAgent'); "
            "sys.exit(0 if h is None else 1)".format(pkg=tempfile.gettempdir())
        )
        # The child needs the agent/ directory on sys.path (same as the test runner)
        agent_dir = os.path.dirname(os.path.dirname(winproc.__file__))
        script = (
            "import sys; "
            "sys.path.insert(0, {root!r}); "
            "from remote_slicing_agent import winproc; "
            "h = winproc.single_instance('Local\\\\TestRemoteSlicingAgent'); "
            "sys.exit(0 if h is None else 1)".format(root=agent_dir)
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        # Clean up the mutex
        winproc._kernel32.CloseHandle(handle)


# Make sure os is available for the test above
import os  # noqa: E402


class TestNonWindows(unittest.TestCase):
    """Verify graceful no-op on non-Windows (import always works)."""

    def test_import(self):
        # The module imports without error regardless of platform
        self.assertTrue(hasattr(winproc, "IS_WINDOWS"))

    def test_set_below_normal_non_windows(self):
        if winproc.IS_WINDOWS:
            self.skipTest("Windows only")
        self.assertFalse(winproc.set_below_normal())

    def test_process_memory_non_windows(self):
        if winproc.IS_WINDOWS:
            self.skipTest("Windows only")
        self.assertIsNone(winproc.process_memory())


if __name__ == "__main__":
    unittest.main()
