"""Tests for remote_slicing_agent.engine."""

import collections
import os
import subprocess
import sys
import tempfile
import time
import unittest

from remote_slicing_agent.engine import (
    EngineError,
    LogParser,
    build_command,
    build_resolved,
    fix_header,
    initial_extruder,
    run,
    STAGES,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_inputs(**overrides):
    """Return a minimal inputs dict with all keys defaulted to -1 / False."""
    base = {
        "default_extruder": 0,
        "wall_0_extruder_nr": -1,
        "wall_x_extruder_nr": -1,
        "roofing_extruder_nr": -1,
        "flooring_extruder_nr": -1,
        "top_bottom_extruder_nr": -1,
        "infill_extruder_nr": -1,
        "support_enable": False,
        "support_infill_extruder_nr": -1,
        "support_extruder_nr_layer_0": -1,
        "support_bottom_enable": False,
        "support_bottom_extruder_nr": -1,
        "support_roof_enable": False,
        "support_roof_extruder_nr": -1,
        "adhesion_type": "none",
        "skirt_brim_extruder_nr": -1,
        "raft_base_extruder_nr": 0,
        "support_structure": "mmg",
        "support_brim_enable": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# initial_extruder
# ---------------------------------------------------------------------------

class TestInitialExtruder(unittest.TestCase):
    def test_brim_skirt_brim_minus_one_models_3_5(self):
        inp = _base_inputs(adhesion_type="brim", skirt_brim_extruder_nr=-1)
        self.assertEqual(initial_extruder(inp, [3, 5]), 3)

    def test_skirt_skirt_brim_2(self):
        inp = _base_inputs(adhesion_type="skirt", skirt_brim_extruder_nr=2)
        self.assertEqual(initial_extruder(inp, [4]), 2)

    def test_raft(self):
        inp = _base_inputs(adhesion_type="raft", raft_base_extruder_nr=1)
        self.assertEqual(initial_extruder(inp, [0]), 1)

    def test_none_support_brim_support(self):
        inp = _base_inputs(
            adhesion_type="none",
            support_enable=True,
            support_infill_extruder_nr=2,
            support_brim_enable=True,
        )
        self.assertEqual(initial_extruder(inp, [0]), 2)

    def test_none_no_support_infill_string_1(self):
        inp = _base_inputs(
            adhesion_type="none",
            infill_extruder_nr="1",
        )
        self.assertEqual(initial_extruder(inp, [4]), 1)

    def test_string_minus_one_values(self):
        inp = _base_inputs(
            adhesion_type="brim",
            skirt_brim_extruder_nr="-1",
            infill_extruder_nr="-1",
            wall_0_extruder_nr="-1",
        )
        self.assertEqual(initial_extruder(inp, [3, 5]), 3)

    def test_empty_models_default_extruder(self):
        inp = _base_inputs(default_extruder=2)
        self.assertEqual(initial_extruder(inp, []), 2)


# ---------------------------------------------------------------------------
# build_resolved
# ---------------------------------------------------------------------------

class TestBuildResolved(unittest.TestCase):
    def test_replace_start_end_gcode(self):
        snapshot = {
            "resolved": {
                "global": {
                    "machine_start_gcode": ";START_DEFAULT",
                    "machine_end_gcode": ";END_DEFAULT",
                },
                "extruder.0": {},
            },
            "gcode_by_initial_extruder": {
                "1": {
                    "machine_start_gcode": ";START_E1",
                    "machine_end_gcode": ";END_E1",
                },
            },
        }
        result = build_resolved(snapshot, 1)
        self.assertEqual(result["global"]["machine_start_gcode"], ";START_E1")
        self.assertEqual(result["global"]["machine_end_gcode"], ";END_E1")

    def test_snapshot_untouched(self):
        snapshot = {
            "resolved": {
                "global": {
                    "machine_start_gcode": ";START_DEFAULT",
                    "machine_end_gcode": ";END_DEFAULT",
                },
            },
            "gcode_by_initial_extruder": {
                "1": {
                    "machine_start_gcode": ";START_E1",
                    "machine_end_gcode": ";END_E1",
                },
            },
        }
        _ = build_resolved(snapshot, 1)
        self.assertEqual(snapshot["resolved"]["global"]["machine_start_gcode"], ";START_DEFAULT")

    def test_no_gcode_by_initial(self):
        snapshot = {
            "resolved": {
                "global": {"machine_start_gcode": ";START_DEFAULT"},
            },
        }
        result = build_resolved(snapshot, 0)
        self.assertEqual(result["global"]["machine_start_gcode"], ";START_DEFAULT")


# ---------------------------------------------------------------------------
# build_command
# ---------------------------------------------------------------------------

class TestBuildCommand(unittest.TestCase):
    def test_two_meshes(self):
        cmd = build_command(
            "/path/engine",
            "/path/resolved.json",
            [("/path/a.stl", 0), ("/path/b.stl", 1)],
            "/path/out.gcode",
        )
        expected = [
            "/path/engine", "slice", "-r", "/path/resolved.json",
            "-e0", "-l", "/path/a.stl", "-s", "extruder_nr=0",
            "-e1", "-l", "/path/b.stl", "-s", "extruder_nr=1",
            "-o", "/path/out.gcode",
        ]
        self.assertEqual(cmd, expected)

    def test_empty_meshes(self):
        with self.assertRaises(ValueError):
            build_command("/path/engine", "/path/resolved.json", [], "/path/out.gcode")


# ---------------------------------------------------------------------------
# LogParser
# ---------------------------------------------------------------------------

_SAMPLE_LINES = [
    "[2026-10-16 15:19:14.462] [info] Starting slice...",
    "[2026-10-16 15:19:15.000] [info] Progress: slice accomplished in 0.5s",
    "[2026-10-16 15:19:15.100] [info] Starting layerparts...",
    "[2026-10-16 15:19:16.000] [info] Progress: layerparts accomplished in 0.9s",
    "[2026-10-16 15:19:16.100] [info] Starting inset+skin...",
    "[2026-10-16 15:19:20.000] [info] Progress: inset+skin accomplished in 3.9s",
    "[2026-10-16 15:19:20.100] [info] Starting support...",
    "[2026-10-16 15:19:21.000] [info] Progress: support accomplished in 0.9s",
    "[2026-10-16 15:19:21.100] [info] Starting process...",
    "[2026-10-16 15:19:25.000] [info] Progress: process accomplished in 3.9s",
    "[2026-10-16 15:19:25.100] [info] Starting export...",
    "[2026-10-16 15:19:28.000] [info] Progress: export accomplished in 2.9s",
    "[2026-10-16 15:19:28.100] [info] Gcode header after slicing: ;FLAVOR:RepRap",
    ";TIME:3679",
    ";Filament used: 1.9615m, 0m",
    ";TARGET_MACHINE.NAME:VORON2 Custom",
    "",
    "[2026-10-16 15:19:28.200] [info] Print time (s): 3679",
    "[2026-10-16 15:19:28.300] [info] Filament (mm^3): 4717",
    "[2026-10-16 15:19:28.400] [error] Something went slightly wrong",
    "[2026-10-16 15:19:28.500] [info] Done.",
]


class TestLogParser(unittest.TestCase):
    def test_progress(self):
        parser = LogParser()
        results = []
        for line in _SAMPLE_LINES:
            r = parser.feed(line + "\n")
            if r is not None:
                results.append(r)
        # One update per completed stage: a stage's "Starting" equals the previous stage's end, and
        # progress never goes backwards (the real engine logs "process" after "export").
        values = [p for p, _ in results]
        self.assertEqual(values, sorted(set(values)))
        self.assertEqual(len(results), 6)
        self.assertEqual(results[-1], (1.0, "export"))

    def test_progress_never_goes_backwards(self):
        parser = LogParser()
        seen = [parser.feed("[t] [info] Progress: %s accomplished in 0.1s\n" % stage)
                for stage in ("slice", "export", "process")]
        self.assertEqual(seen, [(1 / 6, "slice"), (1.0, "export"), None])

    def test_header(self):
        parser = LogParser()
        for line in _SAMPLE_LINES:
            parser.feed(line + "\n")
        self.assertEqual(parser.header, [
            ";FLAVOR:RepRap",
            ";TIME:3679",
            ";Filament used: 1.9615m, 0m",
            ";TARGET_MACHINE.NAME:VORON2 Custom",
        ])

    def test_print_time(self):
        parser = LogParser()
        for line in _SAMPLE_LINES:
            parser.feed(line + "\n")
        self.assertEqual(parser.print_time_s, 3679)

    def test_filament(self):
        parser = LogParser()
        for line in _SAMPLE_LINES:
            parser.feed(line + "\n")
        self.assertAlmostEqual(parser.filament_mm3, 4717.0)

    def test_errors(self):
        parser = LogParser()
        for line in _SAMPLE_LINES:
            parser.feed(line + "\n")
        self.assertEqual(parser.errors, ["Something went slightly wrong"])

    def test_tail(self):
        parser = LogParser()
        for line in _SAMPLE_LINES:
            parser.feed(line + "\n")
        self.assertIsInstance(parser.tail, collections.deque)
        self.assertEqual(len(parser.tail), len(_SAMPLE_LINES))


# ---------------------------------------------------------------------------
# fix_header
# ---------------------------------------------------------------------------

class TestFixHeader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_sample_gcode(self):
        path = os.path.join(self.tmp.name, "test.gcode")
        with open(path, "w") as f:
            f.write(";FLAVOR:Marlin\n")
            f.write(";TIME:6666\n")
            f.write(";MINX:2.14748e+06\n")
            f.write("\n")
            f.write(";Generated with Cura_SteamEngine 5.13.0\n")
            f.write("G28\n")
            f.write("G1 X10 Y10\n")
        return path

    def test_fix_header_replaces_placeholder(self):
        path = self._write_sample_gcode()
        real_header = [
            ";FLAVOR:RepRap",
            ";TIME:3679",
            ";Filament used: 1.9615m, 0m",
        ]
        fix_header(path, real_header)
        with open(path, "r") as f:
            lines = f.readlines()
        self.assertTrue(lines[0].startswith(";FLAVOR:RepRap"))
        self.assertIn(";Generated with Cura_SteamEngine 5.13.0\n", lines)
        self.assertIn("G28\n", lines)
        self.assertIn("G1 X10 Y10\n", lines)
        # Placeholder lines should be gone
        content = "".join(lines)
        self.assertNotIn(";TIME:6666", content)
        self.assertNotIn(";MINX:2.14748e+06", content)

    def test_no_op_empty_header(self):
        path = self._write_sample_gcode()
        with open(path, "r") as f:
            original = f.read()
        fix_header(path, [])
        with open(path, "r") as f:
            after = f.read()
        self.assertEqual(original, after)

    def test_no_generated_line(self):
        """If no ';Generated with' line in first 200 lines, prepend header."""
        path = os.path.join(self.tmp.name, "test.gcode")
        with open(path, "w") as f:
            f.write("G28\n")
            f.write("G1 X10 Y10\n")
        fix_header(path, [";REAL_HEADER"])
        with open(path, "r") as f:
            lines = f.readlines()
        self.assertEqual(lines[0], ";REAL_HEADER\n")
        self.assertEqual(lines[1], "G28\n")
        self.assertEqual(lines[2], "G1 X10 Y10\n")


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------

class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_script(self, name, content):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", newline="\n") as f:
            f.write(content)
        return path

    def _write_sample_script(self, name):
        """Write a Python script that prints the sample log lines."""
        lines_code = "\n".join(
            f'print({line!r}, flush=True)' for line in _SAMPLE_LINES
        )
        return self._write_script(name, lines_code)

    def test_run_success(self):
        script = self._write_sample_script("success.py")
        cmd = [sys.executable, script]
        parser = run(cmd)
        self.assertEqual(parser.print_time_s, 3679)
        self.assertEqual(len(parser.header), 4)

    def test_run_progress_callback(self):
        script = self._write_sample_script("progress.py")
        cmd = [sys.executable, script]
        progress_values: list = []
        parser = run(cmd, on_progress=lambda p, s: progress_values.append(p))
        self.assertTrue(progress_values)
        self.assertEqual(progress_values[-1], 1.0)
        # Values should be increasing
        for i in range(1, len(progress_values)):
            self.assertGreaterEqual(progress_values[i], progress_values[i - 1])

    def test_run_error(self):
        script = self._write_script("error.py", "[2026-10-16 00:00:00] [error] boom\n")
        # We need a script that prints the error line and exits with code 3
        script_err = self._write_script("error_exit.py",
            'import sys\n'
            'print("[2026-10-16 00:00:00] [error] boom", flush=True)\n'
            'sys.exit(3)\n'
        )
        cmd = [sys.executable, script_err]
        with self.assertRaises(EngineError) as ctx:
            run(cmd)
        self.assertEqual(ctx.exception.returncode, 3)
        self.assertIn("boom", str(ctx.exception))

    def test_run_cancelled(self):
        script = self._write_script("slow.py",
            'import time, sys\n'
            'print("[2026-10-16 00:00:00] [info] Starting slice...", flush=True)\n'
            'time.sleep(30)\n'
        )
        cmd = [sys.executable, script]
        cancel_count = [0]

        def canceller():
            cancel_count[0] += 1
            # Cancel after the first line has been processed
            return cancel_count[0] >= 1

        start = time.time()
        with self.assertRaises(EngineError) as ctx:
            run(cmd, cancelled=canceller)
        elapsed = time.time() - start
        self.assertLess(elapsed, 10.0)
        self.assertEqual(ctx.exception.returncode, -1)
        self.assertIn("cancelled", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
