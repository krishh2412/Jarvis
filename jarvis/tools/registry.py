"""Tool registry: turns plain Python functions into LLM-callable tools.

Decorate a function with ``@tool``, give it type hints and a Google-style
docstring, and it becomes available to the model with a generated JSON schema.
Execution is wrapped with auditing and error containment so a raising tool
returns a message the model can reason about instead of killing the turn.
"""

from __future__ import annotations

import inspect
import re
import types
import typing
from dataclasses import dataclass
from typing import Any, Callable


def config_user_title() -> str:
    from jarvis.config import config
    return config.user_title

from jarvis.safety import audit

_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]
    destructive: bool
    category: str

    def schema(self) -> dict[str, Any]:
        """Ollama / OpenAI-compatible function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


REGISTRY: dict[str, Tool] = {}


def _parse_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """Split a docstring into a summary and per-argument descriptions."""
    if not doc:
        return "", {}

    lines = inspect.cleandoc(doc).splitlines()
    summary_lines: list[str] = []
    arg_docs: dict[str, str] = {}
    in_args = False
    current: str | None = None

    for line in lines:
        stripped = line.strip()
        if re.match(r"^(Args|Arguments|Params|Parameters):$", stripped):
            in_args = True
            continue
        if in_args and re.match(r"^(Returns|Raises|Examples?|Note):", stripped):
            break

        if in_args:
            match = re.match(r"^(\*{0,2}\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)$", stripped)
            if match:
                current = match.group(1).lstrip("*")
                arg_docs[current] = match.group(2).strip()
            elif current and stripped:
                arg_docs[current] += " " + stripped
        else:
            summary_lines.append(stripped)

    summary = " ".join(l for l in summary_lines if l).strip()
    return summary, arg_docs


def _json_type(annotation: Any) -> dict[str, Any]:
    """Map a Python type hint onto a JSON Schema fragment."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {"type": "string"}

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    # Optional[X] / X | None -> schema for X (optionality is carried by
    # the required list, not the type). Both spellings produce a different
    # origin: typing.Union for Optional[X], types.UnionType for X | None.
    if origin is typing.Union or origin is types.UnionType:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _json_type(non_none[0])
        return {"type": "string"}

    if origin in (list, tuple, set):
        item = _json_type(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item}

    if origin is dict:
        return {"type": "object"}

    # Literal["a", "b"] becomes an enum, which measurably improves the
    # model's hit rate on constrained arguments.
    if origin is typing.Literal:
        values = list(args)
        kind = _PY_TO_JSON.get(type(values[0]), "string") if values else "string"
        return {"type": kind, "enum": values}

    if isinstance(annotation, type):
        return {"type": _PY_TO_JSON.get(annotation, "string")}

    return {"type": "string"}


