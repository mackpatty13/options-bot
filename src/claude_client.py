"""Claude decision step.

Claude is the gatekeeper, not the signal source. Indicators are computed deterministically
in Python; Claude only chooses among enter/exit/hold given the signal + portfolio state.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from . import config

log = logging.getLogger(__name__)


def _load_rules() -> str:
    rules_path = Path(__file__).resolve().parent.parent / "TRADING_RULES.md"
    return rules_path.read_text(encoding="utf-8")


SYSTEM_INSTRUCTION = (
    "You are the decision gatekeeper for a ~$50,000 paper-trading options bot. "
    "Indicators and entry signals are computed in Python BEFORE you see them. "
    "Your job is to pick exactly one action given the full state, and return ONLY a JSON object. "
    "Do not invent trades not supported by a fired signal. Do not propose option contracts; "
    "the Python layer constructs the spread once you decide a direction. "
    "The order validator will independently re-check every rule; you cannot bypass it. "
    "Return one of:\n"
    '  {"action": "enter", "underlying": "SPY"|"QQQ", "direction": "bull"|"bear"}\n'
    '  {"action": "exit", "position_id": "<symbol of long leg>"}\n'
    '  {"action": "hold"}\n'
    "No prose, no markdown, no explanation. Only the JSON object."
)


class ClaudeClient:
    def __init__(self) -> None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY must be set")
        self._client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._rules_text = _load_rules()

    def decide(self, *, cycle_context: dict[str, Any]) -> dict[str, Any]:
        """Send one decision request. Returns the parsed JSON action."""
        # System content is two blocks: stable instruction (cached) + stable rules (cached).
        # The user message is the per-cycle context, which changes every call.
        system_blocks = [
            {
                "type": "text",
                "text": SYSTEM_INSTRUCTION,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": "TRADING_RULES.md follows. Read it; the validator enforces every limit.\n\n"
                        + self._rules_text,
                "cache_control": {"type": "ephemeral"},
            },
        ]
        user_payload = json.dumps(cycle_context, default=str, indent=2)

        # temperature is deprecated on opus-4.7; the model is deterministic by default.
        resp = self._client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            system=system_blocks,
            messages=[{"role": "user", "content": user_payload}],
        )

        # Extract first text block.
        raw = ""
        for block in resp.content:
            if getattr(block, "type", "") == "text":
                raw = block.text
                break

        return _parse_decision(raw)


def _parse_decision(raw: str) -> dict[str, Any]:
    """Pull the JSON object out of Claude's response. Fail closed."""
    raw = raw.strip()
    # If the model wrapped it in code fences, strip them.
    if raw.startswith("```"):
        raw = raw.strip("`")
        # remove any leading "json\n"
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip("\n")
    # Find the outermost {...}
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return {"action": "hold", "_parse_error": "no JSON object in response", "_raw": raw[:200]}
    try:
        obj = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as e:
        return {"action": "hold", "_parse_error": str(e), "_raw": raw[:200]}
    if obj.get("action") not in ("enter", "exit", "hold"):
        return {"action": "hold", "_parse_error": f"bad action: {obj.get('action')!r}",
                "_raw": raw[:200]}
    return obj
