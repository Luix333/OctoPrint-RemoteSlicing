"""Tests for remote_slicing_agent.push."""

import json
import socket
import unittest
from unittest import mock

import websocket

from remote_slicing_agent import main
from remote_slicing_agent.push import PushSocket, parse_frame


# ---------------------------------------------------------------------------
# parse_frame
# ---------------------------------------------------------------------------

class TestParseFrame(unittest.TestCase):
    """parse_frame extracts plugin data correctly."""

    PLUGIN = "remoteslicing"

    def test_our_plugin_message(self):
        """A frame for our plugin returns (kind, data)."""
        text = json.dumps({
            "plugin": {
                "plugin": self.PLUGIN,
                "data": {"action": "heartbeat", "value": 42},
            },
        })
        kind, data = parse_frame(text, self.PLUGIN)
        self.assertEqual(kind, "plugin")
        self.assertEqual(data["action"], "heartbeat")
        self.assertEqual(data["value"], 42)

    def test_another_plugin_message(self):
        """A frame for another plugin returns data=None."""
        text = json.dumps({
            "plugin": {
                "plugin": "otherplugin",
                "data": {"foo": "bar"},
            },
        })
        kind, data = parse_frame(text, self.PLUGIN)
        self.assertEqual(kind, "plugin")
        self.assertIsNone(data)

    def test_connected_frame(self):
        """A 'connected' frame returns kind='connected', data=None."""
        text = json.dumps({"connected": True})
        kind, data = parse_frame(text, self.PLUGIN)
        self.assertEqual(kind, "connected")
        self.assertIsNone(data)

    def test_current_frame(self):
        """A 'current' frame returns kind='current', data=None."""
        text = json.dumps({"current": {"temp": 200}})
        kind, data = parse_frame(text, self.PLUGIN)
        self.assertEqual(kind, "current")
        self.assertIsNone(data)

    def test_reauth_required(self):
        """A 'reauthRequired' frame returns kind='reauthRequired'."""
        text = json.dumps({"reauthRequired": True})
        kind, data = parse_frame(text, self.PLUGIN)
        self.assertEqual(kind, "reauthRequired")
        self.assertIsNone(data)

    def test_non_json_text(self):
        """Non-JSON text returns kind='invalid'."""
        kind, data = parse_frame("hello world", self.PLUGIN)
        self.assertEqual(kind, "invalid")
        self.assertIsNone(data)

    def test_json_list(self):
        """A JSON list (not object) returns kind='invalid'."""
        kind, data = parse_frame("[1, 2, 3]", self.PLUGIN)
        self.assertEqual(kind, "invalid")
        self.assertIsNone(data)

    def test_empty_dict(self):
        """An empty JSON dict returns kind='invalid'."""
        kind, data = parse_frame("{}", self.PLUGIN)
        self.assertEqual(kind, "invalid")
        self.assertIsNone(data)

    def test_malformed_json(self):
        """Malformed JSON returns kind='invalid'."""
        kind, data = parse_frame("{invalid json", self.PLUGIN)
        self.assertEqual(kind, "invalid")
        self.assertIsNone(data)


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------

class FakeClient:
    api_key = "key"

    def __init__(self, url="http://printer"):
        self.url = url

    def passive_login(self):
        return {"name": "luis", "session": "s"}

    def heartbeat(self, info):
        return {}

    def claim(self):
        return None


class TestConnectErrors(unittest.TestCase):
    """The websocket library's errors are neither OSError nor ConnectionError, so they must be converted here:
    one escaping during a reconnect ended the agent on 2026-11-01."""

    def assert_connection_error(self, error):
        with mock.patch("websocket.create_connection", side_effect=error):
            with self.assertRaises(ConnectionError):
                PushSocket(FakeClient()).connect()

    def test_handshake_timeout(self):
        self.assert_connection_error(websocket.WebSocketTimeoutException("Connection timed out"))

    def test_handshake_refused_by_a_proxy(self):
        self.assert_connection_error(websocket.WebSocketBadStatusException("Handshake status 503", 503))

    def test_socket_error(self):
        self.assert_connection_error(socket.gaierror("no such host"))

    def test_closed_while_subscribing(self):
        ws = mock.Mock()
        ws.send.side_effect = websocket.WebSocketConnectionClosedException("socket is already closed")
        with mock.patch("websocket.create_connection", return_value=ws):
            with self.assertRaises(ConnectionError):
                PushSocket(FakeClient()).connect()

    def test_agent_survives_an_octoprint_too_busy_to_answer_the_handshake(self):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(5)  # accepts the TCP connection in the backlog and never answers
        fake = FakeClient("http://127.0.0.1:%d" % server.getsockname()[1])
        sleeps = []
        agent = main.Agent({"octoprint_url": fake.url, "api_key": "key"}, client_obj=fake, sleep=sleeps.append,
                           push_factory=lambda: PushSocket(fake, connect_timeout=0.2))
        with self.assertLogs("remote_slicing_agent", "WARNING") as logs:
            agent.run_forever(max_rounds=2)
        self.assertEqual(sleeps, [5, 15])
        self.assertEqual(len(logs.records), 1)
        self.assertIn("lost connection to OctoPrint", logs.output[0])


if __name__ == "__main__":
    unittest.main()
