"""'What do I need to do today?' — plan lookup with readiness/injury overrides.

A spreadsheet written weeks in advance cannot see this morning's readiness.
Today's readiness wins. Injury gate outranks everything.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from .analysis import gap_check, injury_flags, readiness
from .config import Athlete
from .db import DB
from .plan_import import INTENSITY_TYPES, check_plan_conflicts


def _segments(row) -> list[dict[str, Any]]:
    raw = row["segments"] if row is not None and "segments" in row.keys() else None
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return []


def missed_prior_sessions(db: DB, today: date, lookback: int = 3) -> list[dict[str, Any]]:
    """Prescribed sessions in the last few days with no matching COROS session.

    Surfaced before today's prescription because the miss-rules change what
    today should be.
    """
    missed = []
    for back in range(1, lookback + 1):
        day = today - timedelta(days=back)
        plan = db.plan_for_day(day.isoformat())
        if not plan:
            continue
        st = (plan["session_type"] or "").lower()
        sport = (plan["sport"] or "").lower()
        if st == "rest" or sport == "rest" or plan["status"] in (
            "done", "skipped", "cancelled", "missed",
        ):
            continue
        sessions = db.sessions_between(day.isoformat(), day.isoformat())
        if sport and sport not in ("rest", "other"):
            matched = [s for s in sessions if s["sport"] == sport]
        else:
            matched = list(sessions)
        if matched:
            continue
        missed.append({
            "day": day.isoformat(),
            "sport": plan["sport"],
            "session_type": plan["session_type"],
            "title": plan["title"],
            "planned_min": plan["planned_min"],
        })
    return missed


def _describe_segments(segments: list[dict[str, Any]]) -> str:
    if not segments:
        return ""
    bits = []
    for s in segments:
        label = s.get("label") or s.get("raw") or ""
        dur = s.get("duration_min")
        hr_lo, hr_hi = s.get("hr_low"), s.get("hr_high")
        piece = f"{dur:.0f} min {label}" if dur else label
        if hr_lo is not None and hr_hi is not None:
            piece += f" ({hr_lo}–{hr_hi} bpm)"
        elif hr_hi is not None:
            piece += f" (under {hr_hi} bpm)"
        bits.append(piece)
    if len(bits) == 1:
        return bits[0]
    if len(bits) == 2:
        return f"{bits[0]}, then {bits[1]}"
    return ", ".join(bits[:-1]) + f", then {bits[-1]}"


def _human_duration(minutes: float | None) -> str:
    if minutes is None:
        return ""
    minutes = float(minutes)
    if minutes >= 60 and minutes % 60 == 0:
        h = int(minutes // 60)
        return f"{h} hour" if h == 1 else f"{h} hours"
    if minutes >= 60:
        h = int(minutes // 60)
        m = int(minutes % 60)
        return f"{h}h {m}m"
    return f"{int(minutes)} min"


def _injury_clear_note(db: DB, athlete: Athlete, today: date) -> str:
    """Short clause when recent overnight symptoms have cleared."""
    since = (today - timedelta(days=3)).isoformat()
    bits = []
    for inj in athlete.injuries:
        rows = db.open_symptoms(inj["key"], since)
        if not rows:
            continue
        latest = rows[0]
        if latest["overnight"] == 1 and not latest["resolved_by"]:
            continue
        if latest["day"] < today.isoformat() and (
            latest["resolved_by"] or latest["overnight"] in (0, None)
        ):
            short = inj["label"].split("(")[0].strip()
            bits.append(f"{short} was clear yesterday")
    if not bits:
        return "no restrictions today"
    return bits[0] + ", no restrictions today"


def what_today(db: DB, athlete: Athlete, today: date | None = None) -> dict[str, Any]:
    """Full answer for 'what do I need to do today?'"""
    today = today or date.today()
    day = today.isoformat()
    plan = db.plan_for_day(day)
    r = readiness(db, athlete, day)
    flags = injury_flags(db, athlete, day)
    gap = gap_check(db, today)
    missed = missed_prior_sessions(db, today)
    conflicts = check_plan_conflicts(
        [dict(plan)] if plan else [], athlete, db=db, today=today,
    )

    override = None
    effective = dict(plan) if plan else None

    if plan and r["flag"] == "red":
        override = {"reason": "readiness_red", "message": r["guidance"]}
        if any(f["severity"] == "red" for f in flags):
            if (plan["sport"] or "").lower() == "run" or (
                plan["session_type"] or ""
            ).lower() in INTENSITY_TYPES:
                effective = {
                    **dict(plan),
                    "sport": "rest",
                    "session_type": "rest",
                    "title": "recovery (injury gate)",
                    "planned_min": 0,
                    "segments": "[]",
                    "notes": "Injury gate open — prescribed session overridden.",
                }
        else:
            effective = {
                **dict(plan),
                "session_type": "rest",
                "title": "recovery day",
                "planned_min": min(float(plan["planned_min"] or 0), 30),
                "notes": "Readiness red — prescribed intensity overridden.",
            }
    elif plan and r["flag"] == "amber":
        override = {"reason": "readiness_amber", "message": r["guidance"]}
        if (plan["session_type"] or "").lower() in INTENSITY_TYPES:
            effective = {
                **dict(plan),
                "session_type": "endurance",
                "title": f"easy {(plan['sport'] or 'session')} (amber override)",
                "notes": "Amber readiness — intensity dropped to easy endurance.",
            }

    volume_note = None
    if (
        gap.get("gap_days")
        and gap["gap_days"] >= 3
        and effective
        and effective.get("planned_min")
        and (effective.get("session_type") or "") != "rest"
    ):
        trimmed = round(float(effective["planned_min"]) * 0.7, 1)
        effective = {**effective, "planned_min": trimmed}
        volume_note = (
            f"2+ days missed — volume cut to 70% ({trimmed:.0f} min). "
            f"Do not catch up."
        )

    reply = _compose_reply(
        db, athlete, today, plan, effective, r, flags, missed,
        override, volume_note, conflicts, gap,
    )
    return {
        "day": day,
        "plan": dict(plan) if plan else None,
        "effective": effective,
        "readiness": r,
        "missed_prior": missed,
        "override": override,
        "conflicts": conflicts,
        "gap": gap,
        "reply": reply,
    }


def _compose_reply(
    db: DB,
    athlete: Athlete,
    today: date,
    plan,
    effective,
    r: dict[str, Any],
    flags: list[dict[str, Any]],
    missed: list[dict[str, Any]],
    override: dict[str, Any] | None,
    volume_note: str | None,
    conflicts: list[dict[str, Any]],
    gap: dict[str, Any],
) -> str:
    parts: list[str] = []

    if missed:
        m = missed[0]
        label = m["title"] or m["sport"] or "session"
        parts.append(
            f"Before today: did you get to {m['day']}'s {label}? "
            f"({gap['action']})"
        )

    if not plan:
        parts.append(
            "Nothing prescribed in the plan for today. "
            f"Readiness is {r['score']}/100 ({r['flag']}). {r['guidance']}"
        )
        return " ".join(parts)

    assert effective is not None
    sport = (effective.get("sport") or plan["sport"] or "session").lower()
    st = (effective.get("session_type") or "").lower()

    if st == "rest" or sport == "rest":
        if override and override["reason"] == "readiness_red":
            parts.append(override["message"])
        else:
            parts.append("Rest day. Prehab and sleep.")
        if flags:
            parts.append("; ".join(f["message"] for f in flags) + ".")
        return " ".join(parts)

    if st == "strength":
        focus = effective.get("lift_focus") or plan["lift_focus"]
        line = "Strength"
        if focus:
            line += f" — {focus}"
        notes = effective.get("notes") or plan["notes"]
        if notes:
            line += f". {notes}"
        if override:
            line += f" Readiness {r['flag']} — take it easy if anything feels off."
        parts.append(line if line.endswith(".") else line + ".")
        return " ".join(parts)

    dur = _human_duration(effective.get("planned_min") or plan["planned_min"])
    sport_label = sport.capitalize() if sport else "Session"
    head = sport_label + (f", {dur}" if dur else "") + "."

    segs = _describe_segments(_segments(effective))
    if segs:
        head += f" {segs}."
    else:
        lo = effective.get("target_hr_low") or plan["target_hr_low"]
        hi = effective.get("target_hr_high") or plan["target_hr_high"]
        if lo and hi:
            head += f" Keep HR {lo}–{hi}."
        elif hi:
            head += f" Keep it under {hi}."

    if override and override["reason"] == "readiness_amber":
        head += " Readiness amber — take the easy end, stop if anything sharpens."
    elif override and override["reason"] == "readiness_red":
        head += f" {override['message']}"
    elif r["flag"] == "green" and not flags:
        head += f" {_injury_clear_note(db, athlete, today).capitalize()}."
    elif r["flag"] == "green":
        head += " Train as planned."

    if volume_note:
        head += f" {volume_note}"
    for c in conflicts:
        if c["kind"] == "block_intensity":
            head += (
                " Conflict: this intensity session sits in a base/Z2-only block "
                "— do not treat the spreadsheet as gospel today."
            )
            break

    parts.append(head)
    return " ".join(parts)
