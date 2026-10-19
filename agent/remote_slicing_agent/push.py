"""Push-socket wrapper for OctoPrint's raw websocket.

Uses ``websocket-client`` to maintain a single connection subscribed
only to this plugin's messages.
"""

import collections
import json

import websocket


# ---------------------------------------------------------------------------
# Frame parser (pure function -- unit-testable)
# ---------------------------------------------------------------------------

def parse_frame(text, plugin):
    """Parse a single websocket frame.

    Parameters
    ----------
    text : str
        Raw frame payload (expected to be a JSON object).
    plugin : str
        The plugin name we are interested in.

    Returns
    -------
    tuple[str, dict or None]
        ``(kind, data)`` where *kind* is the top-level key of the frame
        (``"plugin"``, ``"connected"``, ``"reauthRequired"``, ``"current"``,
        or ``"invalid"``) and *data* is the inner plugin data dict only
        when the frame belongs to our plugin.
    """
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ("invalid", None)

    if not isinstance(obj, dict):
        return ("invalid", None)

    kind = list(obj.keys())[0] if obj else "invalid"

    if kind == "plugin" and isinstance(obj.get("plugin"), dict):
        if obj["plugin"].get("plugin") == plugin:
            return (kind, obj["plugin"].get("data"))

    return (kind, None)


# ---------------------------------------------------------------------------
# PushSocket
# ---------------------------------------------------------------------------

class PushSocket:
    """WebSocket client for OctoPrint's push socket.

    Authenticates via passive login and subscribes to a single plugin.
    """

    def __init__(self, client, plugin="remoteslicing", connect_timeout=15):
        self.client = client
        self.plugin = plugin
        self.connect_timeout = connect_timeout
        self._ws = None
        self.frame_counts = collections.Counter()

    @property
    def connected(self):
        """``True`` when the websocket is open."""
        return self._ws is not None

    # -- connection ----------------------------------------------------------

    def connect(self):
        """Open and authenticate the websocket."""
        login = self.client.passive_login()
        auth = f"{login['name']}:{login['session']}"

        # Build WS URL from the client's base URL
        base = self.client.url
        if base.startswith("https://"):
            ws_url = base.replace("https://", "wss://", 1) + "/sockjs/websocket"
        else:
            ws_url = base.replace("http://", "ws://", 1) + "/sockjs/websocket"

        self._ws = websocket.create_connection(
            ws_url,
            timeout=self.connect_timeout,
            header=["X-Api-Key: " + self.client.api_key],
        )

        # Subscribe to our plugin only
        self._ws.send(
            json.dumps({
                "subscribe": {
                    "state": False,
                    "plugins": [self.plugin],
                    "events": False,
                },
            })
        )

        # Authenticate after subscribing: OctoPrint sends the (large) state history at auth time
        # unless the client has already said it doesn't want state.
        self._ws.send(json.dumps({"auth": auth}))

    # -- message reading -----------------------------------------------------

    def next_message(self, timeout):
        """Receive one message for our plugin.

        Parameters
        ----------
        timeout : float
            Seconds to wait.

        Returns
        -------
        dict or None
            The plugin data dict, or ``None`` on timeout / unrelated frame.

        Raises
        ------
        ConnectionError
            When the socket is closed or an error occurs.
        """
        self._ws.settimeout(timeout)
        try:
            raw = self._ws.recv()
        except websocket.WebSocketTimeoutException:
            return None
        except websocket.WebSocketConnectionClosedException as exc:
            raise ConnectionError("connection closed") from exc
        except (websocket.WebSocketException, OSError) as exc:
            raise ConnectionError(str(exc)) from exc

        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw

        kind, data = parse_frame(text, self.plugin)
        self.frame_counts[kind] += 1

        if kind == "reauthRequired":
            raise ConnectionError("reauth required")

        if kind == "plugin" and data is not None:
            return data

        return None

    # -- helpers -------------------------------------------------------------

    def ping(self):
        """Send a websocket ping.

        Raises
        ------
        ConnectionError
        """
        try:
            self._ws.ping()
        except (websocket.WebSocketException, OSError) as exc:
            raise ConnectionError(str(exc))

    def close(self):
        """Close the websocket quietly."""
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        finally:
            self._ws = None
