# Copyright (c) 2026 Luix333
# Released under the terms of the AGPLv3 or higher.

"""A stand-in for the Arcus slice message that StartSliceJob fills in.

StartSliceJob only ever calls addRepeatedMessage(), getMessage() and sets plain attributes
(name, value, id, extruder, ...), so recording those is enough to capture exactly what Cura
would send to CuraEngine.
"""

from typing import Any, Dict, List


class Recorder:
    def __init__(self) -> None:
        self._repeated: Dict[str, List["Recorder"]] = {}
        self._messages: Dict[str, "Recorder"] = {}

    def addRepeatedMessage(self, name: str) -> "Recorder":
        child = Recorder()
        self._repeated.setdefault(name, []).append(child)
        return child

    def getMessage(self, name: str) -> "Recorder":
        return self._messages.setdefault(name, Recorder())

    def repeated(self, name: str) -> List["Recorder"]:
        return self._repeated.get(name, [])


def _value(setting: Recorder) -> str:
    value = getattr(setting, "value", b"")
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _settings(message: Recorder) -> Dict[str, str]:
    return {setting.name: _value(setting) for setting in message.repeated("settings")}


def toResolvedJson(root: Recorder) -> Dict[str, Any]:
    """Convert a recorded slice message to CuraEngine's `-r` resolved-settings format:
    {"global": {...}, "extruder.<n>": {...}, "limit_to_extruder": {key: "extruder.<n>"}}."""
    resolved: Dict[str, Any] = {"global": _settings(root.getMessage("global_settings"))}
    for extruder in root.repeated("extruders"):
        resolved["extruder.%d" % int(extruder.id)] = _settings(extruder.getMessage("settings"))
    resolved["limit_to_extruder"] = {
        entry.name: "extruder.%d" % int(entry.extruder) for entry in root.repeated("limit_to_extruder")
    }
    return resolved
