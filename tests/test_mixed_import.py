"""Mixed import batch: single-session days + two-a-days together. No LLM."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agoge.config import Athlete, settings
from agoge.db import DB
from agoge.plan_import import import_plan
from agoge.plan_today import what_today

CSV = Path(__file__).resolve().parents[1] / "examples" / "plan.mixed.csv"
TODAY = date(2026, 8, 10)


def main():
    db_path = Path("/tmp/agoge_mixed.db")
    db_path.unlink(missing_ok=True)
    db = DB(db_path)
    athlete = Athlete.load(settings.athlete_file)

    res = import_plan(
        db, athlete, CSV, from_day=TODAY, today=TODAY,
        reason="mixed batch verification", version="v-mixed",
    )
    print(f"== import wrote={res['rows_written']} skipped={res['rows_skipped']} ==")
    assert res["rows_written"] == 8, res

    # Single-session day
    mon = db.plan_for_day("2026-08-10")
    assert len(mon) == 1 and mon[0]["sport"] == "run"
    print(f"  2026-08-10 single: {[p['sport'] for p in mon]}")

    # Two-a-day — CSV order preserved (swim then strength)
    tue = db.plan_for_day("2026-08-11")
    assert len(tue) == 2
    assert [p["sport"] for p in tue] == ["swim", "strength"]
    print(f"  2026-08-11 two-a-day: {[p['sport'] for p in tue]}")

    # Rest day
    rest = db.plan_for_day("2026-08-13")
    assert len(rest) == 1 and (rest[0]["session_type"] == "rest" or rest[0]["sport"] == "rest")
    print(f"  2026-08-13 rest: ok")

    # Another two-a-day later in the batch
    sat = db.plan_for_day("2026-08-15")
    assert [p["sport"] for p in sat] == ["run", "swim"]
    print(f"  2026-08-15 two-a-day: {[p['sport'] for p in sat]}")

    # Single Friday not collapsed
    fri = db.plan_for_day("2026-08-14")
    assert len(fri) == 1 and fri[0]["sport"] == "bike"
    print(f"  2026-08-14 single bike: ok")

    db.upsert_daily("2026-08-11", sleep_hours=7.0, hrv=58, resting_hr=60, prehab_done=1)
    ans = what_today(db, athlete, date(2026, 8, 11))
    assert len(ans["plans"]) == 2
    assert "Swim" in ans["reply"] and "Strength" in ans["reply"]
    print(f"  today two-a-day: {ans['reply'][:120]}...")

    db.upsert_daily("2026-08-10", sleep_hours=7.0, hrv=58, resting_hr=60, prehab_done=1)
    ans2 = what_today(db, athlete, date(2026, 8, 10))
    assert len(ans2["plans"]) == 1
    assert "Two-a-day" not in ans2["reply"]
    assert "Run" in ans2["reply"] or "run" in ans2["reply"].lower()
    print(f"  today single: {ans2['reply'][:100]}...")

    print("\nOK — mixed import batch preserves single- and two-session days.")


if __name__ == "__main__":
    main()
