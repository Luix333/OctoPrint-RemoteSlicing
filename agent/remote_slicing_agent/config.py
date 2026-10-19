"""Configuration and path helpers for the remote slicing agent.

Manages the agent home directory, JSON configuration persistence, and
Dropbox root auto-detection.
"""

import json
import os
import tempfile


# ------------------------------------------------------------------
# Path helpers
# ------------------------------------------------------------------

def home():
    """Return the agent home directory.

    Uses ``REMOTE_SLICING_HOME`` if set and non-empty, otherwise falls
    back to ``%LOCALAPPDATA%\\RemoteSlicing`` (or ``~/RemoteSlicing``).
    """
    val = os.environ.get("REMOTE_SLICING_HOME", "")
    if val:
        return val
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return os.path.join(local, "RemoteSlicing")
    return os.path.join(os.path.expanduser("~"), "RemoteSlicing")


def snapshot_path():
    """Return the full path to ``snapshot.json``."""
    return os.path.join(home(), "snapshot.json")


def config_path():
    """Return the full path to ``agent.json``."""
    return os.path.join(home(), "agent.json")


def log_path():
    """Return the full path to ``agent.log``."""
    return os.path.join(home(), "agent.log")


def work_dir():
    """Return the full path to the ``work`` subdirectory."""
    return os.path.join(home(), "work")


# ------------------------------------------------------------------
# Defaults
# ------------------------------------------------------------------

DEFAULTS = {
    "octoprint_url": "",
    "api_key": "",
    "dropbox_root": "",
    "heartbeat_seconds": 60,
}


def default_dropbox_root():
    """Auto-detect the Dropbox root directory.

    Reads ``%LOCALAPPDATA%\\Dropbox\\info.json`` (with
    ``%APPDATA%\\Dropbox\\info.json`` as fallback) and returns the
    personal or business path.  Falls back to ``~/Dropbox`` on any
    error.
    """
    fallback = os.path.join(os.path.expanduser("~"), "Dropbox")
    try:
        local = os.environ.get("LOCALAPPDATA")
        appdata = os.environ.get("APPDATA")
        candidates = []
        if local:
            candidates.append(os.path.join(local, "Dropbox", "info.json"))
        if appdata:
            candidates.append(os.path.join(appdata, "Dropbox", "info.json"))
        for path in candidates:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            personal = data.get("personal")
            if personal and isinstance(personal, dict):
                p = personal.get("path")
                if p:
                    return p
            business = data.get("business")
            if business and isinstance(business, dict):
                p = business.get("path")
                if p:
                    return p
        return fallback
    except Exception:
        return fallback


# ------------------------------------------------------------------
# Load / Save
# ------------------------------------------------------------------

def load():
    """Load the agent configuration.

    Returns a dict built from ``DEFAULTS`` merged with the on-disk
    JSON file.  Unknown keys in the file are preserved.  If the file
    is missing or corrupt a copy of ``DEFAULTS`` is returned.

    The ``dropbox_root`` key is filled with ``default_dropbox_root()``
    in the returned dict when it is empty (this is *not* persisted).
    The ``octoprint_url`` key has a trailing slash stripped.
    """
    result = dict(DEFAULTS)
    try:
        with open(config_path(), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return result
        result.update(data)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    # Fill dropbox_root if empty (do not save)
    if not result.get("dropbox_root"):
        result["dropbox_root"] = default_dropbox_root()
    # Strip trailing slash from octoprint_url
    url = result.get("octoprint_url", "")
    if url:
        result["octoprint_url"] = url.rstrip("/")
    return result


def save(cfg):
    """Persist *cfg* to disk.

    Creates the home directory if needed, writes JSON with indent 2 to
    a temporary file in the same folder, then atomically replaces the
    target via ``os.replace``.
    """
    h = home()
    os.makedirs(h, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=h, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, config_path())
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------
# Snapshot helpers
# ------------------------------------------------------------------

def snapshot_stat():
    """Return ``(st_mtime_ns, st_size)`` for the snapshot file.

    Returns ``None`` if the file does not exist.
    """
    try:
        st = os.stat(snapshot_path())
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def read_snapshot_summary():
    """Return a minimal summary of the current snapshot.

    Loads ``snapshot.json`` and returns only the keys
    ``created``, ``cura_version``, ``summary``, and ``bed``.
    Returns ``None`` if the file is missing or corrupt.
    """
    try:
        with open(snapshot_path(), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return {
            "created": data["created"],
            "cura_version": data["cura_version"],
            "summary": data["summary"],
            "bed": data["bed"],
        }
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None
