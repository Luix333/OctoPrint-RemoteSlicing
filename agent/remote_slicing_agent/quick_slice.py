"""Slice models straight from Windows Explorer: right-click .stl / .sldprt files > "Slice..." (the menu entries are
written by explorer_menu.py).

Explorer runs ``pythonw agent/quick_slice.pyw --gather [--print] [--place POS | --options] <file>`` once per
selected file; explorer_batch.py turns the processes of one multi-file selection into a single job. A small
window then:
- converts SolidWorks parts to STL on this PC (as a Dropbox fetch does);
- lays the parts out: packed together at one of nine spots on the bed (the centre for "and upload"), as they lie
  in their files, on Cura's default extruder; or, with --options, in the plate editor (plate_editor.py);
- adds the models to the Remote Slicing library in OctoPrint and queues one slice job for the whole plate;
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

def choose_extruder(snapshot):
    """Cura's default extruder (the one a model gets when it is loaded), or else the first enabled one."""
    extruders = snapshot["summary"]["extruders"]
    enabled = [int(e.get("position", i)) for i, e in enumerate(extruders) if e.get("enabled", True)]
    if not enabled:
        raise QuickSliceError("Every extruder is disabled in Cura.")
    default = snapshot.get("initial_extruder_inputs", {}).get("default_extruder", enabled[0])
    return int(default) if int(default) in enabled else enabled[0]


def files_title(paths):
    names = [os.path.basename(p) for p in paths]
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) <= 3:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return "%s and %d more" % (", ".join(names[:3]), len(names) - 3)


def action_text(start_print, place=None, edited=False):
    what = "Slice, upload and print" if start_print else "Slice and upload"
    if edited:
        return what + " (your layout)"
    return "%s (%s)" % (what, place or "center")


def plate_name(parts):
    names = []
    for part in parts:
        if part.name not in names:
            names.append(part.name)
    return ", ".join(names)


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
    """The work behind the window; runs on its own thread and talks to the window only through ``report`` and,
    for --options, ``edit(parts, snapshot)``, which blocks until the user has laid the plate out and returns
    ``(parts, start_print)``, or None when they cancelled."""

    def __init__(self, paths, start_print, cfg, client, report, place="center", edit=None):
        self.paths = list(paths)
        self.start_print = start_print
        self.place = place
        self.edit = edit
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
            log.exception("quick slice of %s failed", self.paths)
            self.report(None, "%s: %s" % (type(e).__name__, e), True, False)

    def _checkpoint(self):
        if self.cancel.is_set():
            raise Stopped("Cancelled; nothing was sent to OctoPrint.")
        if self.stop.is_set():
            raise Stopped("Closed.")

    def _run(self):
        for path in self.paths:
            if os.path.splitext(path)[1].lower() not in EXTENSIONS:
                raise QuickSliceError("Only .stl and .sldprt files can be sliced (%s isn't one)." % os.path.basename(path))
        if not self.cfg.get("octoprint_url") or not self.cfg.get("api_key"):
            raise QuickSliceError("The Remote Slicing agent isn't connected to OctoPrint yet: "
                                  "run python -m remote_slicing_agent setup.")
        snapshot = load_snapshot()
        from . import plate_layout
        os.makedirs(config.work_dir(), exist_ok=True)
        folder = tempfile.mkdtemp(prefix="quick-", dir=config.work_dir())
        try:
            parts = self._load_parts(folder, snapshot)
            if self.edit is not None:
                try:  # start from the packed layout in the middle; the editor shows whatever doesn't fit
                    plate_layout.place_group(parts, snapshot["bed"], "center")
                except plate_layout.LayoutError:
                    pass
                self.report(None, "Waiting for your layout…")
                decision = self.edit(parts, snapshot)
                if decision is None:
                    raise Stopped("Cancelled; nothing was sent to OctoPrint.")
                parts, self.start_print = decision
            else:
                try:
                    plate_layout.place_group(parts, snapshot["bed"], self.place)
                except plate_layout.LayoutError as e:
                    raise QuickSliceError(str(e))
            self._checkpoint()
            model_ids = self._upload(parts)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        self._checkpoint()
        items = []
        for part in parts:
            item = part.item(model_ids[part.source])
            item["x"], item["y"] = plate_layout.agent_xy(item["x"], item["y"], snapshot["bed"])
            items.append(item)
        task = self.client.submit_slice(plate_name(parts), items, self.start_print)
        self.task_id = task["id"]
        log.info("queued %s for %d part(s) from %s (print=%s)", self.task_id, len(items), self.paths, self.start_print)
        if not winproc.instance_running():
            self.report(0.0, "Starting the PC agent…")
            start_agent()
        self._follow()

    def _load_parts(self, folder, snapshot):
        from . import mesh, plate_layout
        extruder = choose_extruder(snapshot)
        parts = []
        for k, path in enumerate(self.paths, 1):
            name, ext = os.path.splitext(os.path.basename(path))
            count = " (%d of %d)" % (k, len(self.paths)) if len(self.paths) > 1 else ""
            stl = path
            if ext.lower() == ".sldprt":
                self.report(None, "Converting %s in SolidWorks…%s" % (os.path.basename(path), count))
                from . import solidworks
                stl = os.path.join(folder, "part%d.stl" % k)
                try:
                    solidworks.convert(path, stl, self.cfg.get("solidworks_plugin_dir", ""))
                except solidworks.SolidWorksError as e:
                    raise QuickSliceError("%s: %s" % (os.path.basename(path), e))
            self._checkpoint()
            self.report(None, "Reading %s…%s" % (os.path.basename(path), count))
            tris = mesh.load_stl(stl)
            if not len(tris):
                raise QuickSliceError("%s has no triangles." % os.path.basename(path))
            part = plate_layout.Part(name, tris, source=stl)
            part.extruder = extruder
            parts.append(part)
        return parts

    def _upload(self, parts):
        """Add each file once to the model library (copies of a part share it); {source: model id}."""
        sources = []
        for part in parts:
            if part.source not in sources:
                sources.append(part.source)
        ids = {}
        for k, source in enumerate(sources, 1):
            name = next(p.name for p in parts if p.source == source)
            count = " (%d of %d)" % (k, len(sources)) if len(sources) > 1 else ""
            self.report(None, "Sending %s to OctoPrint…%s" % (name, count))
            ids[source] = self.client.add_library_model(source, name + ".stl")["id"]
            self._checkpoint()
        return ids

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

