"""Plan adherence: HR band + duration vs logged session. No network, no LLM."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agoge.analysis import plan_adherence
from agoge.config import Athlete, settings
from agoge.db import DB
from agoge.nightly import store_payload, _day_context

DAY = date(2026, 8, 14)
DB_PATH = Path("/tmp/agoge_adherence.db")


def payload(avg_hr, minutes, miles=2.0):
    total = minutes * 60
    return {
        "activities": [{
            "labelId": f"adh-{avg_hr}-{minutes}", "sportType": "indoorRun",
            "date": DAY.isoformat(), "totalTime": total,
            "distance": miles * 1609.344, "avgHr": avg_hr, "maxHr": avg_hr + 10,
            "hrZones": {"z1": total * 0.2, "z2": total * 0.7, "z3": total * 0.1},
        }],
        "daily": {"restingHr": 60, "hrv": 55},
        "sleep": {"totalSleep": 6.5 * 3600},
    }


def plan_row(**kw):
    base = {
        "day": DAY.isoformat(), "sport": "run", "session_type": "endurance",
        "planned_min": 40, "target_hr_low": 117, "target_hr_high": 137,
        "title": "Z2", "status": "planned", "source": "import", "version": "v-a",
    }
    base.update(kw)
    return base


def fresh():
    DB_PATH.unlink(missing_ok=True)
    db = DB(DB_PATH)
    athlete = Athlete.load(settings.athlete_file)
    return db, athlete


def main():
    db, athlete = fresh()

    print("== skip: no plan ==")
    r = plan_adherence(db, athlete, DAY.isoformat())
    assert r["checked"] is False and r["reason"] == "no plan"
    print("  ok")

    print("== skip: segments populated ==")
    db.upsert_plan_row(plan_row(segments='[{"duration_min": 15}]'))
    r = plan_adherence(db, athlete, DAY.isoformat())
    assert r["checked"] is False and r["reason"] == "segments populated"
    print("  ok")

    print("== skip: no matching session (not a miss flag) ==")
    db.upsert_plan_row(plan_row(segments=None))
    r = plan_adherence(db, athlete, DAY.isoformat())
    assert r["checked"] is False
    assert "no matching session" in (r.get("reason") or "")
    print("  ok")

    print("== strength: duration checked, no HR band ==")
    db, athlete = fresh()
    db.upsert_plan_row(plan_row(
        sport="strength", session_type="strength", segments=None,
        target_hr_low=None, target_hr_high=None, planned_min=50,
    ))
    store_payload(db, athlete, DAY, {
        "activities": [{
            "labelId": "lift-only", "sportType": "strength",
            "date": DAY.isoformat(), "totalTime": 70 * 60, "avgHr": 105,
        }],
        "daily": {"restingHr": 60}, "sleep": {"totalSleep": 6.5 * 3600},
    })
    r = plan_adherence(db, athlete, DAY.isoformat())
    assert r["checked"] and r["duration_flag"]
    assert not r["hr_flag"]
    print(f"  {r['flags'][0]['message']}")

    print("== endurance with blank HR targets: skip HR, still check duration ==")
    db, athlete = fresh()
    db.upsert_plan_row(plan_row(
        target_hr_low=None, target_hr_high=None, planned_min=40,
    ))
    store_payload(db, athlete, DAY, payload(avg_hr=150, minutes=55))
    r = plan_adherence(db, athlete, DAY.isoformat())
    assert r["checked"]
    assert not r["hr_flag"], r
    assert r["duration_flag"], r  # 55 vs 40 = +37.5% > 25%
    assert all(f["kind"] != "hr" for f in r["flags"])
    assert any(f["kind"] == "duration" for f in r["flags"])
    assert not any("target None" in f or "None-None" in f for f in r["facts"])
    assert all("vs target" not in f for f in r["facts"])
    print(f"  facts={r['facts']}")
    print(f"  flags={[f['kind'] for f in r['flags']]}")

    print("== within tolerance ==")
    db, athlete = fresh()
    db.upsert_plan_row(plan_row())
    store_payload(db, athlete, DAY, payload(avg_hr=130, minutes=42))
    r = plan_adherence(db, athlete, DAY.isoformat())
    assert r["checked"] and r["ok"]
    assert not r["hr_flag"] and not r["duration_flag"]
    print(f"  {r['facts']}")

    print("== HR outside by more than 8 bpm ==")
    db, athlete = fresh()
    db.upsert_plan_row(plan_row())
    store_payload(db, athlete, DAY, payload(avg_hr=146, minutes=40))
    r = plan_adherence(db, athlete, DAY.isoformat())
    assert r["hr_flag"] and not r["ok"]
    print(f"  {r['flags'][0]['message']}")

    print("== HR 5 bpm over high — inside tolerance ==")
    db, athlete = fresh()
    db.upsert_plan_row(plan_row())
    store_payload(db, athlete, DAY, payload(avg_hr=142, minutes=40))
    r = plan_adherence(db, athlete, DAY.isoformat())
    assert not r["hr_flag"], r
    print("  ok")

    print("== duration +30% — outside 25% ==")
    db, athlete = fresh()
    db.upsert_plan_row(plan_row())
    store_payload(db, athlete, DAY, payload(avg_hr=125, minutes=52))
    r = plan_adherence(db, athlete, DAY.isoformat())
    assert r["duration_flag"]
    print(f"  {r['flags'][0]['message']}")

    print("== nightly context includes plain facts ==")
    db, athlete = fresh()
    db.upsert_plan_row(plan_row())
    store_payload(db, athlete, DAY, payload(avg_hr=150, minutes=55))
    ctx = _day_context(db, athlete, DAY)
    assert "Plan adherence:" in ctx
    assert "FLAG [hr]" in ctx
    assert "FLAG [duration]" in ctx
    for l in ctx.splitlines():
        if "adherence" in l.lower() or "FLAG" in l or l.strip().startswith("avg HR") or l.strip().startswith("duration"):
            print(f"  {l}")

    print("\nOK — plan adherence works.")


if __name__ == "__main__":
    main()
