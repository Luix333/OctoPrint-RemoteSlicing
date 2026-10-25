"""Slice a model straight from Windows Explorer: right-click an .stl or .sldprt file > "Slice and upload" or
"Slice, upload & print" (the menu entries are written by explorer_menu.py).

Explorer runs ``pythonw agent/quick_slice.pyw [--print] <file>``, which opens a small window and:
- converts a SolidWorks part to STL on this PC (like a Dropbox fetch does);
- adds the model to the Remote Slicing library in OctoPrint;
- queues a slice of it in the middle of the bed, as laid (no tilt or turn), on Cura's default extruder;
- follows that job, the same one the tab's Jobs list shows, until OctoPrint has the G-code and, with --print,
  until the print has started.

The resident agent slices it exactly like a job queued from the tab, so the printer checks before a print
starts are the plugin's own. If the agent isn't running, it is started.
"""

import argparse
import json
import logging
import logging.handlers
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from . import config, winproc
from .client import ApiError, Client

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTENSIONS = (".stl", ".sldprt")
POLL_S = 1.0
PRINT_CONFIRM_S = 30.0  # the plugin waits 20 s for the printer to report PrintStarted
OFFLINE_RETRIES = 30
AUTO_CLOSE_S = 8

log = logging.getLogger("remote_slicing_agent.quick_slice")


class QuickSliceError(Exception):
    """Something to tell the user in the window."""


class Stopped(Exception):
    """The window was closed or the job cancelled before anything was queued."""


# ------------------------------------------------------------------------------------------------- decisions

def bed_center(bed):
    """The middle of the bed in the coordinates the tab and the agent use for a model's centre."""
    if bed.get("center_is_zero"):
        return 0.0, 0.0
    return float(bed["width"]) / 2, float(bed["depth"]) / 2


def choose_extruder(snapshot):
    """Cura's default extruder (the one a model gets when it is loaded), or else the first enabled one."""
    extruders = snapshot["summary"]["extruders"]
    enabled = [int(e.get("position", i)) for i, e in enumerate(extruders) if e.get("enabled", True)]
    if not enabled:
        raise QuickSliceError("Every extruder is disabled in Cura.")
    default = snapshot.get("initial_extruder_inputs", {}).get("default_extruder", enabled[0])
    return int(default) if int(default) in enabled else enabled[0]


def check_fits(size, bed):
    """``size`` is the part's (x, y, z) extent in mm. The part stands in the middle of the bed, so it has to fit
    inside the bed less Cura's border (brim, skirt and travel clearance) on every side."""
    edge = float(bed.get("edge_disallowed_size") or 0)
    room = (float(bed["width"]) - 2 * edge, float(bed["depth"]) - 2 * edge, float(bed["height"]))
    if any(s > r + 1e-6 for s, r in zip(size, room)):
        raise QuickSliceError("The part is %.0f x %.0f x %.0f mm and the printable space is %.0f x %.0f x %.0f mm. "
                              "Turn it in the Remote Slicing tab instead." % (tuple(size) + room))


def slice_item(model_id, snapshot):
    x, y = bed_center(snapshot["bed"])
    return {"model_id": model_id, "x": x, "y": y, "rotation": 0, "tilt_x": 0, "tilt_y": 0,
            "extruder": choose_extruder(snapshot)}


def describe(task, start_print, waited_for_print=0.0):
    """What to show for the job: (progress 0..1 or None, text, finished, ok)."""
    if task is None:
        return None, "The job is no longer in OctoPrint's list (cleared from the tab?).", True, False
    state = task.get("state")
    if state == "queued":
        return 0.0, "Waiting for the PC agent to pick the job up…", False, False
    if state == "running":
        stage = (task.get("stage") or "slicing").strip()
        if stage == "claimed":
            stage = "the PC agent picked it up"
        return float(task.get("progress") or 0), stage[:1].upper() + stage[1:] + "…", False, False
    if state == "failed":
        return None, "Slicing failed: %s" % (task.get("error") or "unknown error"), True, False
    if state == "cancelled":
        return None, "The job was cancelled.", True, False
    result = task.get("result") or {}
    path = result.get("path", "?")
    if not start_print:
        return 1.0, "Uploaded to OctoPrint:\n%s" % path, True, True
    if "print_started" in result:
        if result["print_started"]:
            return 1.0, "Printing:\n%s" % path, True, True
        return 1.0, "Uploaded, but the print didn't start: %s.\n%s" % (result.get("print_note") or "?", path), True, False
    if waited_for_print > PRINT_CONFIRM_S:
        return 1.0, "Uploaded, but OctoPrint hasn't confirmed that the print started:\n%s" % path, True, False
    return 1.0, "Uploaded; starting the print…\n%s" % path, False, False


