"""Capability registry.

A capability is a subpackage of `chatbot.capabilities` that exposes `CAPABILITY`: a name, a
one-line description, its own `instructions.md`, and a `register(agent)` that adds its tools.
Adding one = adding a folder; nothing else changes.

Loading policy (D24): every capability's tools and instructions are loaded into the chat agent
upfront, and their token cost is measurable (`chatbot capabilities`) so on-demand loading can
be introduced when the set grows.
"""

import importlib
import json
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.tools import Tool

import chatbot.capabilities as package
from chatbot.config import get_settings


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    tools: tuple[Callable, ...]
    instructions_path: Path

    @property
    def instructions(self) -> str:
        return self.instructions_path.read_text(encoding="utf-8").strip()

    def register(self, agent: Agent) -> None:
        for fn in self.tools:
            agent.tool(fn)


def capability(name: str, description: str, module_file: str, *tools: Callable) -> Capability:
    """Declare a capability from inside its package `__init__.py`."""
    return Capability(name, description, tools, Path(module_file).parent / "instructions.md")


@cache
def all_capabilities() -> tuple[Capability, ...]:
    """Every capability subpackage, sorted by name (stable prompt order => cacheable)."""
    found = []
    for info in pkgutil.iter_modules(package.__path__):
        if not info.ispkg:
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        if isinstance(cap := getattr(module, "CAPABILITY", None), Capability):
            found.append(cap)
    return tuple(sorted(found, key=lambda c: c.name))


def register_all(agent: Agent) -> None:
    for cap in all_capabilities():
        cap.register(agent)


def instructions_block() -> str:
    parts = [f"### {c.name}\n{c.instructions}" for c in all_capabilities()]
    return "## Capabilities\n\n" + "\n\n".join(parts) if parts else ""


def cost_report() -> list[dict]:
    """Estimated always-loaded tokens per capability: instructions + tool schemas."""
    cpt = get_settings().estimate_chars_per_token
    rows = []
    for cap in all_capabilities():
        schema_chars = 0
        for fn in cap.tools:
            td = Tool(fn).tool_def
            schema_chars += len(
                json.dumps({"n": td.name, "d": td.description, "p": td.parameters_json_schema})
            )
        rows.append(
            {
                "name": cap.name,
                "instructions_tokens": int(len(cap.instructions) / cpt),
                "schema_tokens": int(schema_chars / cpt),
            }
        )
    return rows
