"""Telegram router date parsing — no network, no LLM.

_classify extracts a date alongside the intent; TODAY routes pass it through
to `agoge today --date`. Ambiguous date references must not silently become today.
"""
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agoge.telegram_bot import ParsedQueryDate, TelegramBot, parse_query_date

TODAY = date(2026, 8, 7)


def main():
    print("== explicit dates ==")
    for text, expected in [
        ("what should I do on August 13th", date(2026, 8, 13)),
        ("plan for Aug 13", date(2026, 8, 13)),
        ("what about 8/13", date(2026, 8, 13)),
        ("workout on 2026-08-13", date(2026, 8, 13)),
        ("August 13, 2027 please", date(2027, 8, 13)),
    ]:
        p = parse_query_date(text, today=TODAY)
        print(f"  {text!r} → {p.day}")
        assert p.day == expected, (text, p)
        assert not p.ambiguous
    print("  ok")

    print("== relative date ==")
    p = parse_query_date("what should I do tomorrow", today=TODAY)
    assert p.day == TODAY + timedelta(days=1)
    assert not p.ambiguous
    print(f"  tomorrow → {p.day}")
    print("  ok")

    print("== no date — default path (None, not ambiguous) ==")
    p = parse_query_date("what should I do today", today=TODAY)
    assert p == ParsedQueryDate(day=None, ambiguous=False), p
    p2 = parse_query_date("what's on the plan", today=TODAY)
    assert p2.day is None and not p2.ambiguous
    print("  ok")

    print("== date-like but unparseable — ambiguous, do not default ==")
    for text in ("what should I do in August", "plan for next week", "August 99th"):
        p = parse_query_date(text, today=TODAY)
        print(f"  {text!r} → ambiguous={p.ambiguous} day={p.day}")
        assert p.ambiguous, text
        assert p.day is None
    print("  ok")

    print("== TODAY route passes --date through ==")
    bot = TelegramBot("fake-token", "1", today_fn=lambda: TODAY)
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        return "ok"

    bot._run_cli = fake_run  # type: ignore[method-assign]

    with patch.object(bot, "_classify") as classify:
        classify.side_effect = lambda text: (
            "TODAY", parse_query_date(text, today=TODAY),
        )

        calls.clear()
        bot._route("what should I do on August 13th")
        assert calls == [["today", "--date", "2026-08-13"]], calls

        calls.clear()
        bot._route("what should I do tomorrow")
        assert calls == [["today", "--date", "2026-08-08"]], calls

        calls.clear()
        bot._route("what should I do today")
        assert calls == [["today"]], calls

        calls.clear()
        reply = bot._route("what should I do in August")
        assert calls == []
        assert "couldn't parse" in reply.lower()
        assert "silently" not in reply.lower()
    print("  ok")

    print("\nOK — telegram date parsing works.")


if __name__ == "__main__":
    main()
