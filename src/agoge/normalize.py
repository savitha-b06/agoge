"""Turn whatever shape COROS hands back into our session/daily rows.

COROS field names vary by endpoint and change between releases, so every lookup
goes through `pick`, which tries a list of candidate keys and gives up quietly.
When COROS renames something, you fix one list here.
"""
from __future__ import annotations

from typing import Any

M_PER_MILE = 1609.344
M_PER_FOOT = 0.3048

SPORT_MAP = {
    "run": "run", "indoorrun": "run", "trailrun": "run", "trackrun": "run",
    "bike": "bike", "indoorbike": "bike", "roadebike": "bike", "gravelroadbike": "bike",
    "poolswim": "swim", "openwater": "swim", "swim": "swim",
    "strength": "strength", "gymcardio": "other", "walk": "walk", "hike": "walk",
    "triathlon": "brick", "multisport": "brick",
}


def pick(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return default
        if k in d and d[k] not in (None, ""):
            return d[k]
        low = {str(x).lower().replace("_", ""): v for x, v in d.items()}
        kk = k.lower().replace("_", "")
        if kk in low and low[kk] not in (None, ""):
            return low[kk]
    return default


def normalize_sport(raw: Any) -> str:
    key = str(raw or "other").lower().replace("_", "").replace(" ", "")
    return SPORT_MAP.get(key, "other")


def normalize_session(raw: dict[str, Any], day: str) -> dict[str, Any]:
    sport = normalize_sport(pick(raw, "sportType", "sport", "type", "mode"))
    duration_s = _num(pick(raw, "totalTime", "duration", "movingTime", "elapsedTime"))
    distance_m = _num(pick(raw, "distance", "totalDistance"))
    return {
        "coros_id": str(pick(raw, "labelId", "activityId", "id", "workoutId", default="")) or None,
        "day": str(pick(raw, "date", "startDate", default=day))[:10],
        "sport": sport,
        "start_time": pick(raw, "startTime", "startTimestamp", "start"),
        "duration_min": round(duration_s / 60, 1) if duration_s else None,
        "distance_mi": round(distance_m / M_PER_MILE, 2) if distance_m else None,
        "avg_hr": _int(pick(raw, "avgHr", "averageHeartRate", "avgHeartRate", "hrAvg")),
        "max_hr": _int(pick(raw, "maxHr", "maxHeartRate", "hrMax")),
        "avg_cadence": _int(pick(raw, "avgCadence", "averageCadence", "cadenceAvg")),
        "elevation_ft": _ft(pick(raw, "elevGain", "totalAscent", "elevationGain")),
        "indoor": 1 if "indoor" in str(pick(raw, "sportType", "sport", default="")).lower() else 0,
        "zone_breakdown": _zones(raw),
        "source": "coros",
        "raw": raw,
    }


def normalize_daily(raw: dict[str, Any], sleep: dict[str, Any] | None = None,
                    fitness: dict[str, Any] | None = None) -> dict[str, Any]:
    sleep = sleep or {}
    fitness = fitness or {}
    sleep_s = _num(pick(sleep, "totalSleep", "sleepTime", "duration", "totalSleepTime"))
    return {
        "sleep_hours": round(sleep_s / 3600, 2) if sleep_s and sleep_s > 300 else _num(
            pick(sleep, "sleepHours", "hours")),
        "sleep_score": _int(pick(sleep, "sleepScore", "score", "quality")),
        "hrv": _int(pick(raw, "hrv", "avgHrv", "hrvValue") or pick(sleep, "hrv", "avgHrv")),
        "resting_hr": _int(pick(raw, "restingHr", "restHr", "rhr", "minHeartRate")),
        "steps": _int(pick(raw, "steps", "totalSteps", "stepCount")),
        "training_load": _num(pick(raw, "trainingLoad", "load", "weeklyLoad")),
        "stress": _int(pick(raw, "stress", "avgStress", "stressLevel")),
        "vo2max": _num(pick(fitness, "vo2Max", "vo2max", "runningVo2Max")),
        "raw": {"daily": raw, "sleep": sleep, "fitness": fitness},
    }


def _zones(raw: dict[str, Any]) -> dict[str, float] | None:
    z = pick(raw, "hrZones", "heartRateZones", "zones", "zoneDistribution")
    if not z:
        return None
    if isinstance(z, dict):
        return {str(k): _num(v) or 0 for k, v in z.items()}
    if isinstance(z, list):
        return {f"z{i+1}": _num(v if not isinstance(v, dict) else
                                pick(v, "time", "duration", "seconds")) or 0
                for i, v in enumerate(z)}
    return None


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return int(round(n)) if n is not None else None


def _ft(v: Any) -> float | None:
    n = _num(v)
    return round(n / M_PER_FOOT, 1) if n is not None else None
