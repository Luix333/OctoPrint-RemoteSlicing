"""Headless CuraEngine runner.

Builds a CuraEngine command line from a settings snapshot and placed models,
runs CuraEngine.exe without a console window at below-normal priority,
reports coarse progress, and repairs the G-code header.
"""

import collections
import copy
import os
import queue
import re
import shutil
import subprocess
import threading
import sys
import tempfile

STAGES = ("slice", "layerparts", "inset+skin", "support", "process", "export")


def initial_extruder(inputs: dict, model_extruders: list) -> int:
    """Return the initial extruder number, repeating Cura's logic.

    Parameters
    ----------
    inputs : dict
        Cura setting values (string keys, values may be int/str/bool).
    model_extruders : list[int]
        Extruder numbers assigned to the placed models.

    Returns
    -------
    int
        The extruder number Cura will start printing with.
    """

    def _int(v):
        """Convert a setting value to int (handles string booleans too)."""
        if isinstance(v, bool):
            return int(v)
        return int(float(str(v)))

    # 1. Build the used set
    used: set = set(model_extruders)

    for key in (
        "wall_0_extruder_nr",
        "wall_x_extruder_nr",
        "roofing_extruder_nr",
        "flooring_extruder_nr",
        "top_bottom_extruder_nr",
        "infill_extruder_nr",
    ):
        val = _int(inputs.get(key, -1))
        if val != -1:
            used.add(val)

    # Support-related extruders
    support_enable_raw = inputs.get("support_enable", False)
    if isinstance(support_enable_raw, str):
        support_enable = support_enable_raw.lower() == "true"
    else:
        support_enable = bool(support_enable_raw)

    if support_enable:
        used.add(_int(inputs.get("support_infill_extruder_nr", -1)))
        used.add(_int(inputs.get("support_extruder_nr_layer_0", -1)))
        if inputs.get("support_bottom_enable", False):
            bottom_raw = inputs["support_bottom_enable"]
            if isinstance(bottom_raw, str):
                bottom_enable = bottom_raw.lower() == "true"
            else:
                bottom_enable = bool(bottom_raw)
            if bottom_enable:
                used.add(_int(inputs.get("support_bottom_extruder_nr", -1)))
        if inputs.get("support_roof_enable", False):
            roof_raw = inputs["support_roof_enable"]
            if isinstance(roof_raw, str):
                roof_enable = roof_raw.lower() == "true"
            else:
                roof_enable = bool(roof_raw)
            if roof_enable:
                used.add(_int(inputs.get("support_roof_extruder_nr", -1)))

    if not used:
        used = {_int(inputs["default_extruder"])}

    def _val(key):
        return _int(inputs.get(key, -1))

    # 2. Skirt or brim
    adhesion_type = inputs.get("adhesion_type", "none")
    if adhesion_type in ("skirt", "brim"):
        sb = _val("skirt_brim_extruder_nr")
        if sb != -1:
            return sb
        return min(used)

    # 3. Raft
    if adhesion_type == "raft":
        return _val("raft_base_extruder_nr")

    # 4. Support brim
    support_structure = inputs.get("support_structure", "mmg")
    support_brim_enable_raw = inputs.get("support_brim_enable", False)
    if isinstance(support_brim_enable_raw, str):
        support_brim_enable = support_brim_enable_raw.lower() == "true"
    else:
        support_brim_enable = bool(support_brim_enable_raw)

    if (support_enable or support_structure == "tree") and support_brim_enable:
        return _val("support_infill_extruder_nr")

    # 5. Fallback
    return min(used)


def build_resolved(snapshot: dict, initial: int) -> dict:
    """Build the resolved settings dict for CuraEngine.

    Returns a deep copy of ``snapshot["resolved"]`` where the global
    *machine_start_gcode* and *machine_end_gcode* are replaced by the
    per-extruder variants when the snapshot contains them.  The original
    snapshot is never mutated.
    """
    resolved = copy.deepcopy(snapshot["resolved"])
    gcode_by_initial = snapshot.get("gcode_by_initial_extruder", {})
    key = str(initial)
    if key in gcode_by_initial:
        per = gcode_by_initial[key]
        if "machine_start_gcode" in per:
            resolved["global"]["machine_start_gcode"] = per["machine_start_gcode"]
        if "machine_end_gcode" in per:
            resolved["global"]["machine_end_gcode"] = per["machine_end_gcode"]
    return resolved


def build_command(engine_path: str, resolved_path: str, meshes, output_path: str) -> list:
    """Build the CuraEngine ``slice`` command line.

    Parameters
    ----------
    engine_path : str
        Path to CuraEngine executable.
    resolved_path : str
        Path to the resolved settings JSON file.
    meshes : list[tuple[str, int]]
        Pairs of ``(stl_path, extruder_nr)`` in placement order.
    output_path : str
        Path for the output G-code file.

    Returns
    -------
    list[str]
        Command-line arguments suitable for ``subprocess.Popen``.

    Raises
    ------
    ValueError
        If *meshes* is empty.
    """
    if not meshes:
        raise ValueError("meshes must not be empty")

    cmd: list = [engine_path, "slice", "-r", resolved_path]
    for i, (stl_path, extruder_nr) in enumerate(meshes, start=1):
        cmd.extend(["-e" + str(extruder_nr), "-l", stl_path, "-s", "extruder_nr=" + str(extruder_nr)])
    cmd.extend(["-o", output_path])
    return cmd


