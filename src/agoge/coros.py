"""COROS MCP client.

Design note: ingestion is deterministic. We call MCP tools directly and parse
the results ourselves rather than asking a model to do it. The LLM only writes
prose later, from data that is already structured. That is cheaper, faster, and
it cannot hallucinate your heart rate.

The COROS MCP is OAuth-protected. Run `agoge auth` once to complete the browser
flow; the token is cached in data/.coros_tokens.json (gitignored).
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .config import settings

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover
    MCP_AVAILABLE = False


# COROS has not published tool names in a stable public spec, so we discover
# them on first run and cache the mapping. These are the patterns we look for.
TOOL_PATTERNS = {
    "activities": [r"activit", r"workout.*list", r"list.*workout"],
    "activity_detail": [r"activity.*detail", r"workout.*detail", r"get.*activity"],
    "daily": [r"daily", r"summary", r"health"],
    "sleep": [r"sleep"],
    "fitness": [r"fitness", r"vo2", r"threshold", r"training.*status"],
    "laps": [r"lap", r"segment"],
    "fit_file": [r"fit", r"file"],
}


class CorosClient:
    def __init__(self, url: str | None = None, token: str | None = None):
        self.url = url or settings.coros_mcp_url
        self.token = token or self._load_token()
        self.map_path = settings.data_dir / "coros_tools.json"

    # ---------- auth ----------

    def _load_token(self) -> str | None:
        p = settings.token_path
        if p.exists():
            try:
                return json.loads(p.read_text()).get("access_token")
            except Exception:
                return None
        return None

    def save_token(self, access_token: str, **extra: Any) -> None:
        settings.token_path.write_text(
            json.dumps({"access_token": access_token, **extra}, indent=2)
        )
        settings.token_path.chmod(0o600)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    # ---------- transport ----------

    async def _session(self):
        if not MCP_AVAILABLE:
            raise RuntimeError("pip install mcp")
        return streamablehttp_client(self.url, headers=self.headers)

    async def _alist_tools(self) -> list[dict[str, Any]]:
        async with streamablehttp_client(self.url, headers=self.headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.list_tools()
                return [
                    {"name": t.name, "description": t.description or "",
                     "schema": getattr(t, "inputSchema", None)}
                    for t in result.tools
                ]

    async def _acall(self, tool: str, args: dict[str, Any]) -> Any:
        async with streamablehttp_client(self.url, headers=self.headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                res = await session.call_tool(tool, args)
                return _unwrap(res)

    def list_tools(self) -> list[dict[str, Any]]:
        return asyncio.run(self._alist_tools())

    def call(self, tool: str, **args: Any) -> Any:
        return asyncio.run(self._acall(tool, args))

    # ---------- discovery ----------

    def discover(self) -> dict[str, str]:
        """Match live tool names against the patterns above and cache the map."""
        tools = self.list_tools()
        names = [t["name"] for t in tools]
        mapping: dict[str, str] = {}
        for role, patterns in TOOL_PATTERNS.items():
            for pat in patterns:
                hit = next((n for n in names if re.search(pat, n, re.I)), None)
                if hit:
                    mapping[role] = hit
                    break
        self.map_path.write_text(json.dumps(
            {"map": mapping, "all_tools": tools}, indent=2, default=str
        ))
        return mapping

    def tool_for(self, role: str) -> str:
        if self.map_path.exists():
            cached = json.loads(self.map_path.read_text()).get("map", {})
            if role in cached:
                return cached[role]
        mapping = self.discover()
        if role not in mapping:
            raise KeyError(
                f"No COROS tool matched role '{role}'. Run `agoge coros tools` to see "
                f"what is actually exposed, then edit {self.map_path}."
            )
        return mapping[role]

    # ---------- domain fetches ----------

    def fetch_day(self, day: date) -> dict[str, Any]:
        """Everything we want for one calendar day, in one shot."""
        d = day.isoformat()
        out: dict[str, Any] = {"day": d, "activities": [], "daily": {}, "fitness": {}}
        for role, key in (("activities", "activities"), ("daily", "daily"),
                          ("sleep", "sleep"), ("fitness", "fitness")):
            try:
                tool = self.tool_for(role)
            except KeyError:
                continue
            try:
                out[key] = self.call(tool, **_date_args(tool, d))
            except Exception as exc:  # keep going; a partial day beats no day
                out.setdefault("errors", []).append(f"{role}: {exc}")
        return out


def _date_args(tool_name: str, d: str) -> dict[str, str]:
    """COROS tools vary between start/end and single-date parameters. Send both
    shapes; the server ignores what it does not use."""
    return {"date": d, "start_date": d, "end_date": d}


def _unwrap(result: Any) -> Any:
    """MCP tool results arrive as content blocks. Pull out the useful payload."""
    content = getattr(result, "content", None) or []
    texts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            texts.append(block.text)
    joined = "\n".join(texts)
    if not joined:
        return getattr(result, "structuredContent", None) or {}
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return {"text": joined}
