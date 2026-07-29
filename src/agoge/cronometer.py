"""Cronometer ingest.

Runs the `cronosync` Go binary as a subprocess and parses the CSV it returns.
Deliberately arm's-length: cronosync is GPLv2, agoge is MIT, and they only ever
speak JSON over stdout.

Falls back to Cronometer's own CSV export, which cannot break.
"""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from .config import settings

# Cronometer's column headers carry units and drift between releases, so match
# on a prefix rather than an exact string.
NUTRIENT_COLUMNS = {
    "kcal_in": ["energy (kcal)", "energy", "calories"],
    "protein_g": ["protein (g)", "protein"],
    "carbs_g": ["carbs (g)", "carbohydrates", "net carbs (g)"],
    "fat_g": ["fat (g)", "fat"],
    "fiber_g": ["fiber (g)", "fibre (g)", "fiber"],
    "iron_mg": ["iron (mg)", "iron"],
    "calcium_mg": ["calcium (mg)", "calcium"],
    "sodium_mg": ["sodium (mg)", "sodium"],
    "potassium_mg": ["potassium (mg)", "potassium"],
    "vitamin_d_iu": ["vitamin d (iu)", "vitamin d"],
    "caffeine_mg": ["caffeine (mg)", "caffeine"],
    "water_g": ["water (g)", "water"],
}


class CronometerError(RuntimeError):
    pass


def fetch(start: date, end: date | None = None,
          binary: str | None = None) -> dict[str, Any]:
    """Run cronosync. Raises loudly — a silent failure here would record a day
    of zero calories, which is worse than no data at all."""
    end = end or start
    binary = binary or os.getenv("CRONOSYNC_BIN", "cronosync")
    env = dict(os.environ)
    if not env.get("CRONOMETER_USERNAME") or not env.get("CRONOMETER_PASSWORD"):
        raise CronometerError("CRONOMETER_USERNAME / CRONOMETER_PASSWORD not set")

    try:
        proc = subprocess.run(
            [binary, "-start", start.isoformat(), "-end", end.isoformat()],
            capture_output=True, text=True, timeout=120, env=env,
        )
    except FileNotFoundError:
        raise CronometerError(f"cronosync binary not found at '{binary}'")
    except subprocess.TimeoutExpired:
        raise CronometerError("cronosync timed out")

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        raise CronometerError(f"cronosync returned non-JSON: {proc.stderr[:400]}")

    if not payload.get("ok"):
        errs = payload.get("errors") or {"unknown": proc.stderr[:400]}
        raise CronometerError("; ".join(f"{k}: {v}" for k, v in errs.items()))
    return payload


def parse_daily_nutrition(raw_csv: str) -> dict[str, dict[str, float]]:
    """{'2026-07-28': {'kcal_in': 2410.0, 'protein_g': 172.3, ...}}"""
    out: dict[str, dict[str, float]] = {}
    reader = csv.DictReader(io.StringIO(raw_csv))
    if not reader.fieldnames:
        return out
    lowered = {(f or "").strip().lower(): f for f in reader.fieldnames}
    date_col = next((lowered[k] for k in lowered if k in ("date", "day")), None)
    if not date_col:
        return out

    resolved: dict[str, str] = {}
    for field, candidates in NUTRIENT_COLUMNS.items():
        for cand in candidates:
            hit = next((orig for low, orig in lowered.items() if low.startswith(cand)), None)
            if hit:
                resolved[field] = hit
                break

    for row in reader:
        day = (row.get(date_col) or "").strip()[:10]
        if not day:
            continue
        vals = {}
        for field, col in resolved.items():
            n = _num(row.get(col))
            if n is not None:
                vals[field] = n
        if vals:
            out[day] = vals
    return out


def parse_biometrics(raw_csv: str) -> dict[str, dict[str, float]]:
    """Cronometer biometrics are long-format: one row per metric per day."""
    out: dict[str, dict[str, float]] = {}
    reader = csv.DictReader(io.StringIO(raw_csv))
    if not reader.fieldnames:
        return out
    lowered = {(f or "").strip().lower(): f for f in reader.fieldnames}
    dc = next((lowered[k] for k in lowered if k in ("day", "date")), None)
    mc = next((lowered[k] for k in lowered if k in ("metric", "name", "biometric")), None)
    ac = next((lowered[k] for k in lowered if k in ("amount", "value")), None)
    uc = next((lowered[k] for k in lowered if k in ("unit", "units")), None)
    if not (dc and mc and ac):
        return out

    for row in reader:
        day = (row.get(dc) or "").strip()[:10]
        metric = (row.get(mc) or "").strip().lower()
        value = _num(row.get(ac))
        unit = (row.get(uc) or "").strip().lower() if uc else ""
        if not day or value is None:
            continue
        bucket = out.setdefault(day, {})
        if "weight" in metric:
            bucket["weight_lb"] = value * 2.20462 if unit.startswith("kg") else value
        elif "body fat" in metric or "bodyfat" in metric:
            bucket["body_fat_pct"] = value
    return out


def ingest(db, start: date, end: date | None = None,
           payload: dict[str, Any] | None = None) -> dict[str, int]:
    """Write parsed Cronometer data into the daily table."""
    payload = payload or fetch(start, end)
    exports = payload.get("exports", {})
    nutrition = parse_daily_nutrition(exports.get("nutrition", ""))
    bio = parse_biometrics(exports.get("biometrics", ""))

    days = set(nutrition) | set(bio)
    for day in days:
        fields = {**nutrition.get(day, {}), **bio.get(day, {})}
        fields.pop("body_fat_pct", None)  # not a daily column; informational only
        if fields:
            db.upsert_daily(day, **fields)
    return {"days": len(days), "nutrition_days": len(nutrition), "biometric_days": len(bio)}


def ingest_csv_file(db, path: str | Path, kind: str = "nutrition") -> int:
    """Fallback path: a CSV downloaded by hand from Cronometer's own export.
    This never breaks, and it is what you use when the GWT values go stale."""
    raw = Path(path).read_text()
    parsed = parse_daily_nutrition(raw) if kind == "nutrition" else parse_biometrics(raw)
    for day, fields in parsed.items():
        fields.pop("body_fat_pct", None)
        if fields:
            db.upsert_daily(day, **fields)
    return len(parsed)


def _num(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None
