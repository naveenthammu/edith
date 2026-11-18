import inspect
import typing
from dataclasses import dataclass
from typing import Callable

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


@dataclass
class Skill:
    func: Callable[..., str]
    confirm: bool
    schema: dict


REGISTRY: dict[str, Skill] = {}


def _schema(func: Callable) -> dict:
    hints = typing.get_type_hints(func)
    properties, required = {}, []
    for name, param in inspect.signature(func).parameters.items():
        properties[name] = {"type": _JSON_TYPES[hints.get(name, str)]}
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": inspect.getdoc(func) or "",
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def skill(confirm: bool = False):
    """Register a function as a skill the LLM can call."""

    def wrap(func):
        REGISTRY[func.__name__] = Skill(func, confirm, _schema(func))
        return func

    return wrap


def tool_schemas() -> list[dict]:
    return [s.schema for s in REGISTRY.values()]


def load_all() -> None:
    # Importing each module runs its @skill decorators.
    from skills import (assistant, computer, google_apps, info, media, memory, naukri,  # noqa: F401
                        notifications, productivity, screen, system, teach, telegram, web)
