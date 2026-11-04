# Copyright (c) 2026 Luix333
# Released under the terms of the AGPLv3 or higher.

"""Lets one thread read Cura's settings as if another profile were active, while Cura carries on undisturbed.

Formulas can only be evaluated through the active printer's real stacks: extruderValue(), StartSliceJob and
ExtruderManager all go straight to them. Really switching the profile would re-slice, redraw the UI and could feed
the wrong settings to a slice running on another thread. So during ``evaluation()``:

- ``ContainerStack._containers`` becomes a descriptor, and the calling thread alone gets other containers from it.
  Every other thread reads the stack's own list, as usual. Nothing is written to any stack, so no signal fires.
- ``CachedMemberFunctions`` skips Uranium's per-instance caches on that thread. Stacks cache getProperty() across
  threads, so the two views would otherwise leak into each other, in both directions.
"""

import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List

from UM.Settings.ContainerStack import ContainerStack

try:
    from UM.Decorators import CachedMemberFunctions
except ImportError:  # Cura before 5.9 has no such cache
    CachedMemberFunctions = None  # type: ignore

_thread = threading.local()


class _ThreadContainers:
    """A data descriptor, so it wins over the ``_containers`` entry in the stack's ``__dict__``."""

    def __get__(self, stack: Any, owner: Any = None) -> Any:
        if stack is None:
            return self
        shown = getattr(_thread, "containers", None)
        if shown is not None and id(stack) in shown:
            return shown[id(stack)]
        return stack.__dict__["_containers"]

    def __set__(self, stack: Any, value: List[Any]) -> None:
        stack.__dict__["_containers"] = value


@contextmanager
def evaluation() -> Iterator[None]:
    """While this is open the calling thread can ``show()`` profiles. Only one may be open at a time."""
    cached_call = None
    if CachedMemberFunctions is not None:
        cached_call = CachedMemberFunctions.__dict__["callMemberFunction"]
        original = CachedMemberFunctions.callMemberFunction

        def callMemberFunction(cls: Any, instance: Any, function: Any, *args: Any, **kwargs: Any) -> Any:
            memo = getattr(_thread, "memo", None)
            if memo is None:
                return original(instance, function, *args, **kwargs)
            if kwargs:  # the real cache doesn't take these either
                return function(instance, *args, **kwargs)
            key = (id(instance), function, args)
            if key not in memo:
                memo[key] = function(instance, *args)
            return memo[key]

        CachedMemberFunctions.callMemberFunction = classmethod(callMemberFunction)
    ContainerStack._containers = _ThreadContainers()
    try:
        yield
    finally:
        del ContainerStack._containers
        if cached_call is not None:
            CachedMemberFunctions.callMemberFunction = cached_call
        _thread.containers = None
        _thread.memo = None


def show(containers: Dict[Any, List[Any]]) -> None:
    """From now on the calling thread sees these containers in these stacks, with nothing remembered from before."""
    _thread.containers = {id(stack): list(stack_containers) for stack, stack_containers in containers.items()}
    _thread.memo = {}
