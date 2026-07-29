"""Natural-language logging. `agoge log "knee a bit puffy, prehab done, 208"`

This is the piece the watch can never do: the subjective layer that actually
gates the programming.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .config import Athlete, settings
from .db import DB
from .llm import complete_json, load_prompt

VALID_INJURY_KEYS: set[str] = set()


def parse(message: str, athlete: Athlete, day: date | None = None) -> dict[str, Any]:
    day = day or date.today()
    keys = [i["key"] for i in athlete.injuries]
    system = load_prompt("annotate") + f"\n\nValid injury_key values: {keys}"
    user = f"Today is {day.isoformat()} ({day.strftime('%A')}).\n\nMessage: {message}"
    return complete_json(system, user, model=settings.model_fast)


def apply(db: DB, athlete: Athlete, parsed: dict[str, Any],
          day: date | None = None) -> list[str]:
    """Write parsed data to the DB. Returns a human-readable list of what landed
    so the reply can confirm it — silent writes are how you lose trust in a log."""
    day = day or date.today()
    d = day.isoformat()
    keys = {i["key"] for i in athlete.injuries}
    applied: list[str] = []

    daily_fields = {k: parsed[k] for k in
                    ("prehab_done", "weight_lb", "protein_hit", "sleep_hours")
                    if k in parsed and parsed[k] is not None}
    if daily_fields:
        clean = {k: (int(v) if isinstance(v, bool) else v) for k, v in daily_fields.items()}
        db.upsert_daily(d, **clean)
        applied += [f"{k.replace('_', ' ')}: {v}" for k, v in daily_fields.items()]

    for s in parsed.get("symptoms") or []:
        key = s.get("injury_key")
        if key not in keys:
            applied.append(f"skipped unknown injury '{key}'")
            continue
        db.add_symptom(
            day=d, injury_key=key,
            severity=s.get("severity"),
            swelling=int(bool(s.get("swelling"))) if s.get("swelling") is not None else None,
            pain_type=s.get("pain_type"),
            overnight=int(bool(s.get("overnight"))),
            note=s.get("note"),
        )
        bits = [f"{key} severity {s.get('severity')}"]
        if s.get("swelling"):
            bits.append("swelling")
        if s.get("overnight"):
            bits.append("OVERNIGHT — this is the one that matters")
        applied.append(", ".join(bits))

    for e in parsed.get("events") or []:
        db.add_event(d, e.get("kind", "note"), e.get("detail", ""), e.get("severity", "info"))
        applied.append(f"event: {e.get('kind')} ({e.get('severity', 'info')})")

    if parsed.get("unparsed"):
        db.add_event(d, "unparsed", str(parsed["unparsed"]), "info")

    return applied


def resolve_open_symptoms(db: DB, athlete: Athlete, day: date | None = None) -> list[str]:
    """Morning follow-up: anything logged as still present overnight needs an
    answer today. This is the rule that otherwise gets forgotten at 6pm."""
    day = day or date.today()
    since = (day - timedelta(days=3)).isoformat()
    questions = []
    for inj in athlete.injuries:
        pending = [r for r in db.open_symptoms(inj["key"], since)
                   if r["overnight"] == 1 and not r["resolved_by"]]
        if pending:
            questions.append(
                f"{inj['label']}: symptoms logged {pending[-1]['day']} were still there "
                f"overnight. Has it settled?"
            )
    return questions


def mark_resolved(db: DB, injury_key: str, day: date | None = None) -> int:
    day = day or date.today()
    with db.tx() as c:
        cur = c.execute(
            "UPDATE symptoms SET resolved_by=? WHERE injury_key=? AND resolved_by IS NULL",
            (day.isoformat(), injury_key),
        )
        return cur.rowcount
