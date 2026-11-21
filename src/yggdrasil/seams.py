"""Helpers for the A11 extension points.

An extension point is a named slot in an ordered table, in the module that runs it. A lane fills its own slot
with a `"module:function"` reference to a function in a file it owns, so two lanes never edit the same
function. An empty slot (None) does nothing.
"""

from __future__ import annotations

import importlib

from . import util


def resolve(ref):
    """The callable a slot names: a callable, a "module:function" reference inside this package, or None."""
    if ref is None or callable(ref):
        return ref
    module, _, name = str(ref).partition(":")
    return getattr(importlib.import_module("yggdrasil." + module), name)


def run_steps(steps, *args, **kwargs) -> list:
    """Run each filled slot in order; a step's refusal (YggError) doesn't stop the steps after it."""
    results = []
    for _, ref in steps:
        function = resolve(ref)
        if function is None:
            continue
        try:
            results.append(function(*args, **kwargs))
        except util.YggError:
            pass
    return results


def not_built(verb: str) -> util.YggError:
    """The refusal of an A11 verb whose lane hasn't filled it in yet (exit 3)."""
    return util.YggError(util.EXIT_REFUSED, "ygg " + verb + " is not built yet")
