"""'What do I need to do today?' — plan lookup with readiness/injury overrides.

A spreadsheet written weeks in advance cannot see this morning's readiness.
Today's readiness wins. Injury gate outranks everything. Two-a-days list every
prescribed session for the date.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from .analysis import gap_check, injury_flags, readiness
from .config import Athlete
from .db import DB
from .plan_import import INTENSITY_TYPES, check_plan_conflicts

# Miss-rule volume cut applies to endurance load only — not strength duration.
_ENDURANCE_SPORTS = frozenset({"run", "bike", "swim", "walk", "brick"})
_ENDURANCE_TYPES = frozenset({"endurance", "interval", "brick", "test", ""})


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

    Each plan row is checked independently by sport so a completed swim does not
    clear a missed strength session on the same day.
    """
    missed = []
    for back in range(1, lookback + 1):
        day = today - timedelta(days=back)
        for plan in db.plan_for_day(day.isoformat()):
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


def _apply_overrides(plan, r: dict[str, Any],
                     flags: list[dict[str, Any]]) -> tuple[dict[str, Any], dict | None]:
    """Return (effective_row, override) for one plan row."""
    base = dict(plan)
    sport = (plan["sport"] or "").lower()
    st = (plan["session_type"] or "").lower()

    if r["flag"] == "red":
        override = {"reason": "readiness_red", "message": r["guidance"]}
        if any(f["severity"] == "red" for f in flags):
            # Injury gate: kill running / intensity; leave swim/strength alone.
            if sport == "run" or st in INTENSITY_TYPES:
                return {
                    **base,
                    "sport": "rest",
                    "session_type": "rest",
                    "title": "recovery (injury gate)",
                    "planned_min": 0,
                    "segments": "[]",
                    "notes": "Injury gate open — prescribed session overridden.",
                }, override
            return base, override
        return {
            **base,
            "session_type": "rest",
            "title": "recovery day",
            "planned_min": min(float(plan["planned_min"] or 0), 30),
            "notes": "Readiness red — prescribed intensity overridden.",
        }, override

    if r["flag"] == "amber":
        override = {"reason": "readiness_amber", "message": r["guidance"]}
        if st in INTENSITY_TYPES:
            return {
                **base,
                "session_type": "endurance",
                "title": f"easy {(plan['sport'] or 'session')} (amber override)",
                "notes": "Amber readiness — intensity dropped to easy endurance.",
            }, override
        return base, override

    return base, None


def what_today(db: DB, athlete: Athlete, today: date | None = None) -> dict[str, Any]:
    """Full answer for 'what do I need to do today?'"""
    today = today or date.today()
    day = today.isoformat()
    plans = [dict(p) for p in db.plan_for_day(day)]
    r = readiness(db, athlete, day)
    flags = injury_flags(db, athlete, day)
    gap = gap_check(db, today)
    missed = missed_prior_sessions(db, today)
    conflicts = check_plan_conflicts(plans, athlete, db=db, today=today)

    gap_pct = float(athlete.raw.get("load", {}).get("return_from_gap_pct", 70)) / 100
    effective_rows = []
    overrides = []
    for plan in plans:
        eff, ov = _apply_overrides(plan, r, flags)
        sport = (eff.get("sport") or "").lower()
        st = (eff.get("session_type") or "").lower()
        if (
            gap.get("gap_days")
            and gap["gap_days"] >= 3
            and eff.get("planned_min")
            and sport in _ENDURANCE_SPORTS
            and st in _ENDURANCE_TYPES
        ):
            trimmed = round(float(eff["planned_min"]) * gap_pct, 1)
            eff = {**eff, "planned_min": trimmed}
            eff["_volume_note"] = (
                f"2+ days missed — endurance volume cut to {gap_pct:.0%} "
                f"({trimmed:.0f} min). Do not catch up."
            )
        effective_rows.append(eff)
        if ov:
            overrides.append(ov)

    # Dedupe override messages for the reply header.
    override = overrides[0] if overrides else None
    volume_note = next(
        (e["_volume_note"] for e in effective_rows if e.get("_volume_note")),
        None,
    )

    reply = _compose_reply(
        db, athlete, today, plans, effective_rows, r, flags, missed,
        override, volume_note, conflicts, gap,
    )
    return {
        "day": day,
        "plan": plans[0] if len(plans) == 1 else None,
        "plans": plans,
        "effective": effective_rows[0] if len(effective_rows) == 1 else None,
        "effective_plans": effective_rows,
        "readiness": r,
        "missed_prior": missed,
        "override": override,
        "conflicts": conflicts,
        "gap": gap,
        "reply": reply,
    }


def _next_plan_day(db: DB, today: date) -> str | None:
    row = db.conn.execute(
        "SELECT MIN(day) AS d FROM plan "
        "WHERE day > ? AND status IN ('planned','prescribed')",
        (today.isoformat(),),
    ).fetchone()
    return row["d"] if row and row["d"] else None


