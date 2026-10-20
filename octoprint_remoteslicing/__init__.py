# coding=utf-8
"""OctoPrint glue for Remote Slicing.

Wires the lock-free Core (core.py) to OctoPrint's event system, HTTP blueprint,
socket push, and plugin-data-folder persistence.  Python 3.7 compatible.
"""

import io
import json
import os
import re
import shutil
import threading
import time
import uuid

import flask
import octoprint.plugin
import octoprint.util

from .core import DEFAULT_SETTINGS, Core

STATE_FILE = "state.json"
_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")


def _can(permission_name):
    """True when the current request's user holds the permission."""
    try:
        from octoprint.access.permissions import Permissions

        return getattr(Permissions, permission_name).can()
    except Exception:
        return True


class RemoteSlicingPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.BlueprintPlugin,
    octoprint.plugin.EventHandlerPlugin,
):
    def __init__(self):
        self._core = None
        self._save_lock = threading.Lock()
        self._timer = None
        self._pending_print = None  # {"task_id", "path", "deadline"}

    # ---------------------------------------------------------------- startup

    def initialize(self):
        # Runs before the server answers requests: the PC agent reconnects the moment OctoPrint is
        # reachable, and its first heartbeat must find the queue already there.
        state = self._load_json(STATE_FILE, {})
        self._core = Core(self, get_settings=self._current_settings, state=state)

        # Ensure model and source sub-directories exist
        os.makedirs(os.path.dirname(self._model_path("x")), exist_ok=True)
        os.makedirs(os.path.dirname(self._source_path("x", "")), exist_ok=True)

    def on_after_startup(self):
        # Periodic tick: expire leases, check pending prints
        self._timer = octoprint.util.RepeatedTimer(30, self._tick, daemon=True)
        self._timer.start()

        self._logger.info("Remote Slicing ready")

    def on_shutdown(self):
        if self._timer is not None:
            self._timer.cancel()
        if self._core is not None:
            self._write_json(STATE_FILE, self._core.persistent_state())

    # ------------------------------------------------------------ persistence

    def _load_json(self, name, default):
        path = os.path.join(self.get_plugin_data_folder(), name)
        if not os.path.exists(path):
            return default
        try:
            with io.open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            self._logger.exception("Could not read %s", path)
            return default

    def _write_json(self, name, data):
        path = os.path.join(self.get_plugin_data_folder(), name)
        tmp = path + ".tmp"
        with self._save_lock:
            with io.open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2))
            os.replace(tmp, path)

    # -------------------------------------------------- host interface (core)

    def now(self):
        """Current epoch seconds."""
        return time.time()

    def new_id(self):
        """Short unique id for models and tasks."""
        return uuid.uuid4().hex[:12]

    def changed(self):
        """Persist state and push snapshot to every connected browser."""
        try:
            self._write_json(STATE_FILE, self._core.persistent_state())
        except Exception:
            self._logger.exception("Could not save state")
        self._push_state()

    def _push_state(self):
        """Push a state snapshot over the plugin socket."""
        payload = {"type": "state"}
        payload.update(self._core.snapshot())
        self._plugin_manager.send_plugin_message(self._identifier, payload)

    def wake_agent(self, msg):
        """Push a wake-up message to the PC agent (browsers ignore it)."""
        self._plugin_manager.send_plugin_message(self._identifier, msg)

    def _current_settings(self):
        """Read current user settings keyed by DEFAULT_SETTINGS keys."""
        return {key: self._settings.get([key]) for key in DEFAULT_SETTINGS}

    # --------------------------------------------------------- file helpers

    def _model_path(self, model_id):
        """Return the full path for a model STL file."""
        return os.path.join(self.get_plugin_data_folder(), "models", "{}.stl".format(model_id))

    def _source_path(self, source_id, ext):
        """Return the full path for a source file."""
        return os.path.join(self.get_plugin_data_folder(), "sources", "{}{}".format(source_id, ext))

    def _valid_id(self, id_str):
        """True when *id_str* matches the safe id pattern."""
        return bool(_ID_RE.match(id_str))

    # --------------------------------------------------------------- timers

    def _tick(self):
        """Called every 30 s: expire leases and check pending prints."""
        if self._core is None:
            return
        self._core.expire()
        self._check_pending_print()

    # ------------------------------------------------------- starting prints

    def _start_print(self, task):
        """Try to start a print for a finished slice task."""
        path = task["result"]["path"]
        task_id = task["id"]

        if self._printer.get_state_id() != "OPERATIONAL":
            state_str = self._printer.get_state_string()
            self._core.note_print(task_id, False, "printer is {}, the file was only uploaded".format(state_str))
            return

        try:
            self._printer.select_file(path, False, printAfterSelect=True)  # "printing=" is silently swallowed by **kwargs
        except Exception as exc:
            self._logger.exception("Could not start %s", path)
            self._core.note_print(task_id, False, "could not start the print: {}".format(exc))
            return

        self._pending_print = {"task_id": task_id, "path": path, "deadline": self.now() + 20}

    def _check_pending_print(self):
        """If the pending print deadline passed, mark it as failed."""
        if self._pending_print is None:
            return
        if self.now() >= self._pending_print["deadline"]:
            self._core.note_print(
                self._pending_print["task_id"],
                False,
                "the printer did not start the print",
            )
            self._pending_print = None

    # ------------------------------------------------------------ events

    def on_event(self, event, payload):
        if self._core is None:
            return
        payload = payload or {}
        if event == "ClientOpened":
            self._push_state()
        elif event == "PrintStarted":
            self._on_print_started(payload.get("path"))
        # After every event, check pending print deadline
        self._check_pending_print()

    def _on_print_started(self, print_path):
        """Handle PrintStarted: confirm pending print or ignore."""
        if self._pending_print is None:
            return
        if print_path == self._pending_print["path"]:
            self._core.note_print(self._pending_print["task_id"], True, "print started")
            self._pending_print = None

    # -------------------------------------------------------------- settings

    def get_settings_defaults(self):
        return dict(DEFAULT_SETTINGS)

    # ---------------------------------------------------------- assets/views

    def get_assets(self):
        return {
            "js": ["js/remoteslicing_bed.js", "js/remoteslicing.js"],
            "css": ["css/remoteslicing.css"],
        }

    def is_template_autoescaped(self):
        return True

    def get_template_configs(self):
        return [
            {"type": "tab", "name": "Remote Slicing", "custom_bindings": True},
            {"type": "settings", "name": "Remote Slicing", "custom_bindings": False},
        ]

    # ----------------------------------------------------------- blueprint

    def is_blueprint_protected(self):
        return True

    def is_blueprint_csrf_protected(self):
        return True

    # ----- browser routes -----

    @octoprint.plugin.BlueprintPlugin.route("/state", methods=["GET"])
    def state_handler(self):
        """Return the current state snapshot."""
        if not _can("STATUS"):
            flask.abort(403)
        return flask.jsonify(self._core.snapshot())

    @octoprint.plugin.BlueprintPlugin.route("/models", methods=["POST"])
    def models_upload(self):
        """Upload a model (.stl) or SolidWorks file (.sldprt)."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)

        # Read OctoPrint's temp-file metadata
        name_suffix = self._settings.global_get(["server", "uploads", "nameSuffix"])
        path_suffix = self._settings.global_get(["server", "uploads", "pathSuffix"])

        orig_name = flask.request.values.get("file." + name_suffix, "")
        temp_path = flask.request.values.get("file." + path_suffix, "")

        if not temp_path or not os.path.exists(temp_path):
            return flask.jsonify({"error": "no file received"}), 400

        ext = os.path.splitext(orig_name)[1].lower()
        file_size = os.path.getsize(temp_path)

        if ext == ".stl":
            name = os.path.splitext(orig_name)[0]
            try:
                model = self._core.add_model(name, file_size, "upload")
            except ValueError as exc:
                return flask.jsonify({"error": str(exc)}), 409
            dest = self._model_path(model["id"])
            try:
                shutil.move(temp_path, dest)
            except Exception:
                self._core.remove_model(model["id"])
                self._logger.exception("Could not move model file")
                return flask.jsonify({"error": "could not save model"}), 500
            return flask.jsonify({"model": model})

        elif ext == ".sldprt":
            sid = self.new_id()
            name = os.path.splitext(orig_name)[0]
            dest = self._source_path(sid, ".sldprt")
            try:
                shutil.move(temp_path, dest)
            except Exception:
                self._logger.exception("Could not move source file")
                return flask.jsonify({"error": "could not save source"}), 500
            try:
                task = self._core.submit_convert(sid, name)
            except ValueError as exc:
                return flask.jsonify({"error": str(exc)}), 409
            return flask.jsonify({"task": task})

        else:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            return flask.jsonify({"error": "only .stl and .sldprt files"}), 409

    @octoprint.plugin.BlueprintPlugin.route("/models/<model_id>/stl", methods=["GET"])
    def models_download(self, model_id):
        """Download a model STL file."""
        if not _can("FILES_DOWNLOAD"):
            flask.abort(403)
        if not self._valid_id(model_id):
            flask.abort(404)
        model = self._core.get_model(model_id)
        if model is None:
            flask.abort(404)
        path = self._model_path(model_id)
        if not os.path.exists(path):
            flask.abort(404)
        return flask.send_file(path, mimetype="model/stl")

    @octoprint.plugin.BlueprintPlugin.route("/models/<model_id>/delete", methods=["POST"])
    def models_delete(self, model_id):
        """Delete a model."""
        if not _can("FILES_DELETE"):
            flask.abort(403)
        if not self._valid_id(model_id):
            flask.abort(404)
        try:
            ok = self._core.remove_model(model_id)
        except ValueError as exc:
            return flask.jsonify({"error": str(exc)}), 409
        if not ok:
            flask.abort(404)
        path = self._model_path(model_id)
        if os.path.exists(path):
            os.remove(path)
        return flask.jsonify({"ok": True})

    @octoprint.plugin.BlueprintPlugin.route("/slice", methods=["POST"])
    def slice_handler(self):
        """Submit a new slicing task."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)
        data = flask.request.get_json(force=True, silent=True) or {}
        start_print = data.get("start_print", False)
        if start_print and not _can("PRINT"):
            flask.abort(403)
        try:
            task = self._core.submit_slice(
                data.get("name", ""),
                data.get("items", []),
                start_print,
            )
        except ValueError as exc:
            return flask.jsonify({"error": str(exc)}), 409
        return flask.jsonify({"task": task})

    @octoprint.plugin.BlueprintPlugin.route("/fetch", methods=["POST"])
    def fetch_handler(self):
        """Submit a fetch task."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)
        data = flask.request.get_json(force=True, silent=True) or {}
        try:
            task = self._core.submit_fetch(data.get("path", ""))
        except ValueError as exc:
            return flask.jsonify({"error": str(exc)}), 409
        return flask.jsonify({"task": task})

    @octoprint.plugin.BlueprintPlugin.route("/list", methods=["POST"])
    def list_handler(self):
        """Request a directory listing from the agent."""
        if not _can("FILES_LIST"):
            flask.abort(403)
        if not self._core.agent_status()["online"]:
            return flask.jsonify({"error": "the PC agent is offline"}), 409
        data = flask.request.get_json(force=True, silent=True) or {}
        request_id = self._core.request_listing(data.get("path", ""))
        return flask.jsonify({"request_id": request_id})

    @octoprint.plugin.BlueprintPlugin.route("/tasks/<task_id>/cancel", methods=["POST"])
    def task_cancel(self, task_id):
        """Cancel a task."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)
        ok = self._core.cancel(task_id)
        return flask.jsonify({"ok": ok})

    @octoprint.plugin.BlueprintPlugin.route("/tasks/clear", methods=["POST"])
    def task_clear(self):
        """Clear all finished tasks."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)
        removed = self._core.clear_finished()
        return flask.jsonify({"removed": removed})

    # ----- agent routes -----

    @octoprint.plugin.BlueprintPlugin.route("/agent/heartbeat", methods=["POST"])
    def agent_heartbeat(self):
        """Agent heartbeat."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)
        data = flask.request.get_json(force=True, silent=True) or {}
        result = self._core.heartbeat(data)
        return flask.jsonify(result)

    @octoprint.plugin.BlueprintPlugin.route("/agent/claim", methods=["POST"])
    def agent_claim(self):
        """Agent claims the next queued task."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)
        task = self._core.claim()
        return flask.jsonify({"task": task})

    @octoprint.plugin.BlueprintPlugin.route("/agent/models/<model_id>", methods=["GET"])
    def agent_model(self, model_id):
        """Send a model file to the agent."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)
        if not self._valid_id(model_id):
            flask.abort(404)
        model = self._core.get_model(model_id)
        if model is None:
            flask.abort(404)
        path = self._model_path(model_id)
        if not os.path.exists(path):
            flask.abort(404)
        return flask.send_file(path, mimetype="model/stl")

    @octoprint.plugin.BlueprintPlugin.route("/agent/sources/<source_id>", methods=["GET"])
    def agent_source(self, source_id):
        """Send a source file to the agent (glob for extension)."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)
        if not self._valid_id(source_id):
            flask.abort(404)
        import glob

        pattern = os.path.join(self.get_plugin_data_folder(), "sources", "{}.*".format(source_id))
        matches = glob.glob(pattern)
        if not matches:
            flask.abort(404)
        return flask.send_file(matches[0])

    @octoprint.plugin.BlueprintPlugin.route("/agent/tasks/<task_id>/progress", methods=["POST"])
    def agent_progress(self, task_id):
        """Agent reports progress on a task."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)
        data = flask.request.get_json(force=True, silent=True) or {}
        try:
            progress = float(data["progress"])
        except (TypeError, ValueError, KeyError):
            return flask.jsonify({"error": "progress must be numeric"}), 400
        stage = data.get("stage", "working")
        result = self._core.progress(task_id, progress, stage)
        return flask.jsonify(result)

    @octoprint.plugin.BlueprintPlugin.route("/agent/tasks/<task_id>/done", methods=["POST"])
    def agent_done(self, task_id):
        """Agent marks a task as done."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)
        data = flask.request.get_json(force=True, silent=True) or {}
        task = self._core.complete(task_id, data.get("result", {}))
        if task is None:
            flask.abort(404)
        # Clean up source file for convert tasks
        if task["kind"] == "convert":
            src = self._source_path(task["payload"]["source_id"], ".sldprt")
            if os.path.exists(src):
                os.remove(src)
        # Auto-start print for slice tasks
        if task["kind"] == "slice" and task["payload"].get("start_print"):
            self._start_print(task)
        return flask.jsonify({"ok": True})

    @octoprint.plugin.BlueprintPlugin.route("/agent/tasks/<task_id>/failed", methods=["POST"])
    def agent_failed(self, task_id):
        """Agent marks a task as failed."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)
        data = flask.request.get_json(force=True, silent=True) or {}
        task = self._core.fail(task_id, data.get("error", "unknown"))
        if task is None:
            flask.abort(404)
        # Clean up source file for convert tasks
        if task["kind"] == "convert":
            src = self._source_path(task["payload"]["source_id"], ".sldprt")
            if os.path.exists(src):
                os.remove(src)
        return flask.jsonify({"ok": True})

    @octoprint.plugin.BlueprintPlugin.route("/agent/tasks/<task_id>/model", methods=["POST"])
    def agent_model_upload(self, task_id):
        """Agent uploads the model file for a fetch or convert task."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)

        # Find the running task
        tasks = self._core.tasks()
        target = None
        for t in tasks:
            if t["id"] == task_id:
                target = t
                break
        if target is None or target["state"] != "running" or target["kind"] not in ("fetch", "convert"):
            flask.abort(409)

        # Read OctoPrint's temp-file metadata
        name_suffix = self._settings.global_get(["server", "uploads", "nameSuffix"])
        path_suffix = self._settings.global_get(["server", "uploads", "pathSuffix"])
        temp_path = flask.request.values.get("file." + path_suffix, "")
        model_name = os.path.splitext(flask.request.values.get("name", ""))[0] or "model"

        if not temp_path or not os.path.exists(temp_path):
            return flask.jsonify({"error": "no file received"}), 400

        file_size = os.path.getsize(temp_path)

        # Determine source and origin
        if target["kind"] == "fetch":
            source = "dropbox"
            origin = target["payload"].get("path", "")
        else:
            source = "converted"
            origin = target["payload"].get("name", "") + ".sldprt"

        # Create the model
        try:
            model = self._core.add_model(model_name, file_size, source, origin=origin)
        except ValueError as exc:
            return flask.jsonify({"error": str(exc)}), 409

        # Move file into place
        dest = self._model_path(model["id"])
        try:
            shutil.move(temp_path, dest)
        except Exception:
            self._core.remove_model(model["id"])
            self._logger.exception("Could not move model file from agent")
            return flask.jsonify({"error": "could not save model"}), 500

        # Mark task complete
        self._core.complete(task_id, {"model_id": model["id"]})

        # Delete source file for convert tasks
        if target["kind"] == "convert":
            src = self._source_path(target["payload"]["source_id"], ".sldprt")
            if os.path.exists(src):
                os.remove(src)

        return flask.jsonify({"model": model})

    @octoprint.plugin.BlueprintPlugin.route("/agent/listing", methods=["POST"])
    def agent_listing(self):
        """Agent responds to a directory listing request."""
        if not _can("FILES_UPLOAD"):
            flask.abort(403)
        data = flask.request.get_json(force=True, silent=True) or {}
        req = self._core.complete_listing(data.get("request_id", ""))
        if req:
            self._plugin_manager.send_plugin_message(self._identifier, {
                "type": "listing",
                "request_id": data.get("request_id", ""),
                "path": data.get("path", ""),
                "dirs": data.get("dirs", []),
                "files": data.get("files", []),
                "truncated": data.get("truncated", False),
                "error": data.get("error"),
            })
            return flask.jsonify({"ok": True})
        else:
            return flask.jsonify({"ok": False})

    # ----------------------------------------------------------- update hook

    def get_update_information(self):
        return {
            "remoteslicing": {
                "displayName": "Remote Slicing",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "Luix333",
                "repo": "OctoPrint-RemoteSlicing",
                "current": self._plugin_version,
                "pip": "https://github.com/Luix333/OctoPrint-RemoteSlicing/archive/{target_version}.zip",
            }
        }

    # ----------------------------------------------------------- bodysize hook

    def bodysize_hook(self, current_max_body_sizes, *args, **kwargs):
        """Allow large file uploads for model and agent routes."""
        return [
            ("POST", r"/models", 512 * 1024 * 1024),
            ("POST", r"/agent/tasks/[^/]+/model", 512 * 1024 * 1024),
        ]


__plugin_name__ = "Remote Slicing"
__plugin_pythoncompat__ = ">=3.7,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = RemoteSlicingPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.server.http.bodysize": __plugin_implementation__.bodysize_hook,
    }
