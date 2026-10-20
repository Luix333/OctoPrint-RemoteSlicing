# coding=utf-8
"""Plugin logic for Remote Slicing. Nothing in here imports OctoPrint, so it can be
unit tested on any machine (python -m unittest discover -s tests).

The OctoPrint glue in __init__.py implements the small "host" interface:

    host.now()              -> float, epoch seconds
    host.changed()          -> None, persist + push the new state to every browser
    host.wake_agent(msg)    -> None, push a message to the PC agent via the socket
    host.new_id()           -> str, unique id (glue uses uuid4().hex[:12])

The PC agent claims tasks over HTTP and is woken through the push socket; the
model files themselves live in the plugin data folder, managed by the glue.
"""

import copy
import threading
import uuid

DEFAULT_SETTINGS = {
    "agent_timeout": 150,
    "lease_seconds": 120,
    "max_attempts": 3,
    "keep_finished": 50,
    "listing_timeout": 10,
}


def _int(value, default, minimum=None):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    return value


class Core(object):
    """Central state machine for the Remote Slicing plugin.

    All public methods acquire a re-entrant lock so they are safe to call from
    multiple threads (OctoPrint event loop, API handlers, timers).
    """

    # --------------------------------------------------------------------- init

    def __init__(self, host, get_settings=None, state=None):
        """Initialise the core.

        Parameters
        ----------
        host : object
            Must provide now(), changed(), wake_agent(msg), new_id().
        get_settings : callable, optional
            Returns a dict of user settings that override DEFAULT_SETTINGS.
        state : dict, optional
            Previous ``persistent_state()`` dict with keys ``models``, ``tasks``,
            ``agent``.  On construction any task whose state is ``"running"``
            is reset to ``"queued"`` with ``lease_until=None`` (the host
            restarted so the agent lease is no longer valid).
        """
        self._host = host
        self._get_settings = get_settings or (lambda: {})
        self._lock = threading.RLock()

        self._models = []
        self._tasks = []
        self._agent_last_seen = None
        self._agent_version = None
        self._agent_snapshot = None
        self._listings = {}  # request_id -> {path, created}

        if state:
            self._models = list(state.get("models", []) or [])
            self._tasks = list(state.get("tasks", []) or [])
            agent = state.get("agent", {}) or {}
            self._agent_last_seen = agent.get("last_seen")
            self._agent_version = agent.get("version")
            self._agent_snapshot = agent.get("snapshot")

            # Rehydrate: running tasks -> queued (OctoPrint restarted)
            for task in self._tasks:
                if task.get("state") == "running":
                    task["state"] = "queued"
                    task["lease_until"] = None

    def settings(self):
        """Return merged settings (user overrides on top of defaults)."""
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in (self._get_settings() or {}).items() if v is not None})
        return merged

    # --------------------------------------------------------------- state

    def snapshot(self):
        """Everything the UI needs; pushed after every change."""
        with self._lock:
            self.expire()
            return {
                "server_time": self._host.now(),
                "models": self.models(),
                "tasks": self.tasks(),
                "agent": self.agent_status(),
            }

    def persistent_state(self):
        """Return a serialisable dict safe to persist and later pass to the constructor."""
        with self._lock:
            return {
                "models": copy.deepcopy(self._models),
                "tasks": copy.deepcopy(self._tasks),
                "agent": {
                    "last_seen": self._agent_last_seen,
                    "version": self._agent_version,
                    "snapshot": copy.deepcopy(self._agent_snapshot),
                },
            }

    # ----------------------------------------------------------- models

    def add_model(self, name, size, source, origin=""):
        """Create a new model entry and return it.

        Parameters
        ----------
        name : str
            Human-readable model name.
        size : float
            File size in bytes.
        source : str
            One of ``"upload"``, ``"dropbox"``, ``"converted"``.
        origin : str
            Free-form origin string (e.g. Dropbox path or original SLDPRT name).

        Returns
        -------
        dict
            The newly created model dict.
        """
        with self._lock:
            model = {
                "id": self._host.new_id(),
                "name": name,
                "size": size,
                "source": source,
                "origin": origin,
                "created": self._host.now(),
            }
            self._models.append(model)
            self._host.changed()
            return copy.deepcopy(model)

    def remove_model(self, model_id):
        """Remove a model by id.

        Returns ``True`` on success, ``False`` if the model is unknown.
        Raises ``ValueError("model in use")`` if a queued or running slice
        task references the model.
        """
        with self._lock:
            # Check if model exists
            model_idx = None
            for idx, m in enumerate(self._models):
                if m["id"] == model_id:
                    model_idx = idx
                    break
            if model_idx is None:
                return False

            # Check if any queued/running slice task references this model
            for task in self._tasks:
                if task["state"] in ("queued", "running") and task["kind"] == "slice":
                    for item in task.get("payload", {}).get("items", []):
                        if item.get("model_id") == model_id:
                            raise ValueError("model in use")

            self._models.pop(model_idx)
            self._host.changed()
            return True

    def get_model(self, model_id):
        """Return a copy of the model dict, or ``None`` if unknown."""
        with self._lock:
            for m in self._models:
                if m["id"] == model_id:
                    return copy.deepcopy(m)
            return None

    def models(self):
        """Return all models newest-first (by ``created`` timestamp)."""
        with self._lock:
            return sorted(copy.deepcopy(self._models), key=lambda m: m["created"], reverse=True)

    # --------------------------------------------------------- task creation

    def _make_task(self, kind, payload):
        """Create a new task dict (caller must hold the lock)."""
        return {
            "id": self._host.new_id(),
            "kind": kind,
            "state": "queued",
            "created": self._host.now(),
            "updated": self._host.now(),
            "progress": 0,
            "stage": "queued",
            "error": None,
            "attempts": 0,
            "lease_until": None,
            "payload": payload,
            "result": None,
            "cancel_requested": False,
        }

    def submit_slice(self, name, items, start_print):
        """Submit a new slicing task.

        Parameters
        ----------
        name : str
            Plate name (stripped; empty becomes ``"plate"``).
        items : list
            List of dicts with keys ``model_id``, ``x``, ``y``, ``rotation``, ``extruder`` and optional
            ``tilt_x``/``tilt_y`` (multiples of 90 degrees, about X then Y, applied before ``rotation``).
        start_print : bool
            Whether to auto-start the print after slicing completes.

        Returns
        -------
        dict
            The created task.

        Raises
        ------
        ValueError
            On validation failure (empty items, unknown model, bad coordinates,
            or extruder out of range).
        """
        with self._lock:
            # Validate items non-empty
            if not items:
                raise ValueError("nothing to slice")

            # Strip name
            name = name.strip() if isinstance(name, str) else ""
            if not name:
                name = "plate"

            # Validate each item
            cleaned_items = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("bad item")
                mid = item.get("model_id")
                # Check model exists
                model_found = False
                for m in self._models:
                    if m["id"] == mid:
                        model_found = True
                        break
                if not model_found:
                    raise ValueError("unknown model {}".format(mid))

                # x, y, rotation must be numbers
                try:
                    x = float(item["x"])
                except (TypeError, ValueError, KeyError):
                    raise ValueError("x must be a number")
                try:
                    y = float(item["y"])
                except (TypeError, ValueError, KeyError):
                    raise ValueError("y must be a number")
                try:
                    rotation = float(item["rotation"])
                except (TypeError, ValueError, KeyError):
                    raise ValueError("rotation must be a number")

                # optional tilt onto another face: 90-degree steps about X, then Y
                tilt = {}
                for key in ("tilt_x", "tilt_y"):
                    try:
                        value = float(item.get(key, 0) or 0)
                    except (TypeError, ValueError):
                        raise ValueError("{} must be a number".format(key))
                    if value % 90:
                        raise ValueError("{} must be a multiple of 90".format(key))
                    tilt[key] = value % 360

                # extruder must be int >= 0
                try:
                    extruder = int(item["extruder"])
                except (TypeError, ValueError, KeyError):
                    raise ValueError("extruder must be an integer")
                if extruder < 0:
                    raise ValueError("extruder must be >= 0")

                # Check extruder against agent snapshot if available
                if self._agent_snapshot is not None:
                    snapshot_extruders = self._agent_snapshot.get("summary", {}).get("extruders", [])
                    if extruder >= len(snapshot_extruders):
                        raise ValueError("extruder T{} does not exist".format(extruder))

                cleaned_items.append({
                    "model_id": mid,
                    "x": x,
                    "y": y,
                    "rotation": rotation,
                    "tilt_x": tilt["tilt_x"],
                    "tilt_y": tilt["tilt_y"],
                    "extruder": extruder,
                })

            task = self._make_task("slice", {
                "name": name,
                "items": cleaned_items,
                "start_print": bool(start_print),
            })
            self._tasks.append(task)
            self._host.changed()
            self._host.wake_agent({"type": "agent.wake"})
            return copy.deepcopy(task)

    def submit_fetch(self, path):
        """Submit a fetch task for a file at *path* on the PC agent.

        Parameters
        ----------
        path : str
            Path relative to the PC's Dropbox folder.

        Returns
        -------
        dict
            The created task.

        Raises
        ------
        ValueError
            If the path is empty, absolute, or contains ``".."`` segments.
        """
        with self._lock:
            self._validate_path(path)
            task = self._make_task("fetch", {"path": path})
            self._tasks.append(task)
            self._host.changed()
            self._host.wake_agent({"type": "agent.wake"})
            return copy.deepcopy(task)

    def submit_convert(self, source_id, name):
        """Submit a SolidWorks-to-STL conversion task.

        Parameters
        ----------
        source_id : str
            Internal id of the uploaded SolidWorks file.
        name : str
            Desired output name.

        Returns
        -------
        dict
            The created task.
        """
        with self._lock:
            task = self._make_task("convert", {"source_id": source_id, "name": name})
            self._tasks.append(task)
            self._host.changed()
            self._host.wake_agent({"type": "agent.wake"})
            return copy.deepcopy(task)

    @staticmethod
    def _validate_path(path):
        """Validate a path string.

        Raises ``ValueError("bad path")`` if the path is empty, absolute,
        or contains ``".."`` segments.
        """
        if not isinstance(path, str) or not path:
            raise ValueError("bad path")
        # Check for absolute path
        if path.startswith(("/", "\\")) or ":" in path:
            raise ValueError("bad path")
        # Check for ".." segments
        parts = path.replace("\\", "/").split("/")
        if ".." in parts:
            raise ValueError("bad path")

    # ---------------------------------------------------------- task control

    def claim(self):
        """Claim the oldest queued task for the agent.

        Expired leases are processed first.  The oldest queued task (by
        ``created`` timestamp, then insertion order) is marked ``"running"``
        with a fresh lease.  This also counts as an agent heartbeat.

        Returns
        -------
        dict or None
            A copy of the claimed task, or ``None`` if no task was queued.
        """
        with self._lock:
            now = self._host.now()

            # Expire leases first
            self.expire()

            # Find oldest queued task
            queued = [(i, t) for i, t in enumerate(self._tasks) if t["state"] == "queued"]
            if queued:
                # Sort by created time, then by insertion order (index)
                queued.sort(key=lambda pair: (pair[1]["created"], pair[0]))
                idx, task = queued[0]

                task["state"] = "running"
                task["attempts"] = task.get("attempts", 0) + 1
                task["progress"] = 0
                task["stage"] = "claimed"
                task["lease_until"] = now + _int(self.settings()["lease_seconds"], 120)
                task["updated"] = now

                # Heartbeat (online -> may flip online status)
                prev_online = self._is_online(now)
                self._agent_last_seen = now
                now_online = self._is_online(now)

                if prev_online != now_online:
                    self._host.changed()

                return copy.deepcopy(task)
            else:
                # No queued task, but still record heartbeat
                prev_online = self._is_online(now)
                self._agent_last_seen = now
                now_online = self._is_online(now)

                if prev_online != now_online:
                    self._host.changed()

                return None

    def progress(self, task_id, progress, stage):
        """Report progress on a running task.

        Parameters
        ----------
        task_id : str
        progress : float
            Value clamped to 0..1.
        stage : str
            Stage description, truncated to 200 characters.

        Returns
        -------
        dict
            ``{"ok": True/False, "cancel": bool}``.
        """
        with self._lock:
            task = self._find_task(task_id)
            if task is None or task["state"] != "running":
                return {"ok": False, "cancel": True}

            old_progress = task["progress"]
            old_stage = task["stage"]

            # Clamp progress
            progress = max(0.0, min(1.0, float(progress)))
            # Truncate stage
            stage = str(stage)[:200]

            task["progress"] = progress
            task["stage"] = stage
            task["lease_until"] = self._host.now() + _int(self.settings()["lease_seconds"], 120)
            task["updated"] = self._host.now()

            # Heartbeat
            self._agent_last_seen = self._host.now()

            # Only call changed if progress or stage actually changed
            if progress != old_progress or stage != old_stage:
                self._host.changed()

            return {"ok": True, "cancel": task["cancel_requested"]}

    def complete(self, task_id, result):
        """Mark a running task as completed.

        Parameters
        ----------
        task_id : str
        result : dict
            Result data to store.

        Returns
        -------
        dict or None
            A copy of the completed task, or ``None`` if unknown/not running.
        """
        with self._lock:
            task = self._find_task(task_id)
            if task is None or task["state"] != "running":
                return None

            task["state"] = "done"
            task["progress"] = 1
            task["result"] = dict(result)
            task["lease_until"] = None
            task["updated"] = self._host.now()

            # Trimming: keep at most keep_finished finished tasks
            self._trim_finished()

            self._host.changed()
            return copy.deepcopy(task)

    def fail(self, task_id, error):
        """Mark a running task as failed.

        Parameters
        ----------
        task_id : str
        error : str
            Error description, truncated to 500 characters.

        Returns
        -------
        dict or None
            A copy of the failed task, or ``None`` if unknown/not running.
        """
        with self._lock:
            task = self._find_task(task_id)
            if task is None or task["state"] != "running":
                return None

            error = str(error)[:500]

            if task.get("cancel_requested"):
                task["state"] = "cancelled"
                task["error"] = "cancelled"
            else:
                task["state"] = "failed"
                task["error"] = error

            task["updated"] = self._host.now()

            # Trimming
            self._trim_finished()

            self._host.changed()
            return copy.deepcopy(task)

    def cancel(self, task_id):
        """Cancel a task.

        Queued tasks are cancelled immediately.  Running tasks have
        ``cancel_requested`` set to ``True`` (the agent will notice and
        call ``fail()`` which produces state ``"cancelled"``).

        Returns
        -------
        bool
            ``False`` if the task is finished or unknown.
        """
        with self._lock:
            task = self._find_task(task_id)
            if task is None:
                return False

            if task["state"] == "queued":
                task["state"] = "cancelled"
                task["updated"] = self._host.now()
                self._trim_finished()
                self._host.changed()
                return True
            elif task["state"] == "running":
                task["cancel_requested"] = True
                self._host.changed()
                return True
            else:
                return False

    def note_print(self, task_id, started, note):
        """Record print-started status and an optional note on a done task's result.

        Parameters
        ----------
        task_id : str
        started : bool
            Whether the print actually started.
        note : str
            Free-form note.
        """
        with self._lock:
            task = self._find_task(task_id)
            if task is None or task["state"] != "done":
                return
            if task["result"] is None:
                task["result"] = {}
            task["result"]["print_started"] = bool(started)
            task["result"]["print_note"] = str(note)
            self._host.changed()

    def clear_finished(self):
        """Remove all done/failed/cancelled tasks.

        Returns
        -------
        int
            Number of tasks removed.
        """
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t["state"] not in ("done", "failed", "cancelled")]
            removed = before - len(self._tasks)
            if removed:
                self._host.changed()
            return removed

    # --------------------------------------------------------- lease expiry

    def expire(self):
        """Expire leases on running tasks that have timed out.

        Must be called while holding the lock.  Tasks whose ``lease_until``
        is past are either re-queued (if ``attempts < max_attempts``) or
        marked failed.
        """
        now = self._host.now()
        max_attempts = _int(self.settings()["max_attempts"], 3, 1)
        changed = False

        for task in self._tasks:
            if task["state"] != "running":
                continue
            lease = task.get("lease_until")
            if lease is None:
                continue
            if lease >= now:
                continue

            if task["attempts"] >= max_attempts:
                task["state"] = "failed"
                task["error"] = "the PC agent stopped responding"
            else:
                task["state"] = "queued"
                task["lease_until"] = None
                task["stage"] = "re-queued"

            task["updated"] = now
            changed = True

        if changed:
            self._trim_finished()
            self._host.changed()

    def _trim_finished(self):
        """Remove oldest finished tasks beyond keep_finished limit.

        Must be called while holding the lock.
        """
        keep = _int(self.settings()["keep_finished"], 50, 0)
        finished = []
        running = []
        for t in self._tasks:
            if t["state"] in ("done", "failed", "cancelled"):
                finished.append(t)
            else:
                running.append(t)

        if len(finished) > keep:
            finished.sort(key=lambda t: t["created"])
            finished = finished[len(finished) - keep:]

        self._tasks = running + finished

    # ----------------------------------------------------------- tasks

    def tasks(self):
        """Return all tasks newest-first (by ``created`` timestamp)."""
        with self._lock:
            return sorted(copy.deepcopy(self._tasks), key=lambda t: t["created"], reverse=True)

    def _find_task(self, task_id):
        """Find a task by id (caller must hold the lock)."""
        for t in self._tasks:
            if t["id"] == task_id:
                return t
        return None

    # --------------------------------------------------------- agent status

    def _is_online(self, now):
        """Check if the agent is currently online (caller must hold the lock)."""
        if self._agent_last_seen is None:
            return False
        timeout = _int(self.settings()["agent_timeout"], 150)
        return (now - self._agent_last_seen) <= timeout

    def heartbeat(self, info):
        """Process a heartbeat from the PC agent.

        Parameters
        ----------
        info : dict
            ``{"version": str, "snapshot": dict or None}``.

        Returns
        -------
        dict
            ``{"ok": True, "queued": <number of queued tasks>}``.
        """
        with self._lock:
            now = self._host.now()
            version = info.get("version")
            snapshot = info.get("snapshot")

            prev_online = self._is_online(now)
            prev_version = self._agent_version
            prev_snapshot = self._agent_snapshot

            self._agent_last_seen = now

            if version is not None:
                self._agent_version = version
            if snapshot is not None:
                self._agent_snapshot = snapshot

            now_online = self._is_online(now)
            version_changed = prev_version != self._agent_version
            snapshot_changed = prev_snapshot is not snapshot

            queued_count = sum(1 for t in self._tasks if t["state"] == "queued")

            # Only call changed if online flag flipped, version changed, or new snapshot
            if prev_online != now_online or version_changed or snapshot_changed:
                self._host.changed()

            return {"ok": True, "queued": queued_count}

    def agent_status(self):
        """Return current agent status.

        Returns
        -------
        dict
            ``{"online": bool, "last_seen": float or None, "version": str or None,
            "snapshot": dict or None}``.
        """
        with self._lock:
            now = self._host.now()
            return {
                "online": self._is_online(now),
                "last_seen": self._agent_last_seen,
                "version": self._agent_version,
                "snapshot": copy.deepcopy(self._agent_snapshot),
            }

    # ------------------------------------------------------- listing requests

    def request_listing(self, path):
        """Request a directory listing from the agent.

        Parameters
        ----------
        path : str
            Directory path (empty string means root).

        Returns
        -------
        str
            Request id to pass to ``complete_listing()``.
        """
        with self._lock:
            # Validate path (empty is allowed for root)
            if path:
                self._validate_path(path)

            now = self._host.now()
            timeout = _int(self.settings()["listing_timeout"], 10)
            for old in [k for k, v in self._listings.items() if now - v["created"] > timeout]:
                del self._listings[old]  # requests the agent never answered

            rid = self._host.new_id()
            self._listings[rid] = {"path": path, "created": now}
            self._host.wake_agent({
                "type": "agent.list",
                "request_id": rid,
                "path": path,
            })
            return rid

    def complete_listing(self, request_id):
        """Retrieve a pending listing request.

        Parameters
        ----------
        request_id : str

        Returns
        -------
        dict or None
            ``{"path": str, "created": float}`` or ``None`` if unknown
            or older than ``listing_timeout`` seconds.
        """
        with self._lock:
            req = self._listings.pop(request_id, None)
            if req is None:
                return None
            now = self._host.now()
            timeout = _int(self.settings()["listing_timeout"], 10)
            if (now - req["created"]) > timeout:
                return None
            return req