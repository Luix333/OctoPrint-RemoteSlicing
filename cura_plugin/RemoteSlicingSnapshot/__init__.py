# Copyright (c) 2026 Luix333
# Released under the terms of the AGPLv3 or higher.


def getMetaData():
    return {}


def register(app):
    from .SnapshotExporter import SnapshotExporter
    return {"extension": SnapshotExporter()}