def load_snapshot():
    try:
        with open(config.snapshot_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise QuickSliceError("There are no Cura settings on this PC yet: open Cura once with the Remote Slicing "
                              "Snapshot plugin installed.")


def start_agent():
    """Start the resident agent in the background; a second copy exits at once, so this is always safe."""
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pythonw if os.path.exists(pythonw) else sys.executable
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: outlives this window
    subprocess.Popen([exe, "-m", "remote_slicing_agent", "run"], cwd=AGENT_DIR, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)


def _api_message(error):
    try:
        return json.loads(error.body).get("error") or error.body
    except (ValueError, AttributeError):
        return error.body or "no answer"


# ------------------------------------------------------------------------------------------------------- job

class QuickSlice:
    """The work behind the window; runs on its own thread and talks to the window only through ``report``."""

    def __init__(self, path, start_print, cfg, client, report):
        self.path = path
        self.start_print = start_print
        self.cfg = cfg
        self.client = client
        self.report = report  # report(progress or None, text, finished=False, ok=False)
        self.task_id = None
        self.cancel = threading.Event()  # "Cancel job": also cancels the job in OctoPrint
        self.stop = threading.Event()    # window closed: stop watching; a queued job keeps going

    def run(self):
        try:
            self._run()
        except Stopped as e:
            self.report(None, str(e), True, False)
        except QuickSliceError as e:
            self.report(None, str(e), True, False)
        except ApiError as e:
            if e.status == 0:
                self.report(None, "Can't reach OctoPrint at %s: %s" % (self.cfg.get("octoprint_url"), e.body),
                            True, False)
            else:
                self.report(None, "OctoPrint refused it: %s" % _api_message(e), True, False)
        except Exception as e:
            log.exception("quick slice of %s failed", self.path)
            self.report(None, "%s: %s" % (type(e).__name__, e), True, False)

    def _checkpoint(self):
        if self.cancel.is_set():
            raise Stopped("Cancelled; nothing was sent to OctoPrint.")
        if self.stop.is_set():
            raise Stopped("Closed.")

    def _run(self):
        name, ext = os.path.splitext(os.path.basename(self.path))
        if ext.lower() not in EXTENSIONS:
            raise QuickSliceError("Only .stl and .sldprt files can be sliced.")
        if not self.cfg.get("octoprint_url") or not self.cfg.get("api_key"):
            raise QuickSliceError("The Remote Slicing agent isn't connected to OctoPrint yet: "
                                  "run python -m remote_slicing_agent setup.")
        snapshot = load_snapshot()
        os.makedirs(config.work_dir(), exist_ok=True)
        folder = tempfile.mkdtemp(prefix="quick-", dir=config.work_dir())
        try:
            stl = self.path
            if ext.lower() == ".sldprt":
                self.report(None, "Converting in SolidWorks…")
                from . import solidworks
                stl = os.path.join(folder, "part.stl")
                try:
                    solidworks.convert(self.path, stl, self.cfg.get("solidworks_plugin_dir", ""))
                except solidworks.SolidWorksError as e:
                    raise QuickSliceError(str(e))
            self._checkpoint()
            self.report(None, "Checking the size…")
            from . import mesh
            points = mesh.load_stl(stl).reshape(-1, 3)
            if not len(points):
                raise QuickSliceError("The file has no triangles.")
            check_fits(tuple(float(v) for v in points.max(axis=0) - points.min(axis=0)), snapshot["bed"])
            item = slice_item(None, snapshot)
            self._checkpoint()
            self.report(None, "Sending the model to OctoPrint…")
            model = self.client.add_library_model(stl, name + ".stl")
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        self._checkpoint()
        item["model_id"] = model["id"]
        task = self.client.submit_slice(name, [item], self.start_print)
        self.task_id = task["id"]
        log.info("queued %s for %s (T%d, print=%s)", self.task_id, self.path, item["extruder"], self.start_print)
        if not winproc.instance_running():
            self.report(0.0, "Starting the PC agent…")
            start_agent()
        self._follow()

    def _follow(self):
        done_at = None
        cancel_sent = False
        failures = 0
        while not self.stop.is_set():
            try:
                if self.cancel.is_set() and not cancel_sent:
                    self.client.cancel_task(self.task_id)
                    cancel_sent = True
                tasks = self.client.state().get("tasks") or []
                failures = 0
            except ApiError as e:
                if e.status != 0 or failures >= OFFLINE_RETRIES:
                    raise
                failures += 1
                self.report(None, "Lost contact with OctoPrint, retrying…")
                self.stop.wait(POLL_S * 2)
                continue
            task = next((t for t in tasks if t.get("id") == self.task_id), None)
            if task is not None and task.get("state") == "done" and done_at is None:
                done_at = time.monotonic()
            progress, text, finished, ok = describe(task, self.start_print,
                                                    time.monotonic() - done_at if done_at else 0.0)
            if cancel_sent and not finished:
                text = "Cancelling…"
            self.report(progress, text, finished, ok)
            if finished:
                return
            self.stop.wait(POLL_S)


# ---------------------------------------------------------------------------------------------------- window

class Window:
    """A small always-visible progress window (Tk ships with Python, so no extra dependency)."""

    def __init__(self, filename, start_print, octoprint_url):
        import tkinter as tk
        from tkinter import font, ttk
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled displays
        except Exception:
            pass
        self._tk = tk
        self.on_cancel = lambda: None
        self.on_close = lambda: None
        self.updates = queue.Queue()
        self.finished = False
        self.root = root = tk.Tk()
        root.title("Remote Slicing")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(root, padding=(18, 14, 18, 14))
        frame.grid(sticky="nsew")
        bold = font.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold", size=bold.cget("size") + 2)
        ttk.Label(frame, text=filename, font=bold).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, text="Slice, upload & print" if start_print else "Slice and upload",
                  foreground="#666").grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))
        # sets the window width; in points, so it grows with the display scaling like the text does
        self.bar = ttk.Progressbar(frame, length="240p", mode="determinate", maximum=1.0)
        self.bar.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.status = ttk.Label(frame, text="Starting…", wraplength=420, justify="left")
        self.status.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 12))
        # wrap at the label's real width: on a scaled display it is wider than 420 px
        self.status.bind("<Configure>", lambda event: self.status.configure(wraplength=max(event.width, 200)))
        url = (octoprint_url or "").rstrip("/") + "/#tab_plugin_remoteslicing"
        open_button = ttk.Button(frame, text="Open OctoPrint", command=lambda: __import__("webbrowser").open(url))
        open_button.grid(row=4, column=0, sticky="w")
        if not octoprint_url:
            open_button.state(["disabled"])
        self.cancel_button = ttk.Button(frame, text="Cancel job", command=self._cancel)
        self.cancel_button.grid(row=4, column=1, sticky="e", padx=(0, 8))
        self.close_button = ttk.Button(frame, text="Close", command=self.close)
        self.close_button.grid(row=4, column=2, sticky="e")
        frame.columnconfigure(1, weight=1)
        root.update_idletasks()
        x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
        y = (root.winfo_screenheight() - root.winfo_reqheight()) // 3
        root.geometry("+%d+%d" % (x, y))
        root.attributes("-topmost", True)  # come to the front once, then behave normally
        root.after(1500, lambda: root.attributes("-topmost", False))
        root.after(100, self._poll)
        self._use_cura_icon()

    def _use_cura_icon(self):
        """Cura's icon in the title bar and taskbar instead of Tk's feather (Tk can't read an .exe icon itself)."""
        try:
            import ctypes
            from .explorer_menu import cura_icon
            exe = cura_icon().rsplit(",", 1)[0]
            if not exe:
                return
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.root.winfo_id())  # the frame window Tk wraps its toplevel in
            large, small = ctypes.c_void_p(), ctypes.c_void_p()
            if ctypes.windll.shell32.ExtractIconExW(exe, 0, ctypes.byref(large), ctypes.byref(small), 1) > 0:
                for kind, icon in ((1, large), (0, small)):  # WM_SETICON: ICON_BIG = 1, ICON_SMALL = 0
                    if icon.value:
                        user32.SendMessageW(hwnd, 0x0080, kind, icon.value)
        except Exception:
            pass

    def report(self, progress, text, finished=False, ok=False):
        """Thread-safe: the job thread calls this."""
        self.updates.put((progress, text, finished, ok))

    def _poll(self):
        latest = None
        try:
            while True:
                latest = self.updates.get_nowait()
        except queue.Empty:
            pass
        if latest is not None and not self.finished:
            self._show(*latest)
        self.root.after(150, self._poll)

    def _show(self, progress, text, finished, ok):
        self.status.configure(text=text)
        if progress is None and not finished:
            if str(self.bar.cget("mode")) != "indeterminate":
                self.bar.configure(mode="indeterminate")
                self.bar.start(15)
        else:
            self.bar.stop()
            self.bar.configure(mode="determinate", value=progress if progress is not None else 0)
        if finished:
            self.finished = True
            self.cancel_button.state(["disabled"])
            if ok:
                self._countdown(AUTO_CLOSE_S, text)

    def _countdown(self, seconds, text):
        if seconds <= 0:
            self.close()
            return
        self.status.configure(text="%s\nThis window closes in %d s." % (text, seconds))
        self.root.after(1000, self._countdown, seconds - 1, text)

    def _cancel(self):
        self.cancel_button.state(["disabled"])
        self.status.configure(text="Cancelling…")
        self.on_cancel()

    def close(self):
        self.on_close()
        self.root.destroy()


# ------------------------------------------------------------------------------------------------------ main

def _setup_log():
    try:
        os.makedirs(config.home(), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(os.path.join(config.home(), "quick_slice.log"),
                                                       maxBytes=200_000, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="quick_slice")
    parser.add_argument("--print", dest="start_print", action="store_true", help="start the print when it is uploaded")
    parser.add_argument("file")
    args = parser.parse_args(argv)
    _setup_log()
    cfg = config.load()
    window = Window(os.path.basename(args.file), args.start_print, cfg.get("octoprint_url"))
    job = QuickSlice(os.path.abspath(args.file), args.start_print, cfg,
                     Client(cfg.get("octoprint_url") or "http://localhost", cfg.get("api_key") or ""), window.report)
    window.on_cancel = job.cancel.set
    window.on_close = job.stop.set
    # Not a daemon: closing the window mid-conversion lets SolidWorks finish and be released properly.
    worker = threading.Thread(target=job.run, name="quick-slice")
    worker.start()
    window.root.mainloop()
    job.stop.set()
    worker.join()
    return 0
