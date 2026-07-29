"""Sunday: review the week, plan the next one, rebuild the profile."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .analysis import (checkpoint_status, gap_check, injury_flags, load_check,
                       plan_divergence, race_status, week_bounds)
from .config import Athlete, settings
from .db import DB
from .fitness import fitness_trend, format_fitness_trend
from .llm import complete, load_prompt
from .profile import read_profile, update_profile


def run(today: date | None = None, rebuild_profile: bool = True) -> dict[str, Any]:
    today = today or date.today()
    athlete = Athlete.load(settings.athlete_file)
    db = DB(settings.db_path)

    ctx = _weekly_context(db, athlete, today)
    body = complete(system=load_prompt("weekly"), user=ctx,
                    model=settings.model_smart, max_tokens=2000)
    db.add_note(today.isoformat(), "weekly", body, settings.model_smart)

    out: dict[str, Any] = {"report": body, "context": ctx}
    if rebuild_profile:
        out["profile"] = update_profile(db, athlete, today)
    return out


def _weekly_context(db: DB, athlete: Athlete, today: date) -> str:
    this_start, this_end = week_bounds(today)
    prev_start, prev_end = week_bounds(today - timedelta(days=7))
    load = load_check(db, athlete, today)
    race = race_status(db, athlete, today)

    lines = [
        f"Sunday review for week of {this_start}.",
        f"Block: {race['block']} — focus {race['block_focus']}. {race['block_notes']}",
        f"{race['days_to_race']} days ({race['weeks_to_race']} weeks) to race.",
        "",
        "LOAD BUDGET FOR NEXT WEEK — this is a hard constraint:",
        f"  This week completed: {load['this_week_min']:.0f} min",
        f"  Last week: {load['last_week_min']:.0f} min",
        f"  Max ramp: {athlete.max_ramp_pct}% per week",
        f"  Next week must not exceed: "
        f"{load['this_week_min'] * (1 + athlete.max_ramp_pct / 100):.0f} min total",
        "",
    ]

    for label, (s, e) in (("This week", (this_start, this_end)),
                          ("Last week", (prev_start, prev_end))):
        lines.append(f"{label} sessions:")
        rows = db.sessions_between(s, e)
        if not rows:
            lines.append("  none")
        for r in rows:
            bits = [r["day"], r["sport"]]
            if r["duration_min"]:
                bits.append(f"{r['duration_min']:.0f}min")
            if r["distance_mi"]:
                bits.append(f"{r['distance_mi']:.2f}mi")
            if r["avg_hr"]:
                bits.append(f"HR {r['avg_hr']}")
            if r["z2_pct"] is not None:
                bits.append(f"Z2 {r['z2_pct']:.0f}%")
            lines.append("  " + "  ".join(bits))
        lines.append("")

    lines.append("Wellness this week:")
    for d in db.daily_between(this_start, this_end):
        bits = [d["day"]]
        for label, key, fmt in (("sleep", "sleep_hours", "{:.1f}h"), ("HRV", "hrv", "{:.0f}"),
                                ("RHR", "resting_hr", "{:.0f}"),
                                ("readiness", "readiness", "{:.0f}")):
            if d[key] is not None:
                bits.append(f"{label} {fmt.format(d[key])}")
        if d["prehab_done"] is not None:
            bits.append("prehab " + ("y" if d["prehab_done"] else "N"))
        lines.append("  " + "  ".join(bits))

    flags = injury_flags(db, athlete, today.isoformat())
    lines += ["", "Injury flags: " + (
        "; ".join(f"[{f['severity']}] {f['message']}" for f in flags) if flags else "none open")]
    for inj in athlete.injuries:
        lines.append(f"  Rule — {inj['label']}: {inj['rule'].strip()}")

    lines += ["", f"Consistency: {gap_check(db, today)['action']}", "", "Checkpoints:"]
    for c in checkpoint_status(db, athlete, today):
        lines.append(f"  {c['metric']}: {c['actual']} / {c['target']} due {c['due']} "
                     f"({c['days_left']}d) {'on track' if c['on_track'] else 'BEHIND'}")

    # Long-horizon fitness + sustained plan divergence (Phase 1.5)
    lines += ["", format_fitness_trend(fitness_trend(db, athlete, today))]
    div = plan_divergence(db, athlete, today)
    lines += ["", "Plan vs reality:"]
    for w in div["weeks"]:
        pct = f"{w['pct_of_plan']:.0f}%" if w["pct_of_plan"] is not None else "n/a"
        lines.append(
            f"  week of {w['week_start']}: planned {w['planned_min']:.0f} min, "
            f"actual {w['actual_min']:.0f} min ({pct})"
        )
    if div["message"]:
        lines.append(f"  DIVERGENCE: {div['message']}")
        if div["suggest_revision"]:
            lines.append(
                "  Observation only — surface this; do not auto-revise the plan. "
                "A structural revision needs a deliberate re-import."
            )

    next_start = (date.fromisoformat(this_end) + timedelta(days=1)).isoformat()
    next_end = (date.fromisoformat(this_end) + timedelta(days=7)).isoformat()
    upcoming = db.plan_between(next_start, next_end)
    if upcoming:
        lines += ["", "Imported plan for next week (hard gates still apply):"]
        for p in upcoming:
            bits = [p["day"], p["sport"] or "?", p["session_type"] or ""]
            if p["planned_min"]:
                bits.append(f"{p['planned_min']:.0f}min")
            if p["title"]:
                bits.append(p["title"])
            lines.append("  " + "  ".join(str(b) for b in bits if b))

    profile = read_profile()
    lines += ["", "Standing profile:", profile or "(none yet)"]
    return "\n".join(lines)
