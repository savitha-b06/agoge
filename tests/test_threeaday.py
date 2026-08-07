"""Three-a-day: bike + run + swim same date — today order + adherence by sport."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agoge.analysis import plan_adherence
from agoge.config import Athlete, settings
from agoge.db import DB
from agoge.nightly import store_payload
from agoge.plan_import import import_plan
from agoge.plan_today import what_today

DAY = date(2026, 8, 11)
CSV = Path(__file__).resolve().parents[1] / "examples" / "plan.threeaday.csv"


def triple_payload():
    return {
        "activities": [
            {
                "labelId": "bike-3", "sportType": "indoorBike",
                "date": DAY.isoformat(), "totalTime": 55 * 60,
                "distance": 20_000, "avgHr": 130, "maxHr": 145,
            },
            {
                "labelId": "run-3", "sportType": "indoorRun",
                "date": DAY.isoformat(), "totalTime": 42 * 60,
                "distance": 2 * 1609.344, "avgHr": 128, "maxHr": 140,
            },
            {
                "labelId": "swim-3", "sportType": "poolSwim",
                "date": DAY.isoformat(), "totalTime": 50 * 60,
                "distance": 1600, "avgHr": 120, "maxHr": 135,
            },
        ],
        "daily": {"restingHr": 60, "hrv": 55},
        "sleep": {"totalSleep": 7.0 * 3600},
    }


def main():
    db_path = Path("/tmp/agoge_threeaday.db")
    db_path.unlink(missing_ok=True)
    db = DB(db_path)
    athlete = Athlete.load(settings.athlete_file)

    print("== import bike, run, swim same date (CSV order) ==")
    res = import_plan(
        db, athlete, CSV,
        from_day=DAY, reason="three-a-day fixture", today=DAY, version="v-3a",
    )
    assert res["rows_written"] == 3, res
    plans = [dict(p) for p in db.plan_for_day(DAY.isoformat())]
    sports = [p["sport"] for p in plans]
    assert sports == ["bike", "run", "swim"], sports
    print(f"  wrote {res['rows_written']}: {sports}")

    print("== today lists all three in order ==")
    db.upsert_daily(DAY.isoformat(), sleep_hours=7.0, hrv=58, resting_hr=60,
                    prehab_done=1)
    ans = what_today(db, athlete, DAY)
    assert len(ans["plans"]) == 3
    reply = ans["reply"]
    print(f"  {reply}")
    assert "Three-a-day" in reply
    assert "(1)" in reply and "(2)" in reply and "(3)" in reply
    # Order must match CSV / plan_for_day: bike → run → swim
    i_bike = reply.lower().find("bike")
    i_run = reply.lower().find("run")
    i_swim = reply.lower().find("swim")
    assert 0 <= i_bike < i_run < i_swim, (i_bike, i_run, i_swim, reply)
    print("  ok")

    print("== adherence checks all three independently by sport ==")
    store_payload(db, athlete, DAY, triple_payload())
    sessions = db.sessions_between(DAY.isoformat(), DAY.isoformat())
    assert {s["sport"] for s in sessions} == {"bike", "run", "swim"}

    adh = plan_adherence(db, athlete, DAY.isoformat())
    assert adh["checked"] is True
    checked = [s for s in adh["sessions"] if s.get("checked")]
    assert len(checked) == 3, adh["sessions"]
    by_sport = {s["plan"]["sport"]: s for s in checked}
    assert set(by_sport) == {"bike", "run", "swim"}
    # Blank HR targets on all three — duration still checked; no HR flags.
    for sport, row in by_sport.items():
        assert row["hr_flag"] is False, (sport, row)
        assert row["checked"] is True
    # Swim was 50 vs planned 45 → within 25%; bike 55 vs 60 within; run 42 vs 40 within.
    assert by_sport["bike"]["ok"] and by_sport["run"]["ok"] and by_sport["swim"]["ok"]
    assert any("[bike]" in f for f in adh["facts"])
    assert any("[run]" in f for f in adh["facts"])
    assert any("[swim]" in f for f in adh["facts"])
    print("  facts:", adh["facts"])
    print("  ok")

    print("\nOK — three-a-day path works end to end.")


if __name__ == "__main__":
    main()
