"""Empty-plan day must never say 'train as planned'. Also: gap cut, between blocks."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agoge.config import Athlete, settings
from agoge.db import DB
from agoge.plan_today import what_today

DB_PATH = Path("/tmp/agoge_empty_plan.db")


def fresh():
    DB_PATH.unlink(missing_ok=True)
    return DB(DB_PATH), Athlete.load(settings.athlete_file)


def main():
    db, athlete = fresh()

    print("== empty plan, green readiness — no 'train as planned' ==")
    day = date(2026, 8, 19)  # inside Austin block, no plan row
    db.upsert_daily(day.isoformat(), sleep_hours=7.0, hrv=58, resting_hr=60,
                    prehab_done=1)
    ans = what_today(db, athlete, day)
    print(f"  {ans['reply']}")
    assert ans["plans"] == []
    assert "Nothing prescribed" in ans["reply"]
    assert "Readiness is" in ans["reply"]
    assert "as planned" not in ans["reply"].lower()
    assert "Train the session" not in ans["reply"]
    print("  ok")

    print("== empty plan, red injury — guidance ok, still no 'as planned' ==")
    db, athlete = fresh()
    day = date(2026, 8, 19)
    for back in (1, 0):
        d = (day - timedelta(days=back)).isoformat()
        db.add_symptom(day=d, injury_key="knee_r", severity=4, swelling=1,
                       overnight=1, note="puffy")
    db.upsert_daily(day.isoformat(), sleep_hours=6.0, hrv=45, resting_hr=68)
    ans = what_today(db, athlete, day)
    print(f"  {ans['reply'][:160]}...")
    assert "as planned" not in ans["reply"].lower()
    assert ans["readiness"]["flag"] == "red"
    assert "Injury gate" in ans["reply"] or "No running" in ans["reply"]
    print("  ok")

    print("== between blocks + plan starts later — routine continues ==")
    db, athlete = fresh()
    # Gap in athlete.yaml: Aerobic development ends 2026-12-31, Spain starts 2027-01-05
    gap_day = date(2027, 1, 2)
    db.upsert_daily(gap_day.isoformat(), sleep_hours=7.0, hrv=55, resting_hr=60)
    db.insert_plan_row({
        "day": "2027-01-05", "sport": "swim", "session_type": "endurance",
        "planned_min": 40, "title": "Spain start", "status": "planned",
        "source": "import", "version": "v-gap",
    })
    assert athlete.current_block(gap_day) is None
    assert athlete.previous_block(gap_day) is not None
    ans = what_today(db, athlete, gap_day)
    print(f"  {ans['reply']}")
    assert "Between blocks" in ans["reply"]
    assert "current routine continues" in ans["reply"].lower()
    assert "as planned" not in ans["reply"].lower()
    assert "imported plan begins 2027-01-05" in ans["reply"]
    print("  ok")

    print("== 70% cut: endurance yes, strength no ==")
    db, athlete = fresh()
    day = date(2026, 8, 20)
    # Force gap_days >= 3: last session 4 days ago
    from agoge.nightly import store_payload
    last = day - timedelta(days=4)
    store_payload(db, athlete, last, {
        "activities": [{
            "labelId": "old", "sportType": "indoorRun", "date": last.isoformat(),
            "totalTime": 30 * 60, "distance": 2 * 1609.344, "avgHr": 125,
        }],
        "daily": {"restingHr": 60}, "sleep": {"totalSleep": 6.5 * 3600},
    })
    db.clear_planned_day(day.isoformat())
    db.insert_plan_row({
        "day": day.isoformat(), "sport": "run", "session_type": "endurance",
        "planned_min": 40, "title": "Z2", "status": "planned",
        "source": "import", "version": "v-g",
    })
    db.insert_plan_row({
        "day": day.isoformat(), "sport": "strength", "session_type": "strength",
        "planned_min": 50, "lift_focus": "lower", "title": "lift",
        "status": "planned", "source": "import", "version": "v-g",
    })
    db.upsert_daily(day.isoformat(), sleep_hours=7.0, hrv=58, resting_hr=60)
    ans = what_today(db, athlete, day)
    print(f"  {ans['reply']}")
    run_eff = next(e for e in ans["effective_plans"] if e["sport"] == "run")
    lift_eff = next(e for e in ans["effective_plans"] if e["sport"] == "strength")
    assert run_eff["planned_min"] == 28.0, run_eff  # 70% of 40
    assert lift_eff["planned_min"] == 50.0, lift_eff  # untouched
    assert "endurance volume cut" in ans["reply"]
    print("  ok")

    print("\nOK — empty-plan / continuity / gap-cut rules hold.")


if __name__ == "__main__":
    main()
