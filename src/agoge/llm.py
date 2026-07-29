"""Anthropic API wrapper. The model writes prose and parses your texts.
It never invents numbers — everything factual is passed in already computed."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from .config import settings

PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


def _client() -> Anthropic:
    if not settings.anthropic_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set — copy .env.example to .env")
    return Anthropic(api_key=settings.anthropic_key)


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text()


def complete(system: str, user: str, model: str | None = None,
             max_tokens: int = 1200, temperature: float = 0.3) -> str:
    resp = _client().messages.create(
        model=model or settings.model_fast,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def complete_json(system: str, user: str, model: str | None = None,
                  max_tokens: int = 1000) -> Any:
    """For structured extraction. Strips fences, fails loudly rather than
    silently writing garbage to the database."""
    raw = complete(
        system + "\n\nRespond with JSON only. No preamble, no markdown fences.",
        user, model=model, max_tokens=max_tokens, temperature=0,
    )
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model did not return valid JSON: {cleaned[:300]}") from exc
