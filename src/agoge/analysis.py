"""The judgment layer: zone compliance, readiness, load ramp, checkpoints.

Everything here is deterministic arithmetic. No model calls. If the agent ever
tells you to back off, you can trace exactly why.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from .config import Athlete
from .db import DB


# ---------------------------------------------------------------- zone work

def zone_compliance(session: dict[str, Any], athlete: Athlete) -> float | None:
    """Fraction of session time actually spent in the target endurance zone.

    Prefers a real zone breakdown; falls back to average HR, which is a much
    weaker signal (an average of 127 can hide a lot of Z4)."""
    zb = session.get("zone_breakdown")
    if isinstance(zb, str):
        try:
            zb = json.loads(zb)
        except Exception:
            zb = None
    target = athlete.raw.get("target_endurance_zone", "z2")
    if isinstance(zb, dict) and zb:
        total = sum(v for v in zb.values() if v)
        if total:
            return round(100 * (zb.get(target, 0) or 0) / total, 1)
    avg = session.get("avg_hr")
    if avg:
        lo, hi = athlete.zone_bounds(target)
        return 100.0 if lo <= avg < hi else 0.0
    return None


# ---------------------------------------------------------------- readiness

def readiness(db: DB, athlete: Athlete, day: str) -> dict[str, Any]:
    """Green / amber / red, with the reasons attached.

    Deliberately conservative: any unresolved injury flag caps the score,
    because your programming is gated by joints, not by HRV."""
    row = db.day(day)
    d = dict(row) if row else {}
    score, reasons = 100, []

    hrv, hrv_base = d.get("hrv"), db.baseline("hrv", day)
    if hrv and hrv_base:
        delta = 100 * (hrv - hrv_base) / hrv_base
        if delta < -20:
            score -= 25; reasons.append(f"HRV {delta:.0f}% vs 28d baseline")
        elif delta < -10:
            score -= 12; reasons.append(f"HRV {delta:.0f}% vs baseline")

    rhr, rhr_base = d.get("resting_hr"), db.baseline("resting_hr", day)
    if rhr and rhr_base:
        delta = rhr - rhr_base
        if delta >= 7:
            score -= 20; reasons.append(f"resting HR +{delta:.0f} bpm")
        elif delta >= 4:
            score -= 8; reasons.append(f"resting HR +{delta:.0f} bpm")

    sleep = d.get("sleep_hours")
    if sleep is not None:
        if sleep < 5.5:
            score -= 20; reasons.append(f"slept {sleep:.1f}h")
        elif sleep < 6.5:
            score -= 8; reasons.append(f"slept {sleep:.1f}h")

    # Injury gates. These are hard.
    flags = injury_flags(db, athlete, day)
    for f in flags:
        if f["severity"] == "red":
            score = min(score, 35); reasons.append(f["message"])
        elif f["severity"] == "amber":
            score = min(score, 65); reasons.append(f["message"])

    score = max(0, min(100, score))
    flag = "green" if score >= 75 else "amber" if score >= 50 else "red"
    return {
        "score": score,
        "flag": flag,
        "reasons": reasons,
        "injury_flags": flags,
        "guidance": _guidance(flag, flags),
    }


def _guidance(flag: str, flags: list[dict[str, Any]]) -> str:
    if any(f["severity"] == "red" for f in flags):
        return ("Injury gate is open. No running today. Swim, easy spin, or rest. "
                "If this is the second day, that is a physio conversation, not a training one.")
    return {
        "green": "Train the session as planned.",
        "amber": "Train, but take the easy end of the range and stop if anything sharpens.",
        "red": "Recovery day. Walk, prehab, sleep.",
    }[flag]


def injury_flags(db: DB, athlete: Athlete, day: str) -> list[dict[str, Any]]:
    """The rule you wrote down and will otherwise forget at 6pm on a Tuesday:
    swelling that survives the night means the last session was too much."""
    out = []
    today = date.fromisoformat(day)
    for inj in athlete.injuries:
        key = inj["key"]
        since = (today - timedelta(days=7)).isoformat()
        rows = db.open_symptoms(key, since)
        if not rows:
            continue
        unresolved = [r for r in rows if r["overnight"] == 1 and not r["resolved_by"]]
        streak = len(unresolved)
        if streak >= int(inj.get("escalate_if_days", 2)):
            out.append({"injury": key, "severity": "red",
                        "message": f"{inj['label']}: symptoms persisting {streak} days"})
        elif streak >= 1:
            out.append({"injury": key, "severity": "amber",
                        "message": f"{inj['label']}: swelling present overnight"})
        else:
            recent = rows[0]
            if recent["day"] == day and (recent["severity"] or 0) >= 4:
                out.append({"injury": key, "severity": "amber",
                            "message": f"{inj['label']}: reported {recent['severity']}/10 today"})
    return out


# ---------------------------------------------------------------- load guard

def week_bounds(d: date) -> tuple[str, str]:
    start = d - timedelta(days=d.weekday())
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


def load_check(db: DB, athlete: Athlete, today: date | None = None) -> dict[str, Any]:
    """Enforces the ramp rule so a plan can never quietly jump 40%."""
    today = today or date.today()
    this_start, this_end = week_bounds(today)
    prev_start, prev_end = week_bounds(today - timedelta(days=7))
    this_min = db.weekly_minutes(this_start, this_end)
    prev_min = db.weekly_minutes(prev_start, prev_end)
    cap = prev_min * (1 + athlete.max_ramp_pct / 100) if prev_min else None
    pct = (100 * (this_min - prev_min) / prev_min) if prev_min else None
    return {
        "week_start": this_start,
        "this_week_min": round(this_min, 1),
        "last_week_min": round(prev_min, 1),
        "change_pct": round(pct, 1) if pct is not None else None,
        "cap_min": round(cap, 1) if cap else None,
        "headroom_min": round(cap - this_min, 1) if cap else None,
        "breach": bool(cap and this_min > cap),
    }


def gap_check(db: DB, today: date | None = None) -> dict[str, Any]:
    """Your miss-rules, automated: skip one, resume at 70% after two or three."""
    today = today or date.today()
    rows = db.sessions_between((today - timedelta(days=14)).isoformat(), today.isoformat())
    days = sorted({r["day"] for r in rows})
    if not days:
        return {"gap_days": None, "action": "no recent sessions on record"}
    gap = (today - date.fromisoformat(days[-1])).days
    if gap <= 1:
        action = "on track"
    elif gap <= 2:
        action = "one missed session — skip it, resume the next scheduled one, do not double up"
    else:
        action = "2+ days missed — resume at 70% volume, do not try to catch up"
    return {"gap_days": gap, "last_session": days[-1], "action": action}


# ---------------------------------------------------------------- race maths

def race_status(db: DB, athlete: Athlete, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    block = athlete.current_block(today)
    return {
        "days_to_race": athlete.days_to_race(today),
        "weeks_to_race": round(athlete.days_to_race(today) / 7, 1),
        "block": block["name"] if block else "unscheduled",
        "block_focus": block.get("focus") if block else None,
        "block_notes": block.get("notes") if block else None,
    }


def checkpoint_status(db: DB, athlete: Athlete, today: date | None = None) -> list[dict[str, Any]]:
    today = today or date.today()
    out = []
    for cp in athlete.checkpoints:
        metric, target = cp["metric"], cp["target"]
        actual = _measure(db, metric, today)
        out.append({
            "metric": metric,
            "target": target,
            "actual": actual,
            "due": str(cp["due"]),
            "days_left": (date.fromisoformat(str(cp["due"])) - today).days,
            "on_track": (actual is not None and actual >= target),
        })
    return out


def _measure(db: DB, metric: str, today: date) -> float | None:
    since = (today - timedelta(days=21)).isoformat()
    rows = db.sessions_between(since, today.isoformat())
    runs = [r for r in rows if r["sport"] == "run"]
    if metric == "long_run_min":
        return max((r["duration_min"] or 0 for r in runs), default=None) or None
    if metric == "long_run_distance_mi":
        return max((r["distance_mi"] or 0 for r in runs), default=None) or None
    if metric == "z2_pace_mph":
        paced = [r for r in runs if r["distance_mi"] and r["duration_min"]
                 and (r["z2_pct"] or 0) >= 70]
        if not paced:
            return None
        best = max(paced, key=lambda r: r["distance_mi"] / (r["duration_min"] / 60))
        return round(best["distance_mi"] / (best["duration_min"] / 60), 2)
    if metric == "prehab_streak_days":
        return db.prehab_streak(today.isoformat())
    return None


# ---------------------------------------------------------------- fuelling

def energy_availability(db: DB, athlete: Athlete, day: str) -> dict[str, Any]:
    """Energy availability = (intake - exercise cost) / kg fat-free mass.

    Deliberately built as a FLOOR ALARM, not a deficit scoreboard. It exists to
    catch under-fuelling on hard days, which is the failure mode that quietly
    destroys adaptation while looking like discipline from the inside. It does
    not tell you whether you hit a deficit target, and it never should.

    Thresholds follow the commonly cited sports-nutrition bands (~45 kcal/kg FFM
    optimal, <30 associated with endocrine and bone consequences). Those come
    from research largely in trained athletes, so treat them as a rough guide.
    """
    row = db.day(day)
    if not row or row["kcal_in"] is None:
        return {"available": False, "reason": "no intake data"}

    nutrition = athlete.raw.get("nutrition", {})
    bf = nutrition.get("body_fat_pct")
    weight = row["weight_lb"] or nutrition.get("weight_lb")
    if not (weight and bf):
        return {"available": False, "reason": "need weight and body_fat_pct"}

    ffm_kg = (weight * 0.453592) * (1 - bf / 100)
    kcal_out = row["kcal_out"] or _session_kcal(db, day, weight)
    ea = (row["kcal_in"] - kcal_out) / ffm_kg if ffm_kg else None
    if ea is None:
        return {"available": False, "reason": "could not compute"}

    floor = float(nutrition.get("ea_floor", 30))
    flag = "ok" if ea >= floor + 8 else "low" if ea >= floor else "critical"
    return {
        "available": True,
        "ea": round(ea, 1),
        "ffm_kg": round(ffm_kg, 1),
        "kcal_in": row["kcal_in"],
        "kcal_out": round(kcal_out),
        "floor": floor,
        "flag": flag,
        "message": {
            "ok": "",
            "low": f"Energy availability {ea:.0f} kcal/kg FFM — under-fuelled for "
                   f"the work done. Eat more today, particularly carbohydrate.",
            "critical": f"Energy availability {ea:.0f} kcal/kg FFM. This is well "
                        f"below the floor. Recovery, hormones, and bone all suffer "
                        f"here long before performance does. Eat more.",
        }[flag],
    }


def _session_kcal(db: DB, day: str, weight_lb: float) -> float:
    """Rough exercise cost when COROS calories are unavailable. Deliberately
    crude — it is an alarm input, not an accounting figure."""
    per_min = {"run": 0.095, "bike": 0.065, "swim": 0.080,
               "strength": 0.045, "walk": 0.035, "brick": 0.085, "other": 0.050}
    total = 0.0
    for s in db.sessions_between(day, day):
        total += (s["duration_min"] or 0) * per_min.get(s["sport"], 0.05) * weight_lb
    return total


def protein_status(db: DB, athlete: Athlete, day: str, window: int = 7) -> dict[str, Any]:
    """The one nutrition number worth tracking daily."""
    target = float(athlete.raw.get("nutrition", {}).get("protein_target_g", 0) or 0)
    if not target:
        return {"available": False}
    end = date.fromisoformat(day)
    rows = db.daily_between((end - timedelta(days=window - 1)).isoformat(), day)
    vals = [r["protein_g"] for r in rows if r["protein_g"] is not None]
    today_row = db.day(day)
    return {
        "available": True,
        "target": target,
        "today": today_row["protein_g"] if today_row else None,
        "avg": round(sum(vals) / len(vals), 1) if vals else None,
        "days_logged": len(vals),
        "days_hit": sum(1 for v in vals if v >= target),
        "window": window,
    }


def sleep_regularity(db: DB, day: str, window: int = 14) -> dict[str, Any]:
    """Standard deviation of sleep duration as a stand-in for regularity.

    Duration variability is a weaker proxy than midpoint variability — swap this
    for midpoint SD once bedtimes are being captured. Reported as a descriptive
    statistic with n attached, never as a finding.
    """
    end = date.fromisoformat(day)
    rows = db.daily_between((end - timedelta(days=window - 1)).isoformat(), day)
    vals = [r["sleep_hours"] for r in rows if r["sleep_hours"] is not None]
    if len(vals) < 5:
        return {"available": False, "n": len(vals),
                "reason": f"need at least 5 nights, have {len(vals)}"}
    mean = sum(vals) / len(vals)
    sd = (sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
    debt = sum(max(0.0, 8.0 - v) for v in vals[-7:])
    return {
        "available": True,
        "n": len(vals),
        "mean_hours": round(mean, 2),
        "sd_hours": round(sd, 2),
        "sleep_debt_7d": round(debt, 1),
        "note": "Descriptive only. Not enough nights to infer anything causal."
                if len(vals) < 40 else "Descriptive. Effect sizes need an experiment, not a correlation.",
    }
