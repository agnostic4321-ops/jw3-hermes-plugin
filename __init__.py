"""JW3.ai trading plugin for Hermes Agent.

Non-custodial by construction: all tools shell out to the ``jw3`` CLI, which
owns the PAT and enforces server-side gates. The plugin holds no secrets.
"""

from __future__ import annotations

import json
from pathlib import Path

from .tools import TOOLS, TOOLSET, check_jw3_available, pre_tool_call_hook, run_jw3


def _status_handler(raw_args: str = "", **kwargs) -> str:
    """Handler for ``/jw3-status``: positions + risk in compact text.

    Returns text (never prints — output goes to the gateway's stdout).
    """
    del raw_args
    lines: list[str] = []
    for label, argv in (("positions", ["positions"]), ("risk", ["risk"])):
        try:
            payload = json.loads(run_jw3(argv, timeout=60))
        except ValueError:
            payload = {"error": "unparseable CLI output"}
        inner = payload.get("result", payload.get("error", payload))
        text = inner if isinstance(inner, str) else json.dumps(inner)[:1500]
        lines.append(f"== {label} ==\n{text}")
    return "\n".join(lines)


def register(ctx) -> None:
    for schema, handler in TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=check_jw3_available,
            emoji="📈",
        )
    ctx.register_skill(
        "jw3-ops",
        Path(__file__).parent / "skills" / "jw3-ops" / "SKILL.md",
        description="Trade crypto through JW3.ai safely from Hermes.",
    )
    ctx.register_command(
        "jw3-status",
        _status_handler,
        description="Show JW3 open positions and risk/kill-switch status.",
    )
    ctx.register_hook("pre_tool_call", pre_tool_call_hook)
