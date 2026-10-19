"""Small HTTP client for OctoPrint.

Uses only the standard library (``urllib.request``).  One short-lived
connection per call -- no keep-alive pool.
"""

import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from remote_slicing_agent import __version__


# ---------------------------------------------------------------------------
# ApiError
# ---------------------------------------------------------------------------

class ApiError(Exception):
    """Raised when an OctoPrint HTTP call fails.

    Attributes
    ----------
    status : int
        HTTP status code, or ``0`` for network-level failures.
    body : str
        Response body (truncated to 500 characters) or error description.
    """

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body[:500] if body else ""
        super().__init__(f"HTTP {status}: {self.body}")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class Client:
    """Thin HTTP client for the OctoPrint API.

    Every request sends ``X-Api-Key`` (when *api_key* is non-empty) and
    ``User-Agent: RemoteSlicingAgent/<version>``.
    """

    def __init__(self, url: str, api_key: str, timeout: int = 30):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    # -- low-level request ---------------------------------------------------

    def _headers(self, extra=None):
        """Return the default headers merged with *extra*."""
        hdrs = {"User-Agent": f"RemoteSlicingAgent/{__version__}"}
        if self.api_key:
            hdrs["X-Api-Key"] = self.api_key
        if extra:
            hdrs.update(extra)
        return hdrs

    def request(self, method, path, body=None, headers=None, timeout=None):
        """Issue an HTTP request.

        Parameters
        ----------
        method : str
            HTTP method (GET, POST, etc.).
        path : str
            URL path starting with ``/``.
        body : bytes or None
            Request body.
        headers : dict or None
            Extra headers merged on top of the defaults.
        timeout : int or None
            Override the default timeout.

        Returns
        -------
        tuple[int, bytes]
            ``(status_code, response_bytes)``.

        Raises
        ------
        ApiError
        """
        effective_timeout = timeout if timeout is not None else self.timeout
        url = f"{self.url}{path}"
        req = urllib.request.Request(
            url,
            data=body,
            headers=self._headers(headers),
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = repr(raw)
            raise ApiError(status=exc.code, body=text)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ApiError(status=0, body=str(exc))

    # -- JSON helpers --------------------------------------------------------

    def get_json(self, path):
        """GET *path* and return parsed JSON."""
        _status, data = self.request("GET", path)
        if not data:
            return None
        return json.loads(data)

    def post_json(self, path, payload):
        """POST JSON *payload* to *path* and return parsed JSON (or ``None``)."""
        body = json.dumps(payload).encode("utf-8")
        extra = {"Content-Type": "application/json"}
        _status, data = self.request("POST", path, body=body, headers=extra)
        if not data:
            return None
        return json.loads(data)

    # -- download ------------------------------------------------------------

    def download(self, path, dest_path, chunk=1 << 20):
        """Stream a response body to disk.

        Writes to ``dest_path.part`` first, then atomically replaces
        *dest_path*.  Returns the number of bytes written.  Cleans up
        the ``.part`` file on failure.
        """
        part_path = f"{dest_path}.part"
        written = 0
        url = f"{self.url}{path}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp, open(part_path, "wb") as f:
                while True:
                    piece = resp.read(chunk)
                    if not piece:
                        break
                    f.write(piece)
                    written += len(piece)
            os.replace(part_path, dest_path)
        except urllib.error.HTTPError as exc:
            try:
                os.remove(part_path)
            except OSError:
                pass
            raw = exc.read()
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = repr(raw)
            raise ApiError(status=exc.code, body=text)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            try:
                os.remove(part_path)
            except OSError:
                pass
            raise ApiError(status=0, body=str(exc))
        return written

    # -- upload --------------------------------------------------------------

    def upload_file(self, path, field, file_path, filename, fields=None):
        """Multipart/form-data POST.

        Parameters
        ----------
        path : str
            API path.
        field : str
            Name of the file field.
        file_path : str
            Path to the file on disk.
        filename : str
            Filename sent to the server (``"`` and ``\\`` removed).
        fields : dict or None
            Extra text fields written before the file part.

        Returns
        -------
        Parsed JSON response (or ``None``).
        """
        boundary = str(__import__("uuid").uuid4().hex)
        safe_name = filename.replace('"', "").replace("\\", "")
        tmp = tempfile.TemporaryFile()

        # Write extra text fields
        if fields:
            for key, value in fields.items():
                tmp.write(f"--{boundary}\r\n".encode())
                tmp.write(
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                    f"{value}\r\n".encode()
                )

        # Write file part
        tmp.write(f"--{boundary}\r\n".encode())
        tmp.write(
            f'Content-Disposition: form-data; name="{field}"; '
            f'filename="{safe_name}"\r\n\r\n'.encode()
        )
        with open(file_path, "rb") as src:
            while True:
                piece = src.read(1 << 20)
                if not piece:
                    break
                tmp.write(piece)
        tmp.write(b"\r\n")

        # Closing boundary
        tmp.write(f"--{boundary}--\r\n".encode())

        length = tmp.tell()
        tmp.seek(0)
        extra = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Content-Length": str(length)}
        try:
            # urllib streams a file object body in blocks when Content-Length is given
            _status, data = self.request("POST", path, body=tmp, headers=extra, timeout=max(self.timeout, 600))
        finally:
            tmp.close()

        if not data:
            return None
        return json.loads(data)

    # -- gcode upload shortcut -----------------------------------------------

    def upload_gcode(self, local_path, filename, folder="RemoteSlicing"):
        """Upload a G-code file to OctoPrint's local storage.

        Returns the path reported by the server.
        """
        resp = self.upload_file(
            "/api/files/local",
            "file",
            local_path,
            filename,
            fields={
                "path": folder,
                "select": "false",
                "print": "false",
            },
        )
        return resp["files"]["local"]["path"]

    # -- passive login -------------------------------------------------------

    def passive_login(self):
        """Perform a passive login.

        Returns a dict with at least ``"name"`` and ``"session"`` keys.
        """
        result = self.post_json("/api/login", {"passive": True})
        if not isinstance(result, dict):
            raise ApiError(status=401, body="no session")
        if "name" not in result or "session" not in result:
            raise ApiError(status=401, body="no session")
        return result

    # -- plugin shortcuts ----------------------------------------------------

    PLUGIN = "/plugin/remoteslicing"

    def heartbeat(self, info):
        """Send an agent heartbeat."""
        return self.post_json(f"{self.PLUGIN}/agent/heartbeat", info)

    def claim(self):
        """Claim the next task.

        Returns the ``"task"`` value from the response (dict or ``None``).
        """
        resp = self.post_json(f"{self.PLUGIN}/agent/claim", {})
        return resp.get("task") if resp else None

    def progress(self, task_id, progress, stage):
        """Report task progress."""
        return self.post_json(
            f"{self.PLUGIN}/agent/tasks/{urllib.parse.quote(task_id, safe='')}/progress",
            {"progress": progress, "stage": stage},
        )

    def done(self, task_id, result):
        """Mark a task as done."""
        return self.post_json(
            f"{self.PLUGIN}/agent/tasks/{urllib.parse.quote(task_id, safe='')}/done",
            {"result": result},
        )

    def failed(self, task_id, error):
        """Mark a task as failed."""
        return self.post_json(
            f"{self.PLUGIN}/agent/tasks/{urllib.parse.quote(task_id, safe='')}/failed",
            {"error": error},
        )

    def listing(self, request_id, data):
        """Send a listing update."""
        payload = {"request_id": request_id}
        payload.update(data)
        return self.post_json(f"{self.PLUGIN}/agent/listing", payload)

    def download_model(self, model_id, dest):
        """Download a model file."""
        return self.download(
            f"{self.PLUGIN}/agent/models/{urllib.parse.quote(model_id, safe='')}",
            dest,
        )

    def download_source(self, source_id, dest):
        """Download a source file."""
        return self.download(
            f"{self.PLUGIN}/agent/sources/{urllib.parse.quote(source_id, safe='')}",
            dest,
        )

    def upload_model(self, task_id, file_path, name):
        """Upload a model file for a task."""
        return self.upload_file(
            f"{self.PLUGIN}/agent/tasks/{urllib.parse.quote(task_id, safe='')}/model",
            "file",
            file_path,
            name,
            fields={"name": name},
        )


# ---------------------------------------------------------------------------
# App-key request flow
# ---------------------------------------------------------------------------

def request_app_key(
    url,
    app_name="Remote Slicing Agent",
    poll=1.0,
    timeout=300,
    sleep=time.sleep,
    on_pending=None,
    clock=time.monotonic,
):
    """Request an app key from OctoPrint.

    Parameters
    ----------
    url : str
        OctoPrint base URL.
    app_name : str
        Application name shown in the approval dialog.
    poll : float
        Seconds between polling attempts.
    timeout : float
        Total time to wait for approval.
    sleep : callable
        Sleep function (injectable for testing).
    on_pending : callable or None
        Called once with the auth-dialog URL while waiting.
    clock : callable
        Monotonic clock (injectable for testing).

    Returns
    -------
    str
        The API key.

    Raises
    ------
    ApiError
    """
    client = Client(url, api_key="")

    # Step 1: request
    _status, data = client.request(
        "POST",
        "/plugin/appkeys/request",
        body=json.dumps({"app": app_name}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    resp = json.loads(data)
    app_token = resp["app_token"]
    auth_dialog = resp["auth_dialog"]

    if on_pending:
        on_pending(auth_dialog)

    # Step 2: poll
    deadline = clock() + timeout
    while clock() < deadline:
        sleep(poll)
        try:
            _status, data = client.request(
                "GET",
                f"/plugin/appkeys/request/{app_token}",
            )
        except ApiError as exc:
            if exc.status == 404:
                raise
            break
        if _status == 202:
            continue
        if _status == 200:
            return json.loads(data)["api_key"]
        # Unexpected status -- keep waiting
    raise ApiError(status=408, body="no decision")
