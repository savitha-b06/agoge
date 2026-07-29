"""The rolling athlete profile — the 'model of yourself'.

This is a markdown file the agent maintains about you. It is loaded as context
on every conversation. It is not weights; it is a document, which means when it
gets something wrong you can open it and fix the line.
"""
from __future__ import annotations

from datetime import date, timedelta

from .analysis import checkpoint_status, load_check, race_status
from .config import Athlete, settings
from .db import DB
from .llm import complete, load_prompt

HEADER = "# Athlete profile\n\n*Maintained by agoge. Edit freely — the agent reads what you leave here.*\n"


def build_context(db: DB, athlete: Athlete, today: date | None = None,
                  days: int = 21) -> str:
    """Everything the model needs to say something useful, as compact text."""
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat()
    race = race_status(db, athlete, today)
    load = load_check(db, athlete, today)

    lines = [
        f"Athlete: {athlete.name}, age {athlete.raw.get('age')}, max HR {athlete.raw.get('max_hr')}",
        f"Zones: " + ", ".join(f"{k} {v[0]}-{v[1]}" for k, v in athlete.zones.items()),
        f"Race: {athlete.raw['race']['name']} on {athlete.race_date} "
        f"({race['days_to_race']} days, {race['weeks_to_race']} weeks out)",
        f"Current block: {race['block']} — {race['block_notes']}",
        "",
        f"Load: this week {load['this_week_min']} min, last week {load['last_week_min']} min, "
        f"change {load['change_pct']}%, cap {load['cap_min']} min",
        "",
        "Sessions (last 3 weeks):",
    ]
    for s in db.sessions_between(since, today.isoformat()):
        bits = [s["day"], s["sport"]]
        if s["duration_min"]:
            bits.append(f"{s['duration_min']:.0f}min")
        if s["distance_mi"]:
            bits.append(f"{s['distance_mi']:.2f}mi")
        if s["avg_hr"]:
            bits.append(f"avgHR {s['avg_hr']}")
        if s["z2_pct"] is not None:
            bits.append(f"Z2 {s['z2_pct']:.0f}%")
        lines.append("  " + "  ".join(bits))

    lines += ["", "Daily (last 3 weeks):"]
    for d in db.daily_between(since, today.isoformat()):
        bits = [d["day"]]
        for label, key, fmt in (("sleep", "sleep_hours", "{:.1f}h"), ("HRV", "hrv", "{:.0f}"),
                                ("RHR", "resting_hr", "{:.0f}"), ("wt", "weight_lb", "{:.0f}lb")):
            if d[key] is not None:
                bits.append(f"{label} {fmt.format(d[key])}")
        if d["prehab_done"] is not None:
            bits.append("prehab " + ("yes" if d["prehab_done"] else "NO"))
        if len(bits) > 1:
            lines.append("  " + "  ".join(bits))

    sym = db.recent_symptoms(since)
    if sym:
        lines += ["", "Symptoms:"]
        for s in sym:
            lines.append(f"  {s['day']} {s['injury_key']} severity {s['severity']}"
                         f"{' OVERNIGHT' if s['overnight'] else ''}"
                         f"{' — ' + s['note'] if s['note'] else ''}")

    cps = checkpoint_status(db, athlete, today)
    if cps:
        lines += ["", "Checkpoints:"]
        for c in cps:
            lines.append(f"  {c['metric']}: {c['actual']} / target {c['target']} "
                         f"(due {c['due']}, {c['days_left']}d) "
                         f"{'ON TRACK' if c['on_track'] else 'BEHIND'}")

    prior = read_profile()
    if prior:
        lines += ["", "--- Standing profile ---", prior]
    return "\n".join(lines)


def read_profile() -> str:
    p = settings.profile_path
    return p.read_text() if p.exists() else ""


def update_profile(db: DB, athlete: Athlete, today: date | None = None) -> str:
    """Rewrite the standing profile from the last 8 weeks. Run weekly."""
    today = today or date.today()
    context = build_context(db, athlete, today, days=56)
    body = complete(
        system=load_prompt("profile"),
        user=context,
        model=settings.model_smart,
        max_tokens=1500,
    )
    settings.profile_path.write_text(
        f"{HEADER}\n*Last rebuilt {today.isoformat()}*\n\n{body}\n"
    )
    return body
