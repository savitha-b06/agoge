"""Smoke test with fixture data. Runs the whole deterministic path — ingest,
zone scoring, readiness, load guard, injury gates — with no network and no LLM.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agoge.analysis import gap_check, injury_flags, load_check, readiness, zone_compliance
from agoge.config import Athlete, settings
from agoge.db import DB
from agoge.nightly import store_payload
from agoge.report import physio_report

TODAY = date(2026, 8, 12)


def fake_payload(day, minutes, avg_hr, miles, z2_share=0.6):
    total = minutes * 60
    return {
        "activities": [{
            "labelId": f"act-{day}", "sportType": "indoorRun", "date": day.isoformat(),
            "totalTime": total, "distance": miles * 1609.344, "avgHr": avg_hr,
            "maxHr": avg_hr + 18, "avgCadence": 148, "elevGain": 0,
            "hrZones": {"z1": total * (1 - z2_share) * 0.7, "z2": total * z2_share,
                        "z3": total * (1 - z2_share) * 0.3, "z4": 0, "z5": 0},
        }],
        "daily": {"restingHr": 62, "steps": 8400, "trainingLoad": 120, "hrv": 58},
        "sleep": {"totalSleep": 6.4 * 3600, "sleepScore": 71},
        "fitness": {"vo2Max": 34},
    }


def main():
    db_path = Path("/tmp/agoge_test.db")
    db_path.unlink(missing_ok=True)
    db = DB(db_path)
    athlete = Athlete.load(settings.athlete_file)

    # three weeks of Tue/Thu/Sat runs, gently ramping
    plan = [(21, 129, 1.30), (18, 131, 1.10), (28, 127, 1.75),
            (24, 130, 1.50), (21, 133, 1.30), (33, 128, 2.10),
            (27, 129, 1.75), (24, 132, 1.55), (38, 130, 2.50)]
    start = TODAY - timedelta(days=20)
    i = 0
    for offset in range(21):
        d = start + timedelta(days=offset)
        if d.weekday() in (1, 3, 5) and i < len(plan):
            mins, hr, mi = plan[i]; i += 1
            store_payload(db, athlete, d, fake_payload(d, mins, hr, mi))
            db.upsert_daily(d.isoformat(), prehab_done=1, weight_lb=210 - offset * 0.2)
        else:
            db.upsert_daily(d.isoformat(), sleep_hours=6.6, hrv=57, resting_hr=61,
                            prehab_done=1)

    print("== sessions ==")
    for r in db.sessions_between(start.isoformat(), TODAY.isoformat()):
        print(f"  {r['day']} {r['sport']:6} {r['duration_min']:5.0f}min "
              f"{r['distance_mi']:.2f}mi HR{r['avg_hr']} Z2 {r['z2_pct']:.0f}%")

    print("\n== load guard ==")
    print(" ", load_check(db, athlete, TODAY))

    print("\n== readiness, healthy day ==")
    r = readiness(db, athlete, TODAY.isoformat())
    print(f"  {r['score']}/100 {r['flag']} — {r['reasons']}")
    print(f"  {r['guidance']}")

    print("\n== inject overnight knee swelling, 2 consecutive days ==")
    for back in (1, 0):
        d = (TODAY - timedelta(days=back)).isoformat()
        db.add_symptom(day=d, injury_key="knee_r", severity=4, swelling=1,
                       pain_type="dull", overnight=1, note="still puffy in the morning")
    print(" ", injury_flags(db, athlete, TODAY.isoformat()))
    r = readiness(db, athlete, TODAY.isoformat())
    print(f"  {r['score']}/100 {r['flag']}")
    print(f"  {r['guidance']}")

    print("\n== bad sleep + suppressed HRV on top ==")
    db.upsert_daily(TODAY.isoformat(), sleep_hours=4.9, hrv=42, resting_hr=69)
    r = readiness(db, athlete, TODAY.isoformat())
    print(f"  {r['score']}/100 {r['flag']} — {r['reasons']}")

    print("\n== zone compliance on a session run too hard ==")
    hot = {"zone_breakdown": {"z1": 60, "z2": 300, "z3": 900, "z4": 400}, "avg_hr": 149}
    print(f"  Z2 share: {zone_compliance(hot, athlete)}%")

    print("\n== gap check ==")
    print(" ", gap_check(db, TODAY + timedelta(days=4)))

    print("\n== physio export (first 24 lines) ==")
    rep = physio_report(db, athlete, TODAY - timedelta(days=45), TODAY)
    print("\n".join("  " + l for l in rep.splitlines()[:24]))

    print("\nOK — deterministic path works end to end.")


if __name__ == "__main__":
    main()
