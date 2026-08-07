"""Status guidance must respect an active gap — not just readiness colour.

Green + 2+ day gap used to say "train as planned" above Consistency's
"resume at 70%" line. Those two lines must not contradict.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agoge.analysis import gap_check, readiness
from agoge.config import Athlete, settings
from agoge.db import DB
from agoge.nightly import store_payload

DB_PATH = Path("/tmp/agoge_status_gap.db")


def fresh():
    DB_PATH.unlink(missing_ok=True)
    return DB(DB_PATH), Athlete.load(settings.athlete_file)


def main():
    db, athlete = fresh()
    day = date(2026, 8, 20)

    # Last session 4 days ago → gap_days >= 3 (reduced-volume return).
    last = day - timedelta(days=4)
    store_payload(db, athlete, last, {
        "activities": [{
            "labelId": "old", "sportType": "indoorRun", "date": last.isoformat(),
            "totalTime": 30 * 60, "distance": 2 * 1609.344, "avgHr": 125,
        }],
        "daily": {"restingHr": 60}, "sleep": {"totalSleep": 6.5 * 3600},
    })
    # Healthy metrics → green readiness.
    db.upsert_daily(day.isoformat(), sleep_hours=7.0, hrv=58, resting_hr=60)

    gap = gap_check(db, day)
    r = readiness(db, athlete, day.isoformat())

    print("== green readiness + active 2+ day gap ==")
    print(f"  readiness: {r['score']}/100 ({r['flag']})")
    print(f"  guidance:  {r['guidance']}")
    print(f"  consistency: {gap['action']}")

    assert r["flag"] == "green", r
    assert gap["gap_days"] is not None and gap["gap_days"] >= 3, gap
    assert "70%" in gap["action"]
    assert "as planned" not in r["guidance"].lower()
    assert "Train the session" not in r["guidance"]
    # Top-line guidance and Consistency must agree on the reduced-volume return.
    assert "70%" in r["guidance"]
    assert r["guidance"] == gap["action"]
    print("  ok")

    print("\nOK — status guidance respects active gap.")


if __name__ == "__main__":
    main()
