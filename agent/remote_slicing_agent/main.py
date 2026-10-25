"""The resident Remote Slicing agent (``python -m remote_slicing_agent [run|setup|status|...]``).

Idle cost is the design constraint. The process blocks in one websocket ``recv`` on OctoPrint's push socket,
subscribed only to this plugin's messages, and sends an HTTP heartbeat once a minute. Each task runs in a
short-lived child (``python -m remote_slicing_agent.worker``), so numpy, CuraEngine and SolidWorks never
load in here; a unit test makes sure nothing imports them.
"""

import argparse
import gc
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import time

from . import __version__, client, config, dropbox_browse, push, winproc

BACKOFF = (5, 15, 60, 300)  # reconnect delays, then 300 s forever
JOB_POLL = 2.0  # socket wait while a job runs, to notice it finishing
AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs",
                           "Startup", "Remote Slicing Agent.lnk")

_JOB = None  # KillOnCloseJob: workers (and their CuraEngine) die with the agent


class Agent:
    def __init__(self, cfg, client_obj=None, push_factory=None, spawn=None, clock=time.monotonic,
                 sleep=time.sleep, log=None):
        self.cfg = cfg
        self.client = client_obj or client.Client(cfg["octoprint_url"], cfg["api_key"])
        self.push_factory = push_factory or (lambda: push.PushSocket(self.client))
        self.spawn = spawn or self._spawn_worker
        self.clock = clock
        self.sleep = sleep
        self.log = log or logging.getLogger("remote_slicing_agent")
        self.sock = None
        self.job = None
        self._failures = 0
        self._warned = False
        self._connected_once = False
        self._last_stat = object()  # never equal to a real stat: the first heartbeat carries the summary

    # ---------------------------------------------------------------- connection

    def run_forever(self, max_rounds=None):
        rounds = 0
        while max_rounds is None or rounds < max_rounds:
            rounds += 1
            try:
                self.run_once_connected()
            except (ConnectionError, client.ApiError, OSError) as e:
                self._close()
                if not self._warned:  # one line per outage, not one per retry
                    self.log.warning("lost connection to OctoPrint: %s", e)
                    self._warned = True
                self.sleep(BACKOFF[min(self._failures, len(BACKOFF) - 1)])
                self._failures += 1

    def _close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def run_once_connected(self):
        self.sock = self.push_factory()
        self.sock.connect()
        self._failures = 0
        if self._warned or not self._connected_once:
            self.log.info("connected to OctoPrint")
        self._warned = False
        self._connected_once = True
        self.heartbeat(force_snapshot=True)
        self.try_claim()
        interval = float(self.cfg.get("heartbeat_seconds") or 60)
        next_hb = self.clock() + interval
        while True:
            timeout = JOB_POLL if self.job else max(0.5, next_hb - self.clock())
            msg = self.sock.next_message(timeout)
            if msg:
                kind = msg.get("type")
                if kind == "agent.wake":
                    self.try_claim()
                elif kind == "agent.list":
                    self.handle_list(msg)
            self.check_job()
            if self.clock() >= next_hb:
                self.sock.ping()
                self.heartbeat()
                self.try_claim()  # fallback for a wake message lost while reconnecting
                next_hb = self.clock() + interval

    # ------------------------------------------------------------------- server

    def heartbeat(self, force_snapshot=False):
        stat = config.snapshot_stat()
        changed = force_snapshot or stat != self._last_stat
        summary = config.read_snapshot_summary() if changed else None
        self.client.heartbeat({"version": __version__, "snapshot": summary})
        self._last_stat = stat

    def try_claim(self):
        if self.job:
            return
        task = self.client.claim()
        if not task:
            return
        os.makedirs(config.work_dir(), exist_ok=True)
        path = os.path.join(config.work_dir(), task["id"] + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(task, f)
        self.job = {"task": task, "path": path, "proc": self.spawn(task, path)}
        self.log.info("task %s (%s) started", task["id"], task.get("kind"))

    def check_job(self):
        if not self.job or self.job["proc"].poll() is None:
            return
        task, code = self.job["task"], self.job["proc"].poll()
        if code == 0:
            self.log.info("task %s finished", task["id"])
        else:
            tail = _tail(os.path.join(config.work_dir(), task["id"] + ".log"), 5)
            try:
                self.client.failed(task["id"], "the agent's worker crashed (exit code %d): %s" % (code, tail))
            except client.ApiError:
                pass
            self.log.warning("task %s: worker exited with code %d", task["id"], code)
        try:
            os.remove(self.job["path"])
        except OSError:
            pass
        self.job = None
        winproc.empty_working_set()
        gc.collect()
        self.try_claim()  # drain the queue

    def handle_list(self, msg):
        path = msg.get("path", "")
        try:
            data = dropbox_browse.list_folder(self.cfg["dropbox_root"], path)
            data["error"] = None
        except (ValueError, OSError) as e:
            data = {"path": path, "dirs": [], "files": [], "truncated": False, "error": str(e)}
        try:
            self.client.listing(msg.get("request_id"), data)
        except client.ApiError:
            pass

    # ------------------------------------------------------------------ workers

    def _spawn_worker(self, task, task_path):
        global _JOB
        log_file = open(os.path.join(config.work_dir(), task["id"] + ".log"), "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "remote_slicing_agent.worker", task_path],
                cwd=AGENT_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log_file,
                creationflags=getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        finally:
            log_file.close()
        if _JOB is None:
            _JOB = winproc.KillOnCloseJob()
        _JOB.assign(proc)
        return proc


def _tail(path, n):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return " | ".join(line.rstrip() for line in f.readlines()[-n:] if line.strip())
    except OSError:
        return ""


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def startup_shortcut_script(lnk, target, args, workdir):
    """PowerShell that writes a Startup-folder shortcut (WindowStyle 7 = minimised; pythonw has no window)."""
    return ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut({}); $s.TargetPath = {}; "
            "$s.Arguments = {}; $s.WorkingDirectory = {}; $s.WindowStyle = 7; $s.Save()").format(
        _ps_quote(lnk), _ps_quote(target), _ps_quote(args), _ps_quote(workdir))


# ----------------------------------------------------------------------- commands

def cmd_run(args):
    if winproc.single_instance() is None:
        print("Remote Slicing agent is already running")
        return 1
    os.makedirs(config.home(), exist_ok=True)
    log = logging.getLogger("remote_slicing_agent")
    handler = logging.handlers.RotatingFileHandler(config.log_path(), maxBytes=1_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    winproc.set_below_normal()
    winproc.set_eco_qos(True)
    cfg = config.load()
    if not cfg["octoprint_url"] or not cfg["api_key"]:
        log.error("not set up: run python -m remote_slicing_agent setup")
        return 2
    log.info("agent %s starting (dropbox: %s)", __version__, cfg["dropbox_root"])
    Agent(cfg).run_forever()
    return 0


def cmd_setup(args):
    cfg = config.load()
    current = cfg.get("octoprint_url", "")
    url = (args.url or input("OctoPrint URL [%s]: " % current).strip() or current).rstrip("/")
    if not url:
        print("an OctoPrint URL is needed, e.g. http://10.147.17.100")
        return 1
    try:
        key = client.request_app_key(url, on_pending=lambda dialog: print(
            "Approve 'Remote Slicing Agent' in OctoPrint now: a dialog appears in the OctoPrint web page "
            "(or open %s)." % dialog))
    except client.ApiError as e:
        print("setup failed:", e.status, e.body)
        return 1
    cfg["octoprint_url"], cfg["api_key"] = url, key
    config.save(cfg)
    print("saved to", config.config_path())
    return 0


def cmd_status(args):
    cfg = config.load()
    print("config:   ", config.config_path())
    print("octoprint:", cfg.get("octoprint_url") or "(not set)")
    print("api key:  ", "set" if cfg.get("api_key") else "not set")
    print("dropbox:  ", cfg.get("dropbox_root"))
    snap = config.read_snapshot_summary()
    if snap:
        summary = snap.get("summary", {})
        print("snapshot: ", "%s / %s (from %s)" % (summary.get("machine"), summary.get("profile"), snap.get("created")))
    else:
        print("snapshot:  missing - open Cura once with the Remote Slicing Snapshot plugin")
    if os.path.exists(config.log_path()):
        print("--- last log lines ---")
        print(_tail(config.log_path(), 10).replace(" | ", "\n"))
    return 0


def cmd_install_startup(args):
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    target = pythonw if os.path.exists(pythonw) else sys.executable
    script = startup_shortcut_script(STARTUP_LNK, target, "-m remote_slicing_agent run", AGENT_DIR)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True)
    print("created", STARTUP_LNK)
    return 0


def cmd_remove_startup(args):
    if os.path.exists(STARTUP_LNK):
        os.remove(STARTUP_LNK)
        print("removed", STARTUP_LNK)
    else:
        print("no startup shortcut")
    return 0


def cmd_install_menu(args):
    from . import explorer_menu
    for path, values in explorer_menu.install():
        if "" in values:
            print(path, "->", values[""])
    print('Right-click an .stl or .sldprt file: "Slice and upload" and "Slice, upload & print".')
    return 0


def cmd_remove_menu(args):
    from . import explorer_menu
    removed = explorer_menu.uninstall()
    print("removed %d registry keys" % len(removed) if removed else "the menu entries were not installed")
    return 0


def cmd_quick_slice(args):
    from . import quick_slice
    return quick_slice.main((["--print"] if args.start_print else []) + [args.file])


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m remote_slicing_agent")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the agent (default)")
    setup = sub.add_parser("setup", help="connect to OctoPrint (asks you to approve the agent there)")
    setup.add_argument("--url")
    sub.add_parser("status", help="show the configuration and the last log lines")
    sub.add_parser("install-startup", help="start the agent when you log in to Windows")
    sub.add_parser("remove-startup", help="stop starting the agent at login")
    sub.add_parser("install-menu", help='add "Slice and upload" / "Slice, upload & print" to the right-click '
                                        'menu of .stl and .sldprt files in Explorer')
    sub.add_parser("remove-menu", help="remove those right-click entries")
    quick = sub.add_parser("quick-slice", help="slice one file in the middle of the bed (what the menu runs)")
    quick.add_argument("--print", dest="start_print", action="store_true")
    quick.add_argument("file")
    args = parser.parse_args(argv)
    commands = {"setup": cmd_setup, "status": cmd_status, "install-startup": cmd_install_startup,
                "remove-startup": cmd_remove_startup, "install-menu": cmd_install_menu,
                "remove-menu": cmd_remove_menu, "quick-slice": cmd_quick_slice}
    return commands.get(args.command, cmd_run)(args)
