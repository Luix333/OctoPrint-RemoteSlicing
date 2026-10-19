"""Tests for remote_slicing_agent.push."""

import json
import unittest

from remote_slicing_agent.push import parse_frame


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


if __name__ == "__main__":
    unittest.main()