def tool(
    _fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    destructive: bool = False,
    category: str = "general",
) -> Callable[..., Any]:
    """Register a function as an LLM-callable tool.

    Args:
        name: Override the tool name exposed to the model. Defaults to the
            function name.
        destructive: Marks the tool as one that changes or removes state.
            Flagged in the audit log; filesystem tools additionally route
            through the undo journal.
        category: Grouping label, surfaced in the UI's tool activity panel.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        summary, arg_docs = _parse_docstring(fn.__doc__)
        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn)

        properties: dict[str, Any] = {}
        required: list[str] = []

        for pname, param in sig.parameters.items():
            if pname in ("self", "cls") or param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            schema = _json_type(hints.get(pname, param.annotation))
            if pname in arg_docs:
                schema["description"] = arg_docs[pname]
            if param.default is not inspect.Parameter.empty:
                # Advertising the default keeps the model from inventing one.
                if isinstance(param.default, (str, int, float, bool)):
                    schema["default"] = param.default
            else:
                required.append(pname)
            properties[pname] = schema

        tool_name = name or fn.__name__
        REGISTRY[tool_name] = Tool(
            name=tool_name,
            description=summary or f"Run {tool_name}.",
            parameters={
                "type": "object",
                "properties": properties,
                "required": required,
            },
            fn=fn,
            destructive=destructive,
            category=category,
        )
        return fn

    if _fn is not None:
        return decorate(_fn)
    return decorate


def schemas() -> list[dict[str, Any]]:
    """Every registered tool, in the Ollama/OpenAI function-calling format."""
    return [t.schema() for t in REGISTRY.values()]


def anthropic_schemas() -> list[dict[str, Any]]:
    """Every registered tool in Anthropic's tool format.

    Anthropic wants a flat ``{name, description, input_schema}`` rather than the
    OpenAI-style ``{type: "function", function: {...}}`` the Ollama path uses.
    """
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters,
        }
        for t in REGISTRY.values()
    ]


def execute(name: str, args: dict[str, Any], *, _gated: bool = True) -> Any:
    """Run a tool by name, auditing the call and containing any failure.

    Errors come back as a string rather than an exception: the model can read
    "no such file" and adapt, whereas a traceback would end the turn.

    ``_gated=False`` skips the safety gate entirely. Nothing reachable by the
    model passes it — it exists so tests can exercise a tool in isolation.
    """
    entry = REGISTRY.get(name)
    if entry is None:
        known = ", ".join(sorted(REGISTRY)) or "none"
        msg = f"Unknown tool '{name}'. Available: {known}"
        audit.record(name, args, error=msg)
        return {"error": msg}

    # Drop arguments the model hallucinated; keeping them would TypeError.
    sig = inspect.signature(entry.fn)
    accepted = set(sig.parameters)
    cleaned = {k: v for k, v in args.items() if k in accepted}
    dropped = sorted(set(args) - accepted)

    # Safety gate: block catastrophic calls, and put anything confirmable to a
    # human before it runs. (Imported lazily so the tool layer stays usable
    # without the gate, e.g. in isolated tests.)
    if _gated:
        try:
            from jarvis.safety import gate

            decision, reason = gate.check(name, cleaned)
        except Exception as exc:  # noqa: BLE001
            # The gate itself is broken — a bad regex in a hand-edited
            # config.json, say. Fail CLOSED for anything that changes state:
            # running unchecked is how a machine gets wrecked by a bug in the
            # thing meant to prevent that. Reads still go through, so a broken
            # gate degrades JARVIS to read-only rather than to unrestricted.
            failure = f"safety gate failed: {type(exc).__name__}: {exc}"
            print(f"[safety] {failure}")
            if entry.destructive:
                audit.record(name, cleaned, error=f"refused ({failure})",
                             destructive=True)
                return {
                    "blocked": True,
                    "error": f"{failure} — refusing to run {name} until it is "
                             "fixed. Read-only tools still work.",
                }
            decision, reason = ("allow", "")

        if decision == "block":
            audit.record(name, cleaned, error=f"blocked: {reason}",
                         destructive=entry.destructive)
            return {"blocked": True, "error": reason}

        if decision == "confirm":
            # Blocks this worker thread until a person answers. The event loop
            # is free meanwhile — tools already run via asyncio.to_thread.
            approved, refusal = gate.ask(name, cleaned, reason)
            if not approved:
                audit.record(name, cleaned, error=f"not approved: {refusal}",
                             destructive=entry.destructive)
                return {
                    "denied": True,
                    "error": refusal,
                    "instruction": (
                        f"Tell {config_user_title()} plainly that this was not "
                        "done and why. Do not retry it and do not look for "
                        "another way around it."
                    ),
                }
            audit.record(name, cleaned, result="(approved by user)",
                         destructive=entry.destructive)

    # Log the attempt BEFORE running it, so a crash mid-action still leaves a
    # trace of what was tried (the post-run record adds the result).
    audit.record(name, cleaned, result="(started)",
                 destructive=entry.destructive)

    with audit.Timer() as timer:
        try:
            result = entry.fn(**cleaned)
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            error = f"{type(exc).__name__}: {exc}"
            audit.record(
                name, cleaned, error=error,
                duration_ms=timer.ms, destructive=entry.destructive,
            )
            return {"error": error}

    audit.record(
        name, cleaned, result=result,
        duration_ms=timer.ms, destructive=entry.destructive,
    )
    if dropped:
        return {"result": result, "warning": f"ignored unknown args: {dropped}"}
    return result


def load_all() -> int:
    """Import every tool module so their decorators run. Returns tool count."""
    from jarvis.tools import (  # noqa: F401
        apps, files, hearing, learning_tool, machine, media, memory, safety,
        screen, skills_tool, system, ui, web,
    )
    return len(REGISTRY)
