"""Tests for remote_slicing_agent.client."""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO

from remote_slicing_agent.client import ApiError, Client, request_app_key


# ---------------------------------------------------------------------------
# Test server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Records requests and serves canned responses."""

    def log_message(self, format, *args):
        pass  # silence

    def do_GET(self):
        self.server.requests.append(("GET", self.headers, self.rfile))
        self._respond(*self.server._canned_get.get(self.path, (200, b"")))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.server.requests.append(("POST", self.headers, body))
        self._respond(*self.server._canned_post.get(self.path, (200, b"")))

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def _find_free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(canned_get, canned_post):
    """Start a threaded HTTP server in a background thread."""
    port = _find_free_port()
    server = HTTPServer(("127.0.0.1", port), _Handler)
    server.requests = []
    server._canned_get = canned_get
    server._canned_post = canned_post
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------

class TestHeaders(unittest.TestCase):
    """Client sends the expected headers."""

    def setUp(self):
        self.canned_post = {
            "/api/test": (200, json.dumps({"ok": True}).encode()),
        }
        self.server, self.port = _serve({}, self.canned_post)
        self.client = Client(
            f"http://127.0.0.1:{self.port}",
            api_key="secret",
        )

    def tearDown(self):
        self.server.shutdown()

    def test_x_api_key_sent(self):
        self.client.post_json("/api/test", {})
        req = self.server.requests[-1]
        self.assertEqual(req[1]["X-Api-Key"], "secret")

    def test_user_agent_sent(self):
        self.client.post_json("/api/test", {})
        req = self.server.requests[-1]
        self.assertTrue(req[1]["User-Agent"].startswith("RemoteSlicingAgent/"))


# ---------------------------------------------------------------------------
# get_json / post_json
# ---------------------------------------------------------------------------

class TestJsonHelpers(unittest.TestCase):

    def setUp(self):
        self.canned_get = {
            "/api/hello": (200, json.dumps({"msg": "hi"}).encode()),
            "/api/empty": (204, b""),
        }
        self.canned_post = {
            "/api/echo": (200, json.dumps({"echoed": True}).encode()),
            "/api/no-content": (204, b""),
        }
        self.server, self.port = _serve(self.canned_get, self.canned_post)
        self.client = Client(
            f"http://127.0.0.1:{self.port}",
            api_key="",
        )

    def tearDown(self):
        self.server.shutdown()

    def test_get_json(self):
        result = self.client.get_json("/api/hello")
        self.assertEqual(result["msg"], "hi")

    def test_get_json_204_returns_none(self):
        self.assertIsNone(self.client.get_json("/api/empty"))

    def test_post_json(self):
        result = self.client.post_json("/api/echo", {"x": 1})
        self.assertTrue(result["echoed"])

    def test_post_json_204_returns_none(self):
        self.assertIsNone(self.client.post_json("/api/no-content", {}))

    def test_http_409_raises_apierror(self):
        self.canned_post["/api/conflict"] = (409, b"conflict body")
        with self.assertRaises(ApiError) as ctx:
            self.client.post_json("/api/conflict", {})
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("conflict body", ctx.exception.body)


# ---------------------------------------------------------------------------
# Unreachable host
# ---------------------------------------------------------------------------

class TestUnreachable(unittest.TestCase):

    def test_unreachable_port(self):
        """Connection to a closed port raises ApiError(status=0)."""
        # Use a high port that is almost certainly closed
        client = Client("http://127.0.0.1:59999", api_key="", timeout=1)
        with self.assertRaises(ApiError) as ctx:
            client.get_json("/api/test")
        self.assertEqual(ctx.exception.status, 0)


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

class TestDownload(unittest.TestCase):

    def setUp(self):
        payload = b"hello world download"
        self.canned_get = {
            "/api/file.bin": (200, payload),
            "/api/missing": (404, b"not found"),
        }
        self.canned_post = {}
        self.server, self.port = _serve(self.canned_get, self.canned_post)
        self.client = Client(
            f"http://127.0.0.1:{self.port}",
            api_key="",
        )
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def test_download_writes_file(self):
        dest = os.path.join(self.tmp.name, "out.bin")
        n = self.client.download("/api/file.bin", dest)
        self.assertEqual(n, len(b"hello world download"))
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"hello world download")
        # No .part left behind
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_download_no_part_on_error(self):
        dest = os.path.join(self.tmp.name, "fail.bin")
        # Point to a path that will 404
        with self.assertRaises(ApiError):
            self.client.download("/api/missing", dest)
        self.assertFalse(os.path.exists(dest))
        self.assertFalse(os.path.exists(dest + ".part"))


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------

class TestUploadFile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Server echoes back the parsed multipart info
        def make_handler():
            class H(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    pass

                def do_POST(self):
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length)
                    self.server.uploaded_body = body
                    self.server.uploaded_headers = dict(self.headers)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": True}).encode())

            return H

        port = _find_free_port()
        server = HTTPServer(("127.0.0.1", port), make_handler())
        server.requests = []
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.server = server
        self.port = port

        self.client = Client(
            f"http://127.0.0.1:{self.port}",
            api_key="key1",
        )

        # Create a 3 MiB test file
        self.file_path = os.path.join(self.tmp.name, "big.bin")
        with open(self.file_path, "wb") as f:
            chunk = b"A" * (1 << 20)
            f.write(chunk)
            f.write(chunk)
            f.write(chunk)

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def test_upload_file_sends_body(self):
        result = self.client.upload_file(
            "/api/upload",
            "file",
            self.file_path,
            "big.bin",
            fields={"path": "/test", "select": "false"},
        )
        self.assertTrue(result["ok"])
        body = self.server.uploaded_body
        # Should contain the file data
        self.assertIn(b"AAA", body)
        # Should contain the extra fields
        self.assertIn(b'path', body)
        self.assertIn(b'/test', body)

    def test_upload_file_filename_sanitized(self):
        """Quotes and backslashes removed from filename."""
        self.client.upload_file(
            "/api/upload",
            "file",
            self.file_path,
            'weird"file\\name',
            fields=None,
        )
        body = self.server.uploaded_body
        # The sanitized filename (quotes and backslashes removed) should appear
        self.assertIn(b'filename="weirdfilename"', body)
        # Backslashes should not appear in the body at all
        self.assertNotIn(b"\\", body)


