"""Fixture test for the Cronometer parser, the migration, and the fuelling alarm.
No network, no Go binary, no credentials."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agoge.analysis import energy_availability, protein_status, sleep_regularity
from agoge.config import Athlete, settings
from agoge.cronometer import parse_biometrics, parse_daily_nutrition
from agoge.db import DB
from agoge.nightly import store_payload

NUTRITION_CSV = """Date,Energy (kcal),Protein (g),Carbs (g),Fat (g),Fiber (g),Iron (mg),Calcium (mg),Sodium (mg),Caffeine (mg)
2026-08-10,2480,178.4,265.1,82.3,31.2,18.4,1120,3100,180
2026-08-11,1620,141.0,150.2,61.5,22.0,14.1,940,2400,220
"""

BIOMETRIC_CSV = """Day,Metric,Unit,Amount
2026-08-10,Weight,lbs,209.4
2026-08-11,Weight,lbs,208.8
2026-08-11,Body Fat,%,38.1
"""


def fake_run(day, minutes, kcal_hint=None):
    return {"activities": [{
        "labelId": f"a-{day}", "sportType": "indoorRun", "date": day.isoformat(),
        "totalTime": minutes * 60, "distance": minutes * 0.062 * 1609.344,
        "avgHr": 130, "maxHr": 148, "avgCadence": 150,
        "hrZones": {"z1": minutes * 12, "z2": minutes * 44, "z3": minutes * 4},
    }], "daily": {"restingHr": 61}, "sleep": {"totalSleep": 6.5 * 3600}}


def main():
    p = Path("/tmp/agoge_nutrition.db"); p.unlink(missing_ok=True)
    db = DB(p)
    athlete = Athlete.load(settings.athlete_file)

    print("== parse daily nutrition CSV ==")
    nut = parse_daily_nutrition(NUTRITION_CSV)
    for d, v in sorted(nut.items()):
        print(f"  {d}: {v['kcal_in']:.0f} kcal, {v['protein_g']:.0f}g protein, "
              f"{v['iron_mg']:.1f}mg iron")

    print("\n== parse biometrics CSV ==")
    print(" ", parse_biometrics(BIOMETRIC_CSV))

    print("\n== write to db (exercises migration path too) ==")
    bio = parse_biometrics(BIOMETRIC_CSV)
    for d, v in nut.items():
        merged = {**v, **{k: x for k, x in bio.get(d, {}).items() if k != "body_fat_pct"}}
        db.upsert_daily(d, **merged)
    for d in ("2026-08-10", "2026-08-11"):
        db.upsert_daily(d, sleep_hours=6.4, hrv=55, resting_hr=61, prehab_done=1)

    print("\n== fuelling: well-fed day vs long session on low intake ==")
    store_payload(db, athlete, date(2026, 8, 10), fake_run(date(2026, 8, 10), 35))
    store_payload(db, athlete, date(2026, 8, 11), fake_run(date(2026, 8, 11), 95))
    for d in ("2026-08-10", "2026-08-11"):
        ea = energy_availability(db, athlete, d)
        print(f"  {d}: EA {ea['ea']} kcal/kg FFM  [{ea['flag']}]  "
              f"(in {ea['kcal_in']:.0f}, out ~{ea['kcal_out']:.0f}, FFM {ea['ffm_kg']}kg)")
        if ea["message"]:
            print(f"      {ea['message']}")

    print("\n== protein ==")
    print(" ", protein_status(db, athlete, "2026-08-11"))

    print("\n== sleep regularity with too little data ==")
    print(" ", sleep_regularity(db, "2026-08-11"))

    print("\n== sleep regularity with 14 nights ==")
    base = date(2026, 8, 11)
    for i, h in enumerate([6.1, 7.4, 5.2, 6.8, 7.9, 5.5, 6.3, 7.1, 4.9, 6.6, 7.2, 6.0]):
        db.upsert_daily((base - timedelta(days=i + 2)).isoformat(), sleep_hours=h)
    print(" ", sleep_regularity(db, "2026-08-11"))

    print("\nOK — nutrition path works end to end.")


if __name__ == "__main__":
    main()
