"""telegram_bot.py -- the Telegram interface for agoge.

Long-polls Telegram's Bot API. No webhook, no public URL, no SSL cert needed
-- the right choice for a single-user personal bot running off a bare IP.

This file is deliberately a thin router and nothing more: it classifies each
incoming message's intent with one cheap LLM call, then dispatches to the
exact same, already-tested `agoge` CLI commands rather than reimplementing
any of that logic here. If this file breaks, nothing about the underlying
data, analysis, or injury gates breaks with it -- they don't live here.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import settings
from .llm import complete, load_prompt

API_BASE = "https://api.telegram.org/bot{token}"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
POLL_TIMEOUT = 25  # seconds -- Telegram holds a long-poll connection open this long

# The interpreter running this file already lives inside the venv (it was
# launched as `.venv/bin/python3 -m agoge.telegram_bot`), so the `agoge`
# console script sits right next to it. Deriving the path this way means it
# is correct regardless of whether .env paths are right -- one less thing
# this feature depends on getting configured correctly elsewhere.
AGOGE_BIN = str(Path(sys.executable).parent / "agoge")

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

# Patterns that look like a date reference even when parsing fails.
_DATE_HINT_RE = re.compile(
    rf"\b("
    rf"tomorrow|yesterday|next\s+week|last\s+week|"
    rf"\d{{4}}-\d{{1,2}}-\d{{1,2}}|"
    rf"\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?|"
    rf"(?:{_MONTH_ALT})\s+\d{{1,2}}(?:st|nd|rd|th)?|"
    rf"(?:{_MONTH_ALT})"
    rf")\b",
    re.I,
)


@dataclass(frozen=True)
class ParsedQueryDate:
    """Result of pulling a calendar day out of a free-text message."""
    day: date | None = None
    ambiguous: bool = False  # date-like text present, but not confidently parseable


def parse_query_date(text: str, *, today: date | None = None) -> ParsedQueryDate:
    """Extract an explicit or relative date from a message.

    Confident parses: ISO (`2026-08-13`), US numeric (`8/13`, `8/13/2026`),
    month+day (`August 13th`), and `tomorrow` / `yesterday`.

    If the message looks like it names a date but none of those forms match
    cleanly, return ambiguous=True so the caller can ask instead of silently
    defaulting to today.
    """
    today = today or date.today()
    raw = (text or "").strip()
    if not raw:
        return ParsedQueryDate()
    low = raw.lower()

    if re.search(r"\btomorrow\b", low):
        return ParsedQueryDate(day=today + timedelta(days=1))
    if re.search(r"\byesterday\b", low):
        return ParsedQueryDate(day=today - timedelta(days=1))

    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", raw)
    if m:
        try:
            return ParsedQueryDate(day=date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            return ParsedQueryDate(ambiguous=True)

    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", raw)
    if m:
        month, day_n = int(m.group(1)), int(m.group(2))
        year_s = m.group(3)
        if year_s:
            year = int(year_s)
            if year < 100:
                year += 2000
        else:
            year = today.year
        try:
            return ParsedQueryDate(day=date(year, month, day_n))
        except ValueError:
            return ParsedQueryDate(ambiguous=True)

    m = re.search(
        rf"\b({_MONTH_ALT})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{{4}}))?\b",
        low,
    )
    if m:
        month = _MONTHS[m.group(1)]
        day_n = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            return ParsedQueryDate(day=date(year, month, day_n))
        except ValueError:
            return ParsedQueryDate(ambiguous=True)

    if _DATE_HINT_RE.search(raw):
        return ParsedQueryDate(ambiguous=True)
    return ParsedQueryDate()


class TelegramBot:
    def __init__(
        self,
        token: str,
        allowed_chat_id: str,
        *,
        today_fn: Callable[[], date] | None = None,
    ):
        self.token = token
        self.allowed_chat_id = str(allowed_chat_id)
        self.base = API_BASE.format(token=token)
        self.offset = 0
        self._today = today_fn or date.today

    def run(self) -> None:
        print(f"agoge telegram bot starting, allowlisted chat: {self.allowed_chat_id}", flush=True)
        while True:
            try:
                for upd in self._get_updates():
                    self._handle_update(upd)
            except httpx.HTTPError as exc:
                print(f"transient network error, retrying: {exc}", file=sys.stderr, flush=True)
                time.sleep(5)
            except Exception as exc:  # never let one bad update kill the loop
                print(f"unhandled error in poll loop: {exc}", file=sys.stderr, flush=True)
                time.sleep(5)

    def _get_updates(self) -> list[dict[str, Any]]:
        resp = httpx.get(
            f"{self.base}/getUpdates",
            params={"offset": self.offset, "timeout": POLL_TIMEOUT},
            timeout=POLL_TIMEOUT + 10,
        )
        resp.raise_for_status()
        results = resp.json().get("result", [])
        if results:
            self.offset = results[-1]["update_id"] + 1
        return results

    def _handle_update(self, upd: dict[str, Any]) -> None:
        msg = upd.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            return

        if chat_id != self.allowed_chat_id:
            # This is the entire allowlist. Never process or reply to anyone
            # else -- just log the chat_id once so the real owner can find
            # theirs during setup, then go silent.
            print(f"ignored message from unrecognized chat_id={chat_id}", flush=True)
            return

        self._send(chat_id, self._route(text))

    def _route(self, text: str) -> str:
        intent, parsed_date = self._classify(text)
        if intent == "LOG":
            return self._run_cli(["log", text])
        if intent == "TODAY":
            if parsed_date.ambiguous:
                return (
                    "I see a date in that message but couldn't parse it confidently. "
                    "Try YYYY-MM-DD, 'August 13', '8/13', or 'tomorrow'."
                )
            if parsed_date.day is not None:
                return self._run_cli(["today", "--date", parsed_date.day.isoformat()])
            return self._run_cli(["today"])
        if intent == "STATUS":
            return self._run_cli(["status"])
        return ("Not sure what you mean -- try describing what happened, "
                "asking what to do today, or asking for your status.")

    def _classify(self, text: str) -> tuple[str, ParsedQueryDate]:
        """Intent label plus any date extracted from the same message."""
        try:
            label = complete(
                system=load_prompt("route"), user=text,
                model=settings.model_fast, max_tokens=10, temperature=0,
            ).strip().upper()
        except Exception:
            label = "UNCLEAR"
        intent = label if label in ("LOG", "TODAY", "STATUS") else "UNCLEAR"
        return intent, parse_query_date(text, today=self._today())

    def _run_cli(self, args: list[str]) -> str:
        try:
            proc = subprocess.run(
                [AGOGE_BIN, *args], capture_output=True, text=True, timeout=120,
            )
            out = ANSI_RE.sub("", (proc.stdout or "") + (proc.stderr or "")).strip()
            return out or "(no output)"
        except subprocess.TimeoutExpired:
            return "That took too long to answer -- try again in a bit."
        except Exception as exc:
            return f"Something broke on my end: {exc}"

    def _send(self, chat_id: str, text: str) -> None:
        # Telegram caps a single message at 4096 characters. Split rather
        # than silently truncate a longer reply.
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or ["(empty response)"]
        for chunk in chunks:
            httpx.post(f"{self.base}/sendMessage",
                      json={"chat_id": chat_id, "text": chunk}, timeout=15)


def run() -> None:
    import os
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "")
    if not token or not chat_id:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_ID in .env first.",
              file=sys.stderr)
        sys.exit(1)
    TelegramBot(token, chat_id).run()


if __name__ == "__main__":
    run()