# ---------------------------------------------------------------------------
# upload_gcode
# ---------------------------------------------------------------------------

class TestUploadGcode(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        gcode_path = os.path.join(self.tmp.name, "test.gcode")
        with open(gcode_path, "w") as f:
            f.write("G28\nG1 X10\n")

        def make_handler():
            class H(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    pass

                def do_POST(self):
                    length = int(self.headers.get("Content-Length", 0))
                    _ = self.rfile.read(length) if length else None
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    resp = {
                        "files": {
                            "local": {
                                "path": "RemoteSlicing/test.gcode",
                            },
                        },
                    }
                    self.wfile.write(json.dumps(resp).encode())

            return H

        port = _find_free_port()
        server = HTTPServer(("127.0.0.1", port), make_handler())
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.server = server

        self.client = Client(
            f"http://127.0.0.1:{port}",
            api_key="key1",
        )
        self.gcode_path = gcode_path

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def test_upload_gcode_returns_path(self):
        path = self.client.upload_gcode(self.gcode_path, "test.gcode")
        self.assertEqual(path, "RemoteSlicing/test.gcode")


# ---------------------------------------------------------------------------
# request_app_key
# ---------------------------------------------------------------------------

class TestRequestAppKey(unittest.TestCase):

    def test_success_201_202_202_200(self):
        """Server answers 201, then 202, 202, 200 -> returns the key."""
        call_count = [0]

        def make_handler():
            class H(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    pass

                def do_POST(self):
                    length = int(self.headers.get("Content-Length", 0))
                    _ = self.rfile.read(length) if length else None
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    resp = {
                        "app_token": "tok123",
                        "auth_dialog": "http://localhost/dialog",
                    }
                    self.wfile.write(json.dumps(resp).encode())

                def do_GET(self):
                    call_count[0] += 1
                    self.send_response(200 if call_count[0] >= 3 else 202)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    if call_count[0] >= 3:
                        self.wfile.write(
                            json.dumps({"api_key": "newkey123"}).encode()
                        )
                    else:
                        self.wfile.write(b"")

            return H

        port = _find_free_port()
        server = HTTPServer(("127.0.0.1", port), make_handler())
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        result = request_app_key(
            f"http://127.0.0.1:{port}",
            poll=0.05,
            timeout=10,
            sleep=lambda x: None,  # no-op sleep for speed
        )
        self.assertEqual(result, "newkey123")
        server.shutdown()

    def test_404_access_denied(self):
        """Server responds 404 -> ApiError(404)."""
        def make_handler():
            class H(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    pass

                def do_POST(self):
                    length = int(self.headers.get("Content-Length", 0))
                    _ = self.rfile.read(length) if length else None
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        json.dumps({
                            "app_token": "tok404",
                            "auth_dialog": "http://localhost/dialog",
                        }).encode()
                    )

                def do_GET(self):
                    self.send_response(404)
                    self.end_headers()

            return H

        port = _find_free_port()
        server = HTTPServer(("127.0.0.1", port), make_handler())
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        with self.assertRaises(ApiError) as ctx:
            request_app_key(
                f"http://127.0.0.1:{port}",
                poll=0.05,
                timeout=5,
                sleep=lambda x: None,
            )
        self.assertEqual(ctx.exception.status, 404)
        server.shutdown()

    def test_timeout_apierror_408(self):
        """Server never responds 200 -> ApiError(408)."""
        fake_time = [0.0]

        def fake_sleep(x):
            fake_time[0] += x

        def fake_clock():
            return fake_time[0]

        def make_handler():
            class H(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    pass

                def do_POST(self):
                    length = int(self.headers.get("Content-Length", 0))
                    _ = self.rfile.read(length) if length else None
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        json.dumps({
                            "app_token": "tok408",
                            "auth_dialog": "http://localhost/dialog",
                        }).encode()
                    )

                def do_GET(self):
                    self.send_response(202)
                    self.end_headers()

            return H

        port = _find_free_port()
        server = HTTPServer(("127.0.0.1", port), make_handler())
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        with self.assertRaises(ApiError) as ctx:
            request_app_key(
                f"http://127.0.0.1:{port}",
                poll=1.0,
                timeout=5,
                sleep=fake_sleep,
                clock=fake_clock,
            )
        self.assertEqual(ctx.exception.status, 408)
        server.shutdown()


# ---------------------------------------------------------------------------
# ApiError attributes
# ---------------------------------------------------------------------------

class TestApiErrorAttrs(unittest.TestCase):

    def test_status_and_body(self):
        e = ApiError(500, "internal")
        self.assertEqual(e.status, 500)
        self.assertEqual(e.body, "internal")

    def test_body_truncated(self):
        e = ApiError(500, "x" * 600)
        self.assertEqual(len(e.body), 500)

    def test_network_error_status_zero(self):
        e = ApiError(0, "connection refused")
        self.assertEqual(e.status, 0)


if __name__ == "__main__":
    unittest.main()
