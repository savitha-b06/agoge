"""Week-over-week progression: four cases that must not contradict each other.

1. on_track — actual increase matches the plan's built-in ramp
2. under_progressing — plan wanted growth, actual stayed flat
3. holding — red-light (knee overnight) — never called under-progressing
4. capped — load guard breach — not also under-progressing
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agoge.analysis import format_weekly_progression, weekly_progression
from agoge.config import Athlete, settings
from agoge.db import DB
from agoge.nightly import store_payload

DB_PATH = Path("/tmp/agoge_progression.db")


def fresh():
    DB_PATH.unlink(missing_ok=True)
    return DB(DB_PATH), Athlete.load(settings.athlete_file)


def _seed_week(db, athlete, week_monday: date, sessions: list[tuple[int, int]],
               plan_min_per_day: list[tuple[int, float]] | None = None):
    """sessions: list of (weekday_offset 0=Mon, minutes). plan_min_per_day similar."""
    for offset, mins in sessions:
        d = week_monday + timedelta(days=offset)
        store_payload(db, athlete, d, {
            "activities": [{
                "labelId": f"p-{d}-{mins}", "sportType": "indoorRun",
                "date": d.isoformat(), "totalTime": mins * 60,
                "distance": mins * 0.06 * 1609.344, "avgHr": 128, "maxHr": 140,
                "hrZones": {"z1": mins * 6, "z2": mins * 48, "z3": mins * 6},
            }],
            "daily": {"restingHr": 60, "hrv": 55},
            "sleep": {"totalSleep": 6.8 * 3600},
        })
        db.upsert_daily(d.isoformat(), sleep_hours=6.8, hrv=55, resting_hr=60,
                        prehab_done=1)
    if plan_min_per_day:
        for offset, mins in plan_min_per_day:
            d = week_monday + timedelta(days=offset)
            db.insert_plan_row({
                "day": d.isoformat(), "sport": "run", "session_type": "endurance",
                "planned_min": mins, "title": "Z2", "status": "planned",
                "source": "import", "version": "v-prog",
            })


def main():
    # Anchor Sunday = 2026-08-16 (week Mon 8/10 – Sun 8/16)
    sunday = date(2026, 8, 16)
    this_mon = date(2026, 8, 10)
    last_mon = date(2026, 8, 3)
    next_mon = date(2026, 8, 17)

    print("== 1. on_track: actual ramp matches plan ==")
    db, athlete = fresh()
    # Last week 100; this week 110 (+10%) — inside 15% cap and matches plan band
    _seed_week(db, athlete, last_mon,
               sessions=[(1, 40), (3, 30), (5, 30)],
               plan_min_per_day=[(1, 40), (3, 30), (5, 30)])
    _seed_week(db, athlete, this_mon,
               sessions=[(1, 40), (3, 35), (5, 35)],
               plan_min_per_day=[(1, 40), (3, 35), (5, 35)])
    # Next week plan 120
    _seed_week(db, athlete, next_mon, sessions=[],
               plan_min_per_day=[(1, 45), (3, 40), (5, 35)])
    p = weekly_progression(db, athlete, sunday)
    print(f"  status={p['status']} actual={p['actual_delta_pct']} planned={p['planned_delta_pct']}")
    print(f"  {p['message']}")
    print(f"  {p['next_week']['statement']}")
    assert p["status"] == "on_track", p
    assert not p["load"]["breach"]
    assert "under-progress" not in p["message"].lower()
    assert p["next_week"]["planned_min"] == 120
    assert "120 min" in p["next_week"]["statement"]
    print("  ok")

    print("== 2. under_progressing: plan grew, actual flat ==")
    db, athlete = fresh()
    _seed_week(db, athlete, last_mon,
               sessions=[(1, 40), (3, 30), (5, 30)],          # 100
               plan_min_per_day=[(1, 40), (3, 30), (5, 30)])  # 100
    _seed_week(db, athlete, this_mon,
               sessions=[(1, 40), (3, 30), (5, 30)],          # 100 flat
               plan_min_per_day=[(1, 50), (3, 35), (5, 30)])  # 115 +15%
    p = weekly_progression(db, athlete, sunday)
    print(f"  status={p['status']}: {p['message']}")
    assert p["status"] == "under_progressing", p
    assert "holding" not in p["status"]
    assert "capped" not in p["status"]
    print("  ok")

    print("== 3. holding: red-light knee — NOT under-progressing ==")
    db, athlete = fresh()
    # Same flat-vs-growth numbers as case 2 (would be under_progressing without red light)
    _seed_week(db, athlete, last_mon,
               sessions=[(1, 40), (3, 30), (5, 30)],
               plan_min_per_day=[(1, 40), (3, 30), (5, 30)])
    _seed_week(db, athlete, this_mon,
               sessions=[(1, 40), (3, 30), (5, 30)],
               plan_min_per_day=[(1, 50), (3, 35), (5, 30)])
    for back in (1, 0):
        d = (sunday - timedelta(days=back)).isoformat()
        db.add_symptom(day=d, injury_key="knee_r", severity=4, swelling=1,
                       overnight=1, note="still puffy past 24h")
    p = weekly_progression(db, athlete, sunday)
    print(f"  status={p['status']}: {p['message'][:160]}...")
    assert p["status"] == "holding", p
    assert p["red_light"]["active"]
    assert p["status"] != "under_progressing"
    assert "Correctly holding back" in p["message"]
    text = format_weekly_progression(p)
    assert "Status:  holding" in text
    assert "Status:  under_progressing" not in text
    print("  ok")

    print("== 4. capped: load breach — NOT under-progressing ==")
    db, athlete = fresh()
    # Last week 100; this week 140 (+40%) breaches 15% cap (115).
    # Plan also wanted growth so without cap it might look mixed — cap wins.
    _seed_week(db, athlete, last_mon,
               sessions=[(1, 40), (3, 30), (5, 30)],
               plan_min_per_day=[(1, 40), (3, 30), (5, 30)])
    _seed_week(db, athlete, this_mon,
               sessions=[(1, 50), (3, 45), (5, 45)],          # 140
               plan_min_per_day=[(1, 50), (3, 40), (5, 40)])  # 130 planned
    p = weekly_progression(db, athlete, sunday)
    print(f"  status={p['status']}: {p['message']}")
    assert p["status"] == "capped", p
    assert p["load"]["breach"] is True
    assert p["status"] != "under_progressing"
    assert p["status"] != "holding"
    assert "Status:  under_progressing" not in format_weekly_progression(p)
    print("  ok")

    print("== 5. insufficient_data: both actual weeks zero ==")
    db, athlete = fresh()
    # Plan exists both weeks but nothing logged
    _seed_week(db, athlete, last_mon, sessions=[],
               plan_min_per_day=[(1, 40), (3, 30)])
    _seed_week(db, athlete, this_mon, sessions=[],
               plan_min_per_day=[(1, 45), (3, 35)])
    p = weekly_progression(db, athlete, sunday)
    print(f"  status={p['status']}: {p['message']}")
    assert p["status"] == "insufficient_data", p
    assert p["status"] != "on_track"
    assert "Not the same as on track" in p["message"]
    assert "Status:  on_track" not in format_weekly_progression(p)
    print("  ok")

    print("== 6. insufficient_data: planned last week was zero ==")
    db, athlete = fresh()
    _seed_week(db, athlete, last_mon,
               sessions=[(1, 40), (3, 30)],
               plan_min_per_day=[])  # no plan last week
    _seed_week(db, athlete, this_mon,
               sessions=[(1, 45), (3, 35)],
               plan_min_per_day=[(1, 45), (3, 35)])
    p = weekly_progression(db, athlete, sunday)
    print(f"  status={p['status']}: {p['message']}")
    assert p["status"] == "insufficient_data", p
    assert p["planned_delta_pct"] is None
    assert "uncomputable" in p["message"]
    assert p["status"] != "on_track"
    print("  ok")

    print("== messaging never contradicts across cases ==")
    print("  case matrix: on_track / under_progressing / holding / capped / "
          "insufficient_data — exclusive")
    print("\nOK — weekly progression classifications are consistent.")


if __name__ == "__main__":
    main()
