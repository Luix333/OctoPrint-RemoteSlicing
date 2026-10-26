"""One job for several files picked together in Explorer.

Explorer runs a context-menu command once per selected file, each in its own process, all within a few hundred
milliseconds. Every process drops a request for its file into a spool folder. The first one to take a named mutex
becomes the collector: it waits until no new request for the same menu choice has arrived for a moment, claims
them all (by renaming, so each request is claimed exactly once), lets go of the mutex and handles the whole set.
The others see their request claimed and exit without a window. A request nobody claims is left by a process that
died; requests older than a minute are deleted.
"""

import json
import os
import re
import time
import uuid

from . import config, winproc

MUTEX = "Local\\RemoteSlicingExplorerBatch"
FIRST_WAIT_S = 1.2  # the collector always waits this long: Python takes a moment to start in every process
QUIET_S = 0.6       # ... and until no new request has come for this long
MAX_WAIT_S = 6.0
STALE_S = 60.0
POLL_S = 0.05


def spool_dir():
    return os.path.join(config.work_dir(), "explorer")


def _action_tag(action):
    return re.sub(r"[^A-Za-z0-9-]", "_", action)[:40] or "x"


def _requests(folder, tag):
    """(name, path) of this choice's waiting requests, oldest first (the name starts with a time stamp)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted((n, os.path.join(folder, n)) for n in names if n.endswith(".%s.req" % tag))


def _drop_stale(folder):
    now = time.time()
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        path = os.path.join(folder, name)
        try:
            if now - os.path.getmtime(path) > STALE_S:
                os.remove(path)
        except OSError:
            pass


def gather(path, action, clock=time.monotonic, sleep=time.sleep):
    """The files to handle in this process for one Explorer menu choice: ``path`` plus every file Explorer handed
    to sibling processes for the same choice, in the order they were selected. None when another process
    collected this one."""
    folder = spool_dir()
    os.makedirs(folder, exist_ok=True)
    tag = _action_tag(action)
    name = "%020d.%s.%s.req" % (time.time_ns(), uuid.uuid4().hex[:8], tag)
    mine = os.path.join(folder, name)
    with open(mine + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"file": path, "action": action}, f)
    os.replace(mine + ".tmp", mine)

    deadline = clock() + MAX_WAIT_S + 5
    while True:
        lock = winproc.single_instance(MUTEX)
        if lock is not None:
            break
        if not os.path.exists(mine):
            return None  # a collector took it
        if clock() > deadline:  # the collector hung; don't lose the file
            return [path]
        sleep(POLL_S)
    try:
        if not os.path.exists(mine):
            return None  # claimed by the collector just before it let go
        _drop_stale(folder)
        start = last_change = clock()
        seen = len(_requests(folder, tag))
        while True:
            now = clock()
            count = len(_requests(folder, tag))
            if count != seen:
                seen, last_change = count, now
            if (now - start >= FIRST_WAIT_S and now - last_change >= QUIET_S) or now - start >= MAX_WAIT_S:
                break
            sleep(POLL_S)
        files = []
        for req_name, req_path in _requests(folder, tag):
            taken = req_path + ".taken"
            try:
                os.replace(req_path, taken)
            except OSError:
                continue
            try:
                with open(taken, encoding="utf-8") as f:
                    file = json.load(f).get("file")
                if file and file not in files:
                    files.append(file)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    os.remove(taken)
                except OSError:
                    pass
        if path not in files:
            files.insert(0, path)
        return files
    finally:
        winproc.release(lock)
