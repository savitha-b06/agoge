"""Long-horizon fitness trend — deliberately separate from daily readiness.

Readiness answers "is today okay?" against a 28-day baseline.
This answers "is the training working?" on a timescale of months:
  - pace (or distance/time) at a fixed Z2 heart rate, week over week
  - multi-month slope on resting HR and HRV
  - VO2max trend where COROS reports it
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .config import Athlete
from .db import DB


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _linear_slope(xs: list[float], ys: list[float]) -> float | None:
    """Slope of ordinary least squares line. Returns None if under-determined."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return None
    return num / den


def z2_pace_trend(db: DB, athlete: Athlete, today: date | None = None,
                  weeks: int = 8) -> dict[str, Any]:
    """Trailing Z2 pace (mph) from sessions mostly in the target endurance zone.

    Groups by ISO week. Needs duration + distance + either z2_pct >= 70 or
    avg_hr inside the target zone.
    """
    today = today or date.today()
    since = today - timedelta(weeks=weeks)
    target = athlete.raw.get("target_endurance_zone", "z2")
    lo, hi = athlete.zone_bounds(target)
    rows = db.sessions_between(since.isoformat(), today.isoformat())
    by_week: dict[str, list[float]] = {}
    for r in rows:
        if not r["duration_min"] or not r["distance_mi"] or r["duration_min"] < 10:
            continue
        z2 = r["z2_pct"]
        avg = r["avg_hr"]
        in_zone = (z2 is not None and z2 >= 70) or (
            avg is not None and lo <= avg < hi
        )
        if not in_zone:
            continue
        # Prefer run for pace; bike power isn't here yet so bike is distance/time too
        if r["sport"] not in ("run", "bike", "walk"):
            continue
        mph = r["distance_mi"] / (r["duration_min"] / 60)
        ws = _week_start(date.fromisoformat(r["day"])).isoformat()
        by_week.setdefault(ws, []).append(mph)

    series = []
    for ws in sorted(by_week):
        vals = by_week[ws]
        series.append({
            "week_start": ws,
            "mph": round(sum(vals) / len(vals), 2),
            "n": len(vals),
            "sport_sample": "mixed",
        })

    slope = None
    if len(series) >= 3:
        xs = list(range(len(series)))
        ys = [p["mph"] for p in series]
        raw = _linear_slope(xs, ys)
        # mph per week
        slope = round(raw, 3) if raw is not None else None

    first = series[0]["mph"] if series else None
    last = series[-1]["mph"] if series else None
    return {
        "available": len(series) >= 2,
        "weeks": series,
        "first_mph": first,
        "last_mph": last,
        "delta_mph": round(last - first, 2) if first is not None and last is not None else None,
        "slope_mph_per_week": slope,
        "n_weeks": len(series),
        "note": (
            f"Z2 pace at ~{lo}-{hi} bpm moved from {first} to {last} mph "
            f"over {len(series)} weeks"
            if first is not None and last is not None and len(series) >= 2
            else "Need more Z2 sessions with distance to trend pace."
        ),
    }


def wellness_slope(db: DB, today: date | None = None,
                   days: int = 90) -> dict[str, Any]:
    """Multi-month slope on resting HR and HRV — slow signal, not daily delta."""
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat()
    rows = db.daily_between(since, today.isoformat())

    def _slope_for(col: str) -> dict[str, Any]:
        pts = [(i, float(r[col])) for i, r in enumerate(rows) if r[col] is not None]
        if len(pts) < 14:
            return {"available": False, "n": len(pts),
                    "reason": f"need ≥14 days of {col}, have {len(pts)}"}
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        daily = _linear_slope(xs, ys)
        per_week = daily * 7 if daily is not None else None
        return {
            "available": True,
            "n": len(pts),
            "first": ys[0],
            "last": ys[-1],
            "slope_per_week": round(per_week, 2) if per_week is not None else None,
            "delta": round(ys[-1] - ys[0], 1),
        }

    return {
        "window_days": days,
        "resting_hr": _slope_for("resting_hr"),
        "hrv": _slope_for("hrv"),
    }


def vo2_trend(db: DB, today: date | None = None,
              days: int = 120) -> dict[str, Any]:
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat()
    rows = [r for r in db.daily_between(since, today.isoformat())
            if r["vo2max"] is not None]
    if len(rows) < 2:
        return {"available": False, "n": len(rows),
                "reason": "COROS has not reported enough VO2max samples"}
    first, last = rows[0]["vo2max"], rows[-1]["vo2max"]
    return {
        "available": True,
        "n": len(rows),
        "first": first,
        "last": last,
        "delta": round(last - first, 1),
        "first_day": rows[0]["day"],
        "last_day": rows[-1]["day"],
    }


def fitness_trend(db: DB, athlete: Athlete,
                  today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    return {
        "as_of": today.isoformat(),
        "z2_pace": z2_pace_trend(db, athlete, today),
        "wellness": wellness_slope(db, today),
        "vo2max": vo2_trend(db, today),
    }


def format_fitness_trend(trend: dict[str, Any]) -> str:
    """Compact text for the weekly review context and revision export."""
    lines = ["Long-horizon fitness trend (not daily readiness):"]
    z2 = trend["z2_pace"]
    if z2["available"]:
        lines.append(f"  {z2['note']}")
        if z2["slope_mph_per_week"] is not None:
            direction = "building" if z2["slope_mph_per_week"] > 0 else "flat/declining"
            lines.append(f"  Slope: {z2['slope_mph_per_week']:+.3f} mph/week ({direction})")
    else:
        lines.append(f"  Z2 pace: {z2['note']}")

    well = trend["wellness"]
    rhr = well["resting_hr"]
    if rhr.get("available"):
        lines.append(
            f"  Resting HR over {well['window_days']}d: {rhr['first']:.0f} → "
            f"{rhr['last']:.0f} ({rhr['slope_per_week']:+.2f}/week)"
        )
    hrv = well["hrv"]
    if hrv.get("available"):
        lines.append(
            f"  HRV over {well['window_days']}d: {hrv['first']:.0f} → "
            f"{hrv['last']:.0f} ({hrv['slope_per_week']:+.2f}/week)"
        )
    vo2 = trend["vo2max"]
    if vo2.get("available"):
        lines.append(
            f"  VO2max: {vo2['first']} → {vo2['last']} "
            f"({vo2['first_day']} to {vo2['last_day']})"
        )
    else:
        lines.append(f"  VO2max: {vo2.get('reason', 'unavailable')}")
    return "\n".join(lines)