def use_cura_icon(window):
    """Cura's icon in a Tk window's title bar and taskbar instead of Tk's feather (Tk can't read an .exe icon)."""
    try:
        import ctypes
        from .explorer_menu import cura_icon
        exe = cura_icon().rsplit(",", 1)[0]
        if not exe:
            return
        user32 = ctypes.windll.user32
        window.update_idletasks()
        hwnd = user32.GetParent(window.winfo_id())  # the frame window Tk wraps its toplevel in
        large, small = ctypes.c_void_p(), ctypes.c_void_p()
        if ctypes.windll.shell32.ExtractIconExW(exe, 0, ctypes.byref(large), ctypes.byref(small), 1) > 0:
            for kind, icon in ((1, large), (0, small)):  # WM_SETICON: ICON_BIG = 1, ICON_SMALL = 0
                if icon.value:
                    user32.SendMessageW(hwnd, 0x0080, kind, icon.value)
    except Exception:
        pass


class Window:
    """A small always-visible progress window (Tk ships with Python, so no extra dependency)."""

    def __init__(self, title, subtitle, octoprint_url):
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
        self.closed = False
        self.root = root = tk.Tk()
        root.title("Remote Slicing")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(root, padding=(18, 14, 18, 14))
        frame.grid(sticky="nsew")
        bold = font.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold", size=bold.cget("size") + 2)
        self.heading = ttk.Label(frame, text=title, font=bold, justify="left")
        self.heading.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.heading.bind("<Configure>", lambda event: self.heading.configure(wraplength=max(event.width, 200)))
        self.subtitle = ttk.Label(frame, text=subtitle, foreground="#666")
        self.subtitle.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))
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
        use_cura_icon(root)

    def report(self, progress, text, finished=False, ok=False):
        """Thread-safe: the job thread calls this."""
        self.updates.put(("status", progress, text, finished, ok))

    def request_edit(self, parts, snapshot):
        """Thread-safe and blocking: show the plate editor and wait for the user's layout (see QuickSlice)."""
        reply = {"done": threading.Event(), "result": None}
        self.updates.put(("edit", parts, snapshot, reply))
        while not reply["done"].wait(0.2):
            if self.closed:
                return None
        return reply["result"]

    def _poll(self):
        if self.closed:
            return
        latest = None
        try:
            while True:
                message = self.updates.get_nowait()
                if message[0] == "edit":
                    self._open_editor(*message[1:])
                else:
                    latest = message[1:]
        except queue.Empty:
            pass
        if latest is not None and not self.finished:
            self._show(*latest)
        self.root.after(150, self._poll)

    def _open_editor(self, parts, snapshot, reply):
        from .plate_editor import PlateEditor

        def on_slice(new_parts, start_print):
            reply["result"] = (new_parts, start_print)
            self.subtitle.configure(text=action_text(start_print, edited=True))
            self.root.deiconify()
            self.root.lift()
            reply["done"].set()

        def on_cancel():
            reply["done"].set()
            self.close()

        self.root.withdraw()
        editor = PlateEditor(self.root, parts, snapshot, on_slice, on_cancel)
        use_cura_icon(editor.win)

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
        if self.closed:
            return
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
        if self.closed:
            return
        self.closed = True
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


def action_key(start_print, place, options):
    """Which menu entry ran this; files are only batched with others from the same entry."""
    return "options" if options else "%s-%s" % ("print" if start_print else "upload", place)


def main(argv=None):
    from .plate_layout import POSITIONS
    parser = argparse.ArgumentParser(prog="quick_slice")
    parser.add_argument("--print", dest="start_print", action="store_true", help="start the print when it is uploaded")
    parser.add_argument("--place", choices=POSITIONS, default="center", help="where the parts go on the bed")
    parser.add_argument("--options", action="store_true", help="lay the plate out in the editor first")
    parser.add_argument("--gather", action="store_true",
                        help="Explorer: one process per selected file; slice the whole selection as one job")
    parser.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    _setup_log()
    files = [os.path.abspath(f) for f in args.files]
    if args.gather:
        from . import explorer_batch
        gathered = explorer_batch.gather(files[0], action_key(args.start_print, args.place, args.options))
        if not gathered:
            return 0  # another process took this file into its job
        files = gathered + [f for f in files[1:] if f not in gathered]
        log.info("gathered %d file(s): %s", len(files), files)
    cfg = config.load()
    subtitle = "More options" if args.options else action_text(args.start_print, args.place)
    window = Window(files_title(files), subtitle, cfg.get("octoprint_url"))
    job = QuickSlice(files, args.start_print, cfg,
                     Client(cfg.get("octoprint_url") or "http://localhost", cfg.get("api_key") or ""), window.report,
                     place=args.place, edit=window.request_edit if args.options else None)
    window.on_cancel = job.cancel.set
    window.on_close = job.stop.set
    # Not a daemon: closing the window mid-conversion lets SolidWorks finish and be released properly.
    worker = threading.Thread(target=job.run, name="quick-slice")
    worker.start()
    window.root.mainloop()
    window.closed = True
    job.stop.set()
    worker.join()
    return 0
