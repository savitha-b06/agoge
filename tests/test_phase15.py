"""Phase 1.5: plan import, today query, fitness trend, divergence.
No network, no LLM.
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agoge.analysis import plan_divergence, readiness
from agoge.config import Athlete, settings
from agoge.db import DB
from agoge.fitness import fitness_trend, z2_pace_trend
from agoge.nightly import store_payload
from agoge.plan_import import import_plan, parse_segments
from agoge.plan_today import what_today

TODAY = date(2026, 8, 12)
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "plan.example.csv"


def fake_payload(day, minutes, avg_hr, miles, z2_share=0.85):
    total = minutes * 60
    return {
        "activities": [{
            "labelId": f"act-{day}", "sportType": "indoorRun", "date": day.isoformat(),
            "totalTime": total, "distance": miles * 1609.344, "avgHr": avg_hr,
            "maxHr": avg_hr + 12, "avgCadence": 148,
            "hrZones": {"z1": total * (1 - z2_share) * 0.5, "z2": total * z2_share,
                        "z3": total * (1 - z2_share) * 0.5, "z4": 0, "z5": 0},
        }],
        "daily": {"restingHr": 62, "steps": 8000, "hrv": 55},
        "sleep": {"totalSleep": 6.5 * 3600},
        "fitness": {"vo2Max": 34},
    }


def main():
    db_path = Path("/tmp/agoge_phase15.db")
    db_path.unlink(missing_ok=True)
    db = DB(db_path)
    athlete = Athlete.load(settings.athlete_file)

    print("== segment parse ==")
    segs = parse_segments(
        "15min warmup Z1 | 145min steady <160bpm | 20min surge 170-180bpm"
    )
    assert len(segs) == 3, segs
    assert segs[0]["duration_min"] == 15 and segs[0]["zone"] == "z1"
    assert segs[1]["hr_high"] == 160
    assert segs[2]["hr_low"] == 170 and segs[2]["hr_high"] == 180
    print(" ", json.dumps(segs, indent=2))

    print("\n== plan import (scoped; past days skipped relative to --from) ==")
    res = import_plan(
        db, athlete, EXAMPLE,
        from_day=TODAY,
        reason="fixture import for phase 1.5 smoke",
        today=TODAY,
        version="v-test-1",
    )
    print(f"  wrote={res['rows_written']} skipped={res['rows_skipped']} "
          f"conflicts={len(res['conflicts'])}")
    for c in res["conflicts"]:
        print(f"  ! {c['message']}")
    assert res["rows_written"] >= 5
    assert any(c["kind"] == "block_intensity" for c in res["conflicts"]), res["conflicts"]

    row = db.plan_for_day("2026-08-15")
    assert row is not None
    segs = json.loads(row["segments"])
    assert len(segs) == 3
    print(f"  bike long ride segments: {len(segs)}")

    print("\n== re-import preserves past, overwrites future ==")
    # Mark yesterday's plan as historical by leaving it; re-import from today
    # with a different reason should bump version on future rows only.
    res2 = import_plan(
        db, athlete, EXAMPLE,
        from_day=TODAY,
        reason="behind on volume, cutting the block by 15%",
        today=TODAY,
        version="v-test-2",
    )
    assert res2["version"] == "v-test-2"
    hist = db.plan_imports()
    assert len(hist) == 2
    print(f"  import history: {len(hist)} entries")

    # Seed Z2 sessions for fitness trend + a green today
    for i, (mins, hr, mi) in enumerate([
        (30, 125, 1.8), (32, 126, 2.0), (35, 124, 2.2),
        (38, 125, 2.5), (40, 123, 2.8), (42, 124, 3.1),
    ]):
        d = TODAY - timedelta(days=7 * (5 - i) + 1)
        store_payload(db, athlete, d, fake_payload(d, mins, hr, mi))
        db.upsert_daily(d.isoformat(), sleep_hours=6.8, hrv=56 + i,
                        resting_hr=64 - i * 0.3, vo2max=33 + i * 0.2,
                        prehab_done=1)
    db.upsert_daily(TODAY.isoformat(), sleep_hours=7.0, hrv=58, resting_hr=61,
                    prehab_done=1)

    print("\n== today query, green ==")
    ans = what_today(db, athlete, TODAY)
    print(f"  {ans['reply']}")
    assert ans["plan"] is not None
    assert ans["override"] is None
    assert "Strength" in ans["reply"] or "strength" in ans["reply"].lower()

    print("\n== today query, injury gate overrides run ==")
    run_day = date(2026, 8, 13)
    for back in (1, 0):
        d = (run_day - timedelta(days=back)).isoformat()
        db.add_symptom(day=d, injury_key="knee_r", severity=4, swelling=1,
                       pain_type="dull", overnight=1, note="still puffy")
    db.upsert_daily(run_day.isoformat(), sleep_hours=6.5, hrv=50, resting_hr=66)
    ans2 = what_today(db, athlete, run_day)
    print(f"  readiness {ans2['readiness']['flag']}: {ans2['reply'][:160]}...")
    assert ans2["readiness"]["flag"] == "red"
    assert ans2["override"] is not None
    assert (ans2["effective"] or {}).get("session_type") == "rest"

    print("\n== fitness trend ==")
    trend = fitness_trend(db, athlete, TODAY)
    z2 = z2_pace_trend(db, athlete, TODAY)
    print(f"  z2 available={z2['available']} delta={z2.get('delta_mph')} "
          f"note={z2.get('note')}")
    assert z2["available"]
    assert trend["vo2max"]["available"]

    print("\n== plan divergence (under-volume streak) ==")
    # Prescribe heavy weeks, complete almost nothing → streak
    for back in range(4):
        ws = TODAY - timedelta(days=7 * back)
        for offset in range(3):
            d = (ws - timedelta(days=ws.weekday()) + timedelta(days=offset)).isoformat()
            db.upsert_plan_row({
                "day": d, "sport": "run", "session_type": "endurance",
                "planned_min": 60, "title": "ghost session", "status": "planned",
                "source": "import", "version": "v-div",
            })
    div = plan_divergence(db, athlete, TODAY, weeks=4)
    print(f"  under_streak={div['under_streak']} msg={div['message']}")
    assert div["under_streak"] >= 3
    assert div["suggest_revision"]

    print("\nOK — Phase 1.5 path works end to end.")


if __name__ == "__main__":
    main()