class LogParser:
    """Parse CuraEngine log lines for progress, header, and metadata.

    Attributes
    ----------
    header : list[str]
        G-code header lines extracted from the log.
    print_time_s : int or None
        Total print time in seconds.
    filament_mm3 : float or None
        Filament volume in mm^3.
    errors : list[str]
        Message text of ``[error]`` and ``[critical]`` lines.
    tail : collections.deque
        Last 40 raw log lines.
    """

    def __init__(self) -> None:
        self.header: list = []
        self.print_time_s: int | None = None
        self.filament_mm3: float | None = None
        self.errors: list = []
        self.tail: collections.deque = collections.deque(maxlen=40)
        self._in_header = False
        self._progress = 0.0  # CuraEngine logs "process" after "export"; never report going backwards

    def _advance(self, progress, stage):
        if progress <= self._progress:
            return None
        self._progress = progress
        return (progress, stage)

    def feed(self, line: str):
        """Feed a single log line.

        Returns ``(progress, stage)`` when progress changes, else ``None``.
        """
        stripped = line.rstrip("\n").rstrip("\r")
        self.tail.append(stripped)

        # Extract stage progress from "Starting <stage>..." and "Progress: <stage> accomplished"
        m_start = re.match(r".*\[info\] Starting (.+?)\.\.\.", stripped)
        if m_start:
            stage_name = m_start.group(1)
            if stage_name in STAGES:
                return self._advance(STAGES.index(stage_name) / len(STAGES), stage_name)

        m_prog = re.match(r".*\[info\] Progress: (.+?) accomplished", stripped)
        if m_prog:
            stage_name = m_prog.group(1)
            if stage_name in STAGES:
                return self._advance((STAGES.index(stage_name) + 1) / len(STAGES), stage_name)

        # Collect header lines
        m_header_start = re.match(r".*\[info\] Gcode header after slicing: (.*)", stripped)
        if m_header_start:
            self.header = [m_header_start.group(1)]
            self._in_header = True
            return None

        # Header continuation: lines that do NOT start with "[", up to the first blank line
        if self._in_header:
            if stripped.startswith("[") or not stripped.strip():
                self._in_header = False
            else:
                self.header.append(stripped.strip())
                return None

        # Print time
        m_time = re.match(r".*\[info\] Print time \(s\): (\d+)", stripped)
        if m_time:
            self.print_time_s = int(m_time.group(1))

        # Filament volume
        m_fil = re.match(r".*\[info\] Filament \(mm\^3\): ([\d.]+)", stripped)
        if m_fil:
            self.filament_mm3 = float(m_fil.group(1))

        # Errors
        m_err = re.match(r".*\[(?:error|critical)\] (.*)", stripped)
        if m_err:
            self.errors.append(m_err.group(1))

        return None


def fix_header(gcode_path: str, header: list) -> None:
    """Repair the G-code header in-place.

    Replaces CuraEngine's placeholder header with the real header
    extracted from the engine log.  If *header* is empty this is a no-op.

    The file is streamed line-by-line into a temporary file and then
    atomically replaced (suitable for large files).
    """
    if not header:
        return

    folder = os.path.dirname(gcode_path) or "."
    head = "".join(h + "\n" for h in header).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(suffix=".gcode", dir=folder)
    try:
        with open(gcode_path, "rb") as src, os.fdopen(fd, "wb") as dst:
            dst.write(head)
            found = False
            for _ in range(200):  # the placeholder header is ~12 lines
                line = src.readline()
                if not line:
                    break
                if line.startswith(b";Generated with"):
                    dst.write(line)
                    found = True
                    break
            if not found:
                src.seek(0)  # no marker: keep the whole file after the header
            shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(tmp_path, gcode_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


class EngineError(Exception):
    """Raised when CuraEngine fails or is cancelled.

    Attributes
    ----------
    returncode : int
        Process exit code (or -1 for cancellation).
    tail : list[str]
        Last 40 log lines.
    """

    def __init__(self, message: str, returncode: int = -1, tail: list | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.tail = tail or []


def run(cmd: list, on_progress=None, cwd=None, cancelled=None) -> LogParser:
    """Run CuraEngine and return a populated ``LogParser``.

    Parameters
    ----------
    cmd : list[str]
        Command line (see :func:`build_command`).
    on_progress : callable, optional
        Called as ``on_progress(progress: float, stage: str)`` whenever
        stage progress changes.
    cwd : str, optional
        Working directory for the process.
    cancelled : callable, optional
        Checked after every output line; when it returns ``True`` the
        process is killed and an ``EngineError("cancelled")`` is raised.

    Returns
    -------
    LogParser
        Populated with progress, header, metadata, and errors.

    Raises
    ------
    EngineError
        If the process exits with a non-zero code or is cancelled.
    """
    creationflags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)

    parser = LogParser()

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=cwd,
        creationflags=creationflags,
    )

    lines = queue.Queue()

    def _pump():
        try:
            for raw in proc.stdout:
                lines.put(raw)
        finally:
            lines.put(None)

    reader = threading.Thread(target=_pump, name="curaengine-log", daemon=True)
    reader.start()
    try:
        while True:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                line = ""
            if cancelled and cancelled():
                proc.kill()
                proc.wait()
                raise EngineError("cancelled", returncode=-1, tail=list(parser.tail))
            if line is None:
                break
            if line:
                result = parser.feed(line)
                if result is not None and on_progress is not None:
                    on_progress(result[0], result[1])
    finally:
        if proc.poll() is None and cancelled and cancelled():
            proc.kill()
        reader.join(timeout=5)
        proc.stdout.close()

    proc.wait()

    if proc.returncode != 0:
        message = "CuraEngine exited with code {}".format(proc.returncode)
        if parser.errors:
            message = parser.errors[-1]
        raise EngineError(message, returncode=proc.returncode, tail=list(parser.tail))

    return parser
