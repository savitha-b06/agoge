"""Two-a-day support: import, today, adherence — end to end. No network, no LLM."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agoge.analysis import plan_adherence
from agoge.config import Athlete, settings
from agoge.db import DB
from agoge.nightly import _day_context, store_payload
from agoge.plan_import import import_plan
from agoge.plan_today import what_today

DAY = date(2026, 8, 11)
CSV = Path(__file__).resolve().parents[1] / "examples" / "plan.twoaday.csv"


def dual_payload():
    """COROS-style fixture: swim + strength on the same day."""
    return {
        "activities": [
            {
                "labelId": "swim-twoaday", "sportType": "poolSwim",
                "date": DAY.isoformat(), "totalTime": 48 * 60,
                "distance": 1500, "avgHr": 155, "maxHr": 168,
                "hrZones": {"z1": 200, "z2": 2000, "z3": 680},
            },
            {
                "labelId": "lift-twoaday", "sportType": "strength",
                "date": DAY.isoformat(), "totalTime": 70 * 60,
                "avgHr": 110, "maxHr": 140,
            },
        ],
        "daily": {"restingHr": 60, "hrv": 55},
        "sleep": {"totalSleep": 7.0 * 3600},
    }


def main():
    db_path = Path("/tmp/agoge_twoaday.db")
    db_path.unlink(missing_ok=True)
    db = DB(db_path)
    athlete = Athlete.load(settings.athlete_file)

    print("== import swim + strength same date ==")
    res = import_plan(
        db, athlete, CSV,
        from_day=DAY, reason="two-a-day fixture", today=DAY, version="v-2a",
    )
    assert res["rows_written"] == 2, res
    plans = db.plan_for_day(DAY.isoformat())
    sports = sorted(p["sport"] for p in plans)
    assert sports == ["strength", "swim"], sports
    print(f"  wrote {res['rows_written']}: {sports}")

    print("== re-import keeps both (clear once per date) ==")
    res2 = import_plan(
        db, athlete, CSV,
        from_day=DAY, reason="re-import", today=DAY, version="v-2a-b",
    )
    plans = db.plan_for_day(DAY.isoformat())
    assert len(plans) == 2
    assert all(p["version"] == "v-2a-b" for p in plans)
    print(f"  still {len(plans)} rows after re-import")

    print("== today describes both ==")
    db.upsert_daily(DAY.isoformat(), sleep_hours=7.0, hrv=58, resting_hr=60,
                    prehab_done=1)
    ans = what_today(db, athlete, DAY)
    assert len(ans["plans"]) == 2
    reply = ans["reply"]
    print(f"  {reply}")
    assert "Swim" in reply or "swim" in reply.lower()
    assert "Strength" in reply or "strength" in reply.lower()
    assert "Two-a-day" in reply or reply.lower().count("swim") + reply.lower().count("strength") >= 2

    print("== log COROS fixtures for both sports ==")
    store_payload(db, athlete, DAY, dual_payload())
    sessions = db.sessions_between(DAY.isoformat(), DAY.isoformat())
    assert {s["sport"] for s in sessions} == {"swim", "strength"}
    print(f"  logged: {sorted(s['sport'] for s in sessions)}")

    print("== adherence checks each by sport ==")
    adh = plan_adherence(db, athlete, DAY.isoformat())
    assert adh["checked"] is True
    checked = [s for s in adh["sessions"] if s.get("checked")]
    assert len(checked) == 2, adh["sessions"]
    by_sport = {s["plan"]["sport"]: s for s in checked}
    assert "swim" in by_sport and "strength" in by_sport
    # Swim: avg HR 155 vs 110-140 → 15 bpm over high (>8) → hr flag
    assert by_sport["swim"]["hr_flag"] is True
    # Strength: 70 min vs planned 50 → +40% (>25%) → duration flag; no HR targets
    assert by_sport["strength"]["duration_flag"] is True
    assert by_sport["strength"]["hr_flag"] is False
    print("  swim:", by_sport["swim"]["flags"])
    print("  strength:", by_sport["strength"]["flags"])
    assert any("[swim]" in f for f in adh["facts"])
    assert any("[strength]" in f for f in adh["facts"])

    ctx = _day_context(db, athlete, DAY)
    assert "Plan adherence:" in ctx
    assert "[swim]" in ctx and "[strength]" in ctx
    print("  nightly context tags both sports")

    print("\nOK — two-a-day path works end to end.")


if __name__ == "__main__":
    main()
