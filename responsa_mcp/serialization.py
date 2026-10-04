"""Turns a responsa_api result (a dataclass, possibly nesting other
dataclasses, lists, and BrowseError enum values) into plain, JSON-safe
Python for an MCP tool's return value.

Deliberately NOT plain dataclasses.asdict(): its recursion only
special-cases dataclass instances, list/tuple, and dict -- any other
field value (including an Enum member) goes through copy.deepcopy()
unchanged, and deep-copying an Enum member returns the same canonical
singleton. So BrowseResult.error would still be a BrowseError *instance*
after asdict(), not a plain str. Because BrowseError(str, Enum) also
subclasses str, a subsequent plain json.dumps() happens to render it
correctly anyway (its isinstance(o, str) check is satisfied) -- but
that's incidental to json.dumps's own behavior, not something to rely on
for whatever the mcp SDK does internally to turn a tool's return value
into wire content. to_jsonable() converts every Enum to .value
explicitly, so the dict handed to the SDK is unambiguously built only
from dict/list/str/int/float/bool/None.
"""
import dataclasses
from enum import Enum
from typing import Any


def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    return obj