def _continuity_note(db: DB, athlete: Athlete, today: date) -> str | None:
    """Between blocks / before an imported plan starts — routine continues.

    Used instead of a bare "nothing prescribed" when today sits after a block
    ends and before the next block or the first future plan row.
    """
    if athlete.current_block(today):
        return None
    prev = athlete.previous_block(today)
    nxt = athlete.next_block(today)
    plan_starts = _next_plan_day(db, today)
    if not prev and not plan_starts:
        return None

    bits = []
    if prev:
        bits.append(f"{prev['name']} ended {prev['end']}")
    if nxt:
        bits.append(f"{nxt['name']} starts {nxt['start']}")
    if plan_starts and (not nxt or plan_starts <= str(nxt["start"])):
        bits.append(f"imported plan begins {plan_starts}")
    if not bits:
        return None
    return "Between blocks (" + "; ".join(bits) + ") — current routine continues."


def _no_plan_reply(db: DB, athlete: Athlete, today: date,
                   r: dict[str, Any], flags: list[dict[str, Any]]) -> str:
    """Empty-plan day: readiness only — never 'train as planned'."""
    continuity = _continuity_note(db, athlete, today)
    if continuity:
        line = continuity
    else:
        line = "Nothing prescribed in the plan for today."
    line += f" Readiness is {r['score']}/100 ({r['flag']})."
    # Injury / recovery guidance is fine without a prescription.
    # Green "Train the session as planned" is not — there is no session.
    if any(f["severity"] == "red" for f in flags):
        line += f" {r['guidance']}"
    elif r["flag"] == "red":
        line += " Recovery day if you do anything — walk, prehab, sleep."
    elif r["flag"] == "amber":
        line += " If you train, take the easy end and stop if anything sharpens."
    return line


def _describe_one(plan, effective) -> str:
    sport = (effective.get("sport") or plan.get("sport") or "session").lower()
    st = (effective.get("session_type") or "").lower()

    if st == "rest" or sport == "rest":
        return "Rest / recovery"

    if st == "strength" or sport == "strength":
        focus = effective.get("lift_focus") or plan.get("lift_focus")
        line = "Strength"
        if focus:
            line += f" — {focus}"
        notes = effective.get("notes") or plan.get("notes")
        if notes and "override" not in (notes or "").lower():
            line += f". {notes}"
        return line if line.endswith(".") else line + "."

    dur = _human_duration(effective.get("planned_min") or plan.get("planned_min"))
    sport_label = sport.capitalize() if sport else "Session"
    head = sport_label + (f", {dur}" if dur else "") + "."

    segs = _describe_segments(_segments(effective))
    if segs:
        head += f" {segs}."
    else:
        lo = effective.get("target_hr_low") or plan.get("target_hr_low")
        hi = effective.get("target_hr_high") or plan.get("target_hr_high")
        if lo and hi:
            head += f" Keep HR {lo}–{hi}."
        elif hi:
            head += f" Keep it under {hi}."
    return head


def _compose_reply(
    db: DB,
    athlete: Athlete,
    today: date,
    plans: list[dict[str, Any]],
    effective_rows: list[dict[str, Any]],
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

    if not plans:
        parts.append(_no_plan_reply(db, athlete, today, r, flags))
        return " ".join(parts)

    # All-rest after overrides
    if all(
        (e.get("session_type") or "").lower() == "rest"
        or (e.get("sport") or "").lower() == "rest"
        for e in effective_rows
    ):
        if override and override["reason"] == "readiness_red":
            parts.append(override["message"])
        else:
            parts.append("Rest day. Prehab and sleep.")
        if flags:
            parts.append("; ".join(f["message"] for f in flags) + ".")
        return " ".join(parts)

    session_lines = [
        _describe_one(p, e)
        for p, e in zip(plans, effective_rows)
        if (e.get("session_type") or "").lower() != "rest"
        and (e.get("sport") or "").lower() != "rest"
    ]

    if len(session_lines) == 1:
        head = session_lines[0]
    else:
        numbered = " ".join(
            f"({i}) {line.rstrip('.')}" for i, line in enumerate(session_lines, 1)
        )
        n = len(session_lines)
        label = {2: "Two-a-day", 3: "Three-a-day"}.get(n, f"{n} sessions")
        head = f"{label}: {numbered}."

    if override and override["reason"] == "readiness_amber":
        head = head.rstrip(".") + ". Readiness amber — take the easy end, stop if anything sharpens."
    elif override and override["reason"] == "readiness_red":
        # Partial override (e.g. run killed, swim kept)
        head = head.rstrip(".") + f". {override['message']}"
    elif r["flag"] == "green" and not flags:
        head = head.rstrip(".") + f". {_injury_clear_note(db, athlete, today).capitalize()}."
    elif r["flag"] == "green":
        head = head.rstrip(".") + ". Train as planned."

    if volume_note:
        head = head.rstrip(".") + f". {volume_note}"
    for c in conflicts:
        if c["kind"] == "block_intensity":
            head = head.rstrip(".") + (
                ". Conflict: an intensity session sits in a base/Z2-only block "
                "— do not treat the spreadsheet as gospel today."
            )
            break

    parts.append(head if head.endswith(".") else head + ".")
    return " ".join(parts)
