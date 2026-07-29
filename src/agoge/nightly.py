"""The nightly job. Cron runs this at 04:00.

Order matters: pull, normalize, store, compute, then write prose. The model is
the last step and it only ever sees numbers that are already in the database.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .analysis import (energy_availability, gap_check, injury_flags, load_check,
                       protein_status, race_status, readiness, zone_compliance)
from .config import Athlete, settings
from .coros import CorosClient
from .db import DB
from .llm import complete, load_prompt
from .normalize import normalize_daily, normalize_session, pick


def ingest_day(db: DB, athlete: Athlete, day: date, client: CorosClient | None = None) -> dict[str, Any]:
    client = client or CorosClient()
    payload = client.fetch_day(day)
    return store_payload(db, athlete, day, payload)


def store_payload(db: DB, athlete: Athlete, day: date, payload: dict[str, Any]) -> dict[str, Any]:
    """Split out from ingest_day so it can be tested with fixtures and so a
    manual FIT/CSV import can reuse the exact same path."""
    d = day.isoformat()
    acts = payload.get("activities") or []
    if isinstance(acts, dict):
        acts = acts.get("data") or acts.get("activities") or acts.get("items") or []

    stored = []
    for raw in acts:
        s = normalize_session(raw, d)
        if not s.get("coros_id"):
            s["coros_id"] = f"{d}-{s['sport']}-{s.get('start_time') or len(stored)}"
        s["z2_pct"] = zone_compliance(s, athlete)
        db.upsert_session(s)
        stored.append(s)

    daily_raw = payload.get("daily") or {}
    if isinstance(daily_raw, dict) and "data" in daily_raw:
        daily_raw = daily_raw["data"]
    dd = normalize_daily(daily_raw, payload.get("sleep"), payload.get("fitness"))
    db.upsert_daily(d, **dd)

    r = readiness(db, athlete, d)
    db.upsert_daily(d, readiness=r["score"], readiness_flag=r["flag"])
    return {"sessions": stored, "daily": dd, "readiness": r}


def write_note(db: DB, athlete: Athlete, day: date) -> str:
    d = day.isoformat()
    ctx = _day_context(db, athlete, day)
    body = complete(system=load_prompt("nightly"), user=ctx,
                    model=settings.model_fast, max_tokens=500)
    db.add_note(d, "daily", body, settings.model_fast)
    return body


def _day_context(db: DB, athlete: Athlete, day: date) -> str:
    d = day.isoformat()
    row = db.day(d)
    sessions = db.sessions_between(d, d)
    race = race_status(db, athlete, day)
    load = load_check(db, athlete, day)
    gap = gap_check(db, day)
    r = readiness(db, athlete, d)

    lines = [f"Date: {d} ({day.strftime('%A')})",
             f"Block: {race['block']} — {race['block_notes']}",
             f"{race['days_to_race']} days to {athlete.raw['race']['name']}",
             ""]

    if sessions:
        lines.append("Sessions today:")
        for s in sessions:
            parts = [f"  {s['sport']}"]
            for label, key, fmt in (("duration", "duration_min", "{:.0f} min"),
                                    ("distance", "distance_mi", "{:.2f} mi"),
                                    ("avg HR", "avg_hr", "{:.0f}"),
                                    ("max HR", "max_hr", "{:.0f}"),
                                    ("cadence", "avg_cadence", "{:.0f} spm"),
                                    ("Z2 time", "z2_pct", "{:.0f}%")):
                if s[key] is not None:
                    parts.append(f"{label} {fmt.format(s[key])}")
            lines.append(", ".join(parts))
    else:
        lines.append("Sessions today: none recorded")

    lines.append("")
    if row:
        wellness = []
        for label, key, fmt in (("sleep", "sleep_hours", "{:.1f} h"),
                                ("sleep score", "sleep_score", "{:.0f}"),
                                ("HRV", "hrv", "{:.0f}"),
                                ("resting HR", "resting_hr", "{:.0f}"),
                                ("steps", "steps", "{:.0f}"),
                                ("training load", "training_load", "{:.0f}"),
                                ("weight", "weight_lb", "{:.1f} lb")):
            if row[key] is not None:
                wellness.append(f"{label} {fmt.format(row[key])}")
        lines.append("Wellness: " + (", ".join(wellness) if wellness else "no data"))
        for col, label in (("hrv", "HRV"), ("resting_hr", "resting HR")):
            base = db.baseline(col, d)
            if base and row[col]:
                lines.append(f"  {label} 28-day baseline {base:.0f} (today {row[col]:.0f})")
        if row["prehab_done"] is not None:
            lines.append(f"Prehab: {'done' if row['prehab_done'] else 'NOT DONE'} "
                         f"(streak {db.prehab_streak(d)} days)")

    lines += ["",
              f"Readiness: {r['score']}/100 ({r['flag']}) — "
              f"{'; '.join(r['reasons']) if r['reasons'] else 'nothing flagged'}",
              f"Load: week to date {load['this_week_min']:.0f} min vs "
              f"{load['last_week_min']:.0f} min last week "
              f"(cap {load['cap_min']}, headroom {load['headroom_min']})",
              f"Consistency: {gap['action']}"]

    ea = energy_availability(db, athlete, d)
    if ea.get("available"):
        lines.append(f"Fuelling: intake {ea['kcal_in']:.0f} kcal, exercise cost "
                     f"~{ea['kcal_out']:.0f}, energy availability {ea['ea']:.0f} "
                     f"kcal/kg FFM ({ea['flag']}, floor {ea['floor']:.0f})")
    prot = protein_status(db, athlete, d)
    if prot.get("available") and prot.get("today") is not None:
        lines.append(f"Protein: {prot['today']:.0f} g today, target {prot['target']:.0f}, "
                     f"{prot['avg']:.0f} g/day over last {prot['days_logged']} logged days")

    flags = injury_flags(db, athlete, d)
    if flags:
        lines.append("INJURY FLAGS:")
        for f in flags:
            lines.append(f"  [{f['severity']}] {f['message']}")
    for inj in athlete.injuries:
        lines.append(f"Rule for {inj['label']}: {inj['rule'].strip()}")
    return "\n".join(lines)


def run(day: date | None = None, skip_fetch: bool = False) -> dict[str, Any]:
    """Entry point for cron."""
    day = day or (date.today() - timedelta(days=1))
    athlete = Athlete.load(settings.athlete_file)
    db = DB(settings.db_path)
    result: dict[str, Any] = {"day": day.isoformat()}
    if not skip_fetch:
        try:
            result.update(ingest_day(db, athlete, day))
        except Exception as exc:
            db.add_event(day.isoformat(), "ingest_error", str(exc), "warn")
            result["ingest_error"] = str(exc)
    try:
        from .cronometer import ingest as cron_ingest
        result["cronometer"] = cron_ingest(db, day)
    except Exception as exc:
        # Never fatal, and never silent: a missing nutrition day must not be
        # recorded as a day of zero calories.
        db.add_event(day.isoformat(), "cronometer_error", str(exc), "warn")
        result["cronometer_error"] = str(exc)

    result["energy_availability"] = energy_availability(db, athlete, day.isoformat())
    if result["energy_availability"].get("ea") is not None:
        db.upsert_daily(day.isoformat(), energy_avail=result["energy_availability"]["ea"])

    result["note"] = write_note(db, athlete, day)
    result["readiness"] = readiness(db, athlete, day.isoformat())
    return result
