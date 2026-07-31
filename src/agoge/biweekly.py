"""Biweekly deep review — every other Sunday of the current block.

Cadence is (days since block start) % 14, evaluated inside the existing
Sunday weekly job. No second cron entry.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .analysis import checkpoint_status
from .config import Athlete, settings
from .db import DB
from .llm import complete, load_prompt

KM_PER_MILE = 1.609344

# Example / default fixed baseline when athlete.yaml has no benchmarks block.
# Matches the Jul 28, 2026 treadmill session called out in the product brief.
DEFAULT_BENCHMARK = {
    "key": "treadmill_z2_baseline",
    "date": "2026-07-28",
    "sport": "run",
    "avg_hr": 129,
    "pace_per_km": "11:05",
    "label": "Jul 28, 2026 treadmill Z2",
}


def is_biweekly_deep_review_day(athlete: Athlete, today: date) -> bool:
    """True on every other Sunday of the current block.

    Uses days-since-block-start mod 14: the Sunday that falls in the 0..6 half
    of each fortnight fires. Requires at least one full fortnight elapsed so
    the 14-vs-14 comparison has a prior window.
    """
    block = athlete.current_block(today)
    if not block:
        return False
    start = _as_date(block["start"])
    days = (today - start).days
    if days < 14:
        return False
    # Piggyback the Sunday weekly job only.
    if today.weekday() != 6:
        return False
    return (days % 14) < 7


def run(db: DB, athlete: Athlete, today: date | None = None,
        force: bool = False) -> dict[str, Any] | None:
    """Generate the biweekly deep review, or return None if today is not due."""
    today = today or date.today()
    if not force and not is_biweekly_deep_review_day(athlete, today):
        return None

    metrics = compute_biweekly_metrics(db, athlete, today)
    ctx = format_biweekly_context(metrics, athlete, today)
    body = complete(
        system=load_prompt("biweekly"),
        user=ctx,
        model=settings.model_smart,
        max_tokens=2500,
    )
    db.add_note(today.isoformat(), "biweekly", body, settings.model_smart)
    return {"report": body, "context": ctx, "metrics": metrics}


# ---------------------------------------------------------------- metrics

def compute_biweekly_metrics(db: DB, athlete: Athlete,
                             today: date) -> dict[str, Any]:
    """Last 14 days vs the 14 before that — all deterministic."""
    # Windows are inclusive end-dated at today / today-14.
    curr_end = today
    curr_start = today - timedelta(days=13)
    prev_end = today - timedelta(days=14)
    prev_start = today - timedelta(days=27)

    curr = _window_stats(db, athlete, curr_start, curr_end)
    prev = _window_stats(db, athlete, prev_start, prev_end)
    bench = _benchmark_comparison(db, athlete, curr_start, curr_end)
    checkpoints = checkpoint_status(db, athlete, today)

    block = athlete.current_block(today)
    days_into_block = (
        (today - _as_date(block["start"])).days if block else None
    )
    return {
        "as_of": today.isoformat(),
        "block": block["name"] if block else None,
        "days_into_block": days_into_block,
        "current_window": {
            "start": curr_start.isoformat(),
            "end": curr_end.isoformat(),
            **curr,
        },
        "prior_window": {
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
            **prev,
        },
        "deltas": _deltas(curr, prev),
        "benchmark": bench,
        "checkpoints": checkpoints,
    }


def _window_stats(db: DB, athlete: Athlete,
                  start: date, end: date) -> dict[str, Any]:
    s, e = start.isoformat(), end.isoformat()
    sessions = db.sessions_between(s, e)
    daily = db.daily_between(s, e)
    plan = db.plan_between(s, e)

    planned = [
        p for p in plan
        if (p["session_type"] or "").lower() != "rest"
        and (p["sport"] or "").lower() != "rest"
        and (p["planned_min"] or 0) > 0
    ]
    completed_planned = 0
    missed = []
    for p in planned:
        sport = (p["sport"] or "").lower()
        day_sessions = [x for x in sessions if x["day"] == p["day"]]
        if sport and sport not in ("other",):
            matched = [x for x in day_sessions if x["sport"] == sport]
        else:
            matched = day_sessions
        if matched:
            completed_planned += 1
        elif p["status"] not in ("done", "skipped", "cancelled"):
            missed.append({
                "day": p["day"],
                "sport": p["sport"],
                "title": p["title"],
                "planned_min": p["planned_min"],
            })

    completion_rate = (
        round(100 * completed_planned / len(planned), 1) if planned else None
    )

    z2_vals = [r["z2_pct"] for r in sessions if r["z2_pct"] is not None]
    mins = sum(r["duration_min"] or 0 for r in sessions)
    miles = sum(r["distance_mi"] or 0 for r in sessions
                if r["sport"] == "run")

    def _mean(rows, key):
        vals = [r[key] for r in rows if r[key] is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    return {
        "sessions": len(sessions),
        "total_min": round(mins, 1),
        "run_miles": round(miles, 2),
        "planned_sessions": len(planned),
        "completed_planned": completed_planned,
        "completion_rate_pct": completion_rate,
        "missed_planned": missed,
        "mean_z2_pct": round(sum(z2_vals) / len(z2_vals), 1) if z2_vals else None,
        "z2_sessions_n": len(z2_vals),
        "mean_sleep_h": _mean(daily, "sleep_hours"),
        "mean_hrv": _mean(daily, "hrv"),
        "mean_resting_hr": _mean(daily, "resting_hr"),
        "sleep_n": sum(1 for r in daily if r["sleep_hours"] is not None),
        "hrv_n": sum(1 for r in daily if r["hrv"] is not None),
        "rhr_n": sum(1 for r in daily if r["resting_hr"] is not None),
    }


def _deltas(curr: dict[str, Any], prev: dict[str, Any]) -> dict[str, Any]:
    def d(key):
        a, b = curr.get(key), prev.get(key)
        if a is None or b is None:
            return None
        return round(a - b, 2)

    return {
        "completion_rate_pct": d("completion_rate_pct"),
        "mean_z2_pct": d("mean_z2_pct"),
        "mean_sleep_h": d("mean_sleep_h"),
        "mean_hrv": d("mean_hrv"),
        "mean_resting_hr": d("mean_resting_hr"),
        "total_min": d("total_min"),
        "run_miles": d("run_miles"),
    }


def _benchmark_comparison(db: DB, athlete: Athlete,
                          start: date, end: date) -> dict[str, Any]:
    """Compare any near-HR run in the window against the fixed treadmill baseline."""
    bench = _resolve_benchmark(athlete)
    if not bench:
        return {"available": False, "reason": "no benchmark configured"}

    target_hr = int(bench["avg_hr"])
    hr_tol = int(bench.get("hr_tolerance", 5))
    baseline_sec_km = _pace_to_sec_per_km(bench["pace_per_km"])
    baseline_mph = _sec_per_km_to_mph(baseline_sec_km)

    candidates = []
    for r in db.sessions_between(start.isoformat(), end.isoformat()):
        if (r["sport"] or "") != (bench.get("sport") or "run"):
            continue
        if not r["avg_hr"] or not r["distance_mi"] or not r["duration_min"]:
            continue
        if r["duration_min"] < 15:
            continue
        if abs(int(r["avg_hr"]) - target_hr) > hr_tol:
            continue
        sec_km = _session_sec_per_km(r["distance_mi"], r["duration_min"])
        mph = r["distance_mi"] / (r["duration_min"] / 60)
        candidates.append({
            "day": r["day"],
            "avg_hr": r["avg_hr"],
            "duration_min": r["duration_min"],
            "distance_mi": r["distance_mi"],
            "pace_per_km": _format_pace(sec_km),
            "pace_sec_per_km": round(sec_km, 1),
            "mph": round(mph, 2),
            "delta_sec_per_km": round(sec_km - baseline_sec_km, 1),
            "faster": sec_km < baseline_sec_km,
        })

    if not candidates:
        return {
            "available": False,
            "reason": (
                f"No {bench.get('sport', 'run')} session in window with "
                f"avg HR {target_hr}±{hr_tol} and distance logged"
            ),
            "baseline": _baseline_public(bench, baseline_sec_km, baseline_mph),
        }

    # Best (fastest pace) near-HR match for a clean head-to-head.
    best = min(candidates, key=lambda c: c["pace_sec_per_km"])
    return {
        "available": True,
        "baseline": _baseline_public(bench, baseline_sec_km, baseline_mph),
        "match": best,
        "all_matches": candidates,
        "verdict": (
            f"At ~{best['avg_hr']} bpm on {best['day']}: "
            f"{best['pace_per_km']}/km vs baseline {_format_pace(baseline_sec_km)}/km "
            f"({best['delta_sec_per_km']:+.0f}s/km, "
            f"{'faster' if best['faster'] else 'slower'})."
        ),
    }


def _resolve_benchmark(athlete: Athlete) -> dict[str, Any] | None:
    benches = athlete.raw.get("benchmarks") or []
    if benches:
        b = dict(benches[0])
        if "pace_per_km" not in b and "pace_min_per_km" in b:
            b["pace_per_km"] = b["pace_min_per_km"]
        return b
    return dict(DEFAULT_BENCHMARK)


def _baseline_public(bench: dict[str, Any], sec_km: float,
                     mph: float) -> dict[str, Any]:
    return {
        "key": bench.get("key"),
        "label": bench.get("label") or bench.get("key"),
        "date": str(bench.get("date")),
        "avg_hr": bench["avg_hr"],
        "pace_per_km": _format_pace(sec_km),
        "mph": round(mph, 2),
    }


def _session_sec_per_km(distance_mi: float, duration_min: float) -> float:
    km = distance_mi * KM_PER_MILE
    return (duration_min * 60) / km if km else float("inf")


def _pace_to_sec_per_km(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if ":" in s:
        mins, secs = s.split(":", 1)
        return int(mins) * 60 + float(secs)
    return float(s)


def _sec_per_km_to_mph(sec_per_km: float) -> float:
    # miles per hour from seconds per km
    return 3600 / (sec_per_km * KM_PER_MILE)


def _format_pace(sec_per_km: float) -> str:
    sec_per_km = int(round(sec_per_km))
    return f"{sec_per_km // 60}:{sec_per_km % 60:02d}"


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


# ---------------------------------------------------------------- context for LLM

def format_biweekly_context(metrics: dict[str, Any], athlete: Athlete,
                            today: date) -> str:
    curr = metrics["current_window"]
    prev = metrics["prior_window"]
    d = metrics["deltas"]
    lines = [
        f"Biweekly deep review for {athlete.name} as of {today.isoformat()}.",
        f"Block: {metrics['block']} (day {metrics['days_into_block']} of block).",
        f"Current 14d: {curr['start']} → {curr['end']}",
        f"Prior 14d:    {prev['start']} → {prev['end']}",
        "",
        "SESSION COMPLETION (against imported/prescribed plan):",
        f"  Current: {curr['completed_planned']}/{curr['planned_sessions']} "
        f"({_pct(curr['completion_rate_pct'])}) — "
        f"{curr['sessions']} total sessions logged, "
        f"{curr['total_min']:.0f} min, {curr['run_miles']:.2f} run miles",
        f"  Prior:   {prev['completed_planned']}/{prev['planned_sessions']} "
        f"({_pct(prev['completion_rate_pct'])}) — "
        f"{prev['sessions']} sessions, {prev['total_min']:.0f} min, "
        f"{prev['run_miles']:.2f} run miles",
        f"  Delta completion: {_delta(d['completion_rate_pct'], 'pp')}",
    ]
    if curr["missed_planned"]:
        lines.append("  Missed in current window:")
        for m in curr["missed_planned"][:8]:
            lines.append(
                f"    {m['day']} {m['sport'] or '?'} "
                f"{m['title'] or ''} ({m['planned_min'] or '?'} min)"
            )

    lines += [
        "",
        "ZONE COMPLIANCE (mean % time in target endurance zone):",
        f"  Current: {_pct(curr['mean_z2_pct'])} across {curr['z2_sessions_n']} sessions",
        f"  Prior:   {_pct(prev['mean_z2_pct'])} across {prev['z2_sessions_n']} sessions",
        f"  Delta:   {_delta(d['mean_z2_pct'], 'pp')}",
        "",
        "WELLNESS TRENDS (daily means):",
        f"  Sleep:  {_num(curr['mean_sleep_h'], 'h')} (n={curr['sleep_n']}) vs "
        f"{_num(prev['mean_sleep_h'], 'h')} prior — delta {_delta(d['mean_sleep_h'], 'h')}",
        f"  HRV:    {_num(curr['mean_hrv'])} (n={curr['hrv_n']}) vs "
        f"{_num(prev['mean_hrv'])} prior — delta {_delta(d['mean_hrv'])}",
        f"  Resting HR: {_num(curr['mean_resting_hr'], ' bpm')} (n={curr['rhr_n']}) vs "
        f"{_num(prev['mean_resting_hr'], ' bpm')} prior — "
        f"delta {_delta(d['mean_resting_hr'], ' bpm')}",
        "",
        "CHECKPOINTS:",
    ]
    for c in metrics["checkpoints"]:
        lines.append(
            f"  {c['metric']}: {c['actual']} / {c['target']} due {c['due']} "
            f"({c['days_left']}d left) — "
            f"{'on track' if c['on_track'] else 'BEHIND'}"
        )
    if not metrics["checkpoints"]:
        lines.append("  (none configured)")

    bench = metrics["benchmark"]
    lines += ["", "BENCHMARK (fixed baseline comparison):"]
    bl = bench.get("baseline") or {}
    if bl:
        lines.append(
            f"  Baseline: {bl.get('label')} on {bl.get('date')} — "
            f"{bl.get('avg_hr')} bpm @ {bl.get('pace_per_km')}/km "
            f"({bl.get('mph')} mph)"
        )
    if bench.get("available"):
        lines.append(f"  {bench['verdict']}")
        if len(bench.get("all_matches") or []) > 1:
            lines.append(f"  ({len(bench['all_matches'])} near-HR matches in window; "
                         f"fastest used above)")
    else:
        lines.append(f"  No match: {bench.get('reason')}")

    lines += [
        "",
        "Write the deep review from these numbers only. "
        "Do not invent values. Credit only what the numbers support.",
    ]
    return "\n".join(lines)


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:.0f}%"


def _num(v, suffix: str = "") -> str:
    return "n/a" if v is None else f"{v}{suffix}"


def _delta(v, suffix: str = "") -> str:
    if v is None:
        return "n/a"
    sign = "+" if v > 0 else ""
    return f"{sign}{v}{suffix}"
