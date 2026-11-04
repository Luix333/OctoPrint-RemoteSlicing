# Copyright (c) 2026 Luix333
# Released under the terms of the AGPLv3 or higher.

"""The profiles Cura offers for the active printer, each as the containers its stacks would hold when active.

These follow MachineManager._setQualityGroup, _setQualityChangesGroup and setIntentByCategory, which do the same on
the real stacks. A profile here is what is saved in it: unsaved changes in the sidebar are not part of any of them.
"""

import hashlib
from typing import Any, Dict, List

from UM.Settings.ContainerRegistry import ContainerRegistry

from cura.Machines.ContainerTree import ContainerTree
from cura.Settings.CuraContainerStack import _ContainerIndexes
from cura.Settings.IntentManager import IntentManager
from cura.Settings.cura_empty_instance_containers import (empty_container, empty_intent_container,
                                                          empty_quality_changes_container, empty_quality_container)


def _intentFor(global_stack, extruder, quality, category: str):
    if quality.getId() == empty_quality_container.getId():
        return empty_intent_container
    try:
        machine = ContainerTree.getInstance().machines[global_stack.definition.getId()]
        material = machine.variants[extruder.variant.getName()].materials[extruder.material.getMetaDataEntry("base_file")]
        quality_node = material.qualities[quality.getId()]
    except KeyError:  # not in the tree: Cura then leaves the extruder's intent as it is
        return extruder.intent
    for intent_node in quality_node.intents.values():
        if intent_node.intent_category == category:
            return intent_node.container
    return empty_intent_container


def _containers(stack, quality_changes, intent, quality) -> List[Any]:
    containers = list(stack.getContainers())  # that list is Cura's own cached one
    containers[_ContainerIndexes.UserChanges] = empty_container
    containers[_ContainerIndexes.QualityChanges] = quality_changes
    containers[_ContainerIndexes.Intent] = intent
    containers[_ContainerIndexes.Quality] = quality
    return containers


def _qualityProfile(global_stack, group, category: str) -> Dict[str, Any]:
    stacks = {global_stack: _containers(global_stack, empty_quality_changes_container, global_stack.intent,
                                        group.node_for_global.container)}
    for position, extruder in enumerate(global_stack.extruderList):
        node = group.nodes_for_extruders.get(position)
        quality = node.container if node else extruder.quality  # a disabled extruder keeps what it has
        stacks[extruder] = _containers(extruder, empty_quality_changes_container,
                                       _intentFor(global_stack, extruder, quality, category), quality)
    return {
        "id": "quality/%s/%s" % (group.quality_type, category),
        "name": group.name if category == "default" else "%s %s" % (category.capitalize(), group.name),
        "group": "quality",
        "quality_type": group.quality_type,
        "intent": category,
        "containers": stacks,
    }


def _customProfile(global_stack, changes_group, quality_groups: Dict[str, Any]) -> Dict[str, Any]:
    registry = ContainerRegistry.getInstance()

    def changesContainer(metadata):
        found = registry.findContainers(id = metadata["id"]) if metadata else []
        return found[0] if found else empty_quality_changes_container

    group = quality_groups.get(changes_group.quality_type)  # "not_supported" has none, and uses empty quality
    node = group.node_for_global if group else None
    stacks = {global_stack: _containers(global_stack, changesContainer(changes_group.metadata_for_global), global_stack.intent,
                                        node.container if node else empty_quality_container)}
    for position, extruder in enumerate(global_stack.extruderList):
        node = group.nodes_for_extruders.get(position) if group else None
        quality = node.container if node else empty_quality_container
        stacks[extruder] = _containers(extruder, changesContainer(changes_group.metadata_per_extruder.get(position)),
                                       _intentFor(global_stack, extruder, quality, changes_group.intent_category), quality)
    return {
        "id": "custom/" + changes_group.name,
        "name": changes_group.name,
        "group": "custom",
        "quality_type": changes_group.quality_type,
        "intent": changes_group.intent_category,
        "containers": stacks,
    }


def listProfiles(global_stack) -> List[Dict[str, Any]]:
    """What the profile menu offers, leaving out what it greys out. Must run on the GUI thread: this can load the
    containers from disk, and fills them in so the export thread doesn't have to."""
    tree = ContainerTree.getInstance()
    groups = tree.getCurrentQualityGroups()
    available = {quality_type: group for quality_type, group in groups.items()
                 if group.is_available and group.node_for_global is not None and group.node_for_global.container}
    # Sorted: this order varies from one Cura start to the next, and the fingerprint must not.
    profiles = [_qualityProfile(global_stack, available[quality_type], "default") for quality_type in sorted(available)]
    for category, quality_type in sorted(IntentManager.getInstance().getCurrentAvailableIntents()):
        if category != "default" and quality_type in available:
            profiles.append(_qualityProfile(global_stack, available[quality_type], category))
    for changes_group in sorted(tree.getCurrentQualityChangesGroups(), key = lambda group: group.name.lower()):
        if changes_group.is_available:
            profiles.append(_customProfile(global_stack, changes_group, available))
    for profile in profiles:
        for containers in profile["containers"].values():
            for container in containers:
                container.getAllKeys()
    return profiles


def _describe(container) -> str:
    # Value-less keys are settings Cura no longer has: they drop out of getAllKeys() once a container is fully loaded.
    values = [(key, str(value)) for key, value in ((key, container.getProperty(key, "value"))
                                                   for key in sorted(container.getAllKeys())) if value is not None]
    metadata = sorted((key, str(value)) for key, value in container.getMetaData().items())
    return repr((container.getId(), metadata, values))


def fingerprint(version: str, global_stack, profiles: List[Dict[str, Any]]) -> str:
    """Changes when any exported profile would. Unsaved changes in the sidebar don't count, so ordinary editing in
    Cura doesn't make every profile export again."""
    digest = hashlib.sha1()
    digest.update(version.encode("utf-8"))
    digest.update(global_stack.getId().encode("utf-8"))
    for stack in [global_stack] + list(global_stack.extruderList):
        digest.update(repr(bool(stack.isEnabled) if stack is not global_stack else True).encode("utf-8"))
        for container in (stack.material, stack.variant, stack.definitionChanges):
            digest.update(_describe(container).encode("utf-8"))
    seen = set()
    for profile in profiles:
        digest.update(profile["id"].encode("utf-8"))
        for containers in profile["containers"].values():
            for slot in (_ContainerIndexes.QualityChanges, _ContainerIndexes.Intent, _ContainerIndexes.Quality):
                container = containers[slot]
                digest.update(container.getId().encode("utf-8"))
                if container.getId() not in seen:
                    seen.add(container.getId())
                    digest.update(_describe(container).encode("utf-8"))
    return digest.hexdigest()
