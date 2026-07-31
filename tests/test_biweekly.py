"""Biweekly deep-review cadence + deterministic metrics. No network, no LLM."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agoge.biweekly import (compute_biweekly_metrics, format_biweekly_context,
                            is_biweekly_deep_review_day, _pace_to_sec_per_km,
                            _session_sec_per_km)
from agoge.config import Athlete, settings
from agoge.db import DB
from agoge.nightly import store_payload

# Block in athlete.yaml / example starts 2026-07-28 (Tue).
# Sundays: Aug 2 (d5), Aug 9 (d12), Aug 16 (d19), Aug 23 (d26), Aug 30 (d33)
BLOCK_START = date(2026, 7, 28)


def fake_run(day, minutes, avg_hr, miles, z2_share=0.8):
    total = minutes * 60
    return {
        "activities": [{
            "labelId": f"b-{day}-{avg_hr}", "sportType": "indoorRun",
            "date": day.isoformat(),
            "totalTime": total, "distance": miles * 1609.344, "avgHr": avg_hr,
            "maxHr": avg_hr + 10, "avgCadence": 150,
            "hrZones": {"z1": total * 0.1, "z2": total * z2_share,
                        "z3": total * (0.9 - z2_share), "z4": 0, "z5": 0},
        }],
        "daily": {"restingHr": 62, "hrv": 55},
        "sleep": {"totalSleep": 6.5 * 3600},
    }


def main():
    athlete = Athlete.load(settings.athlete_file)

    print("== cadence: days-since-block-start mod 14 ==")
    # Austin block 2026-07-28 → 2026-08-25
    assert not is_biweekly_deep_review_day(athlete, date(2026, 8, 2))   # Sun, day 5
    assert not is_biweekly_deep_review_day(athlete, date(2026, 8, 9))   # Sun, day 12
    assert is_biweekly_deep_review_day(athlete, date(2026, 8, 16))      # Sun, day 19 → yes
    assert not is_biweekly_deep_review_day(athlete, date(2026, 8, 23))  # Sun, day 26
    assert not is_biweekly_deep_review_day(athlete, date(2026, 8, 11))  # Tue day 14 — not Sunday
    # Vanderbilt block starts 2026-08-26; first biweekly Sunday after 14d is Sep 13
    assert not is_biweekly_deep_review_day(athlete, date(2026, 9, 6))   # day 11
    assert is_biweekly_deep_review_day(athlete, date(2026, 9, 13))      # day 18 → yes
    print("  every other Sunday from block start — OK")

    print("\n== pace math (11:05/km baseline) ==")
    sec = _pace_to_sec_per_km("11:05")
    assert sec == 665
    # Session at exactly baseline pace: 11:05/km over ~2 mi
    # 2 mi = 3.2187 km → 665 * 3.2187 sec ≈ 35.7 min
    km = 2.0 * 1.609344
    mins = (665 * km) / 60
    got = _session_sec_per_km(2.0, mins)
    assert abs(got - 665) < 0.5, got
    print(f"  baseline {sec}s/km; reconstructed session {got:.1f}s/km")

    print("\n== 14-vs-14 metrics + benchmark match ==")
    db_path = Path("/tmp/agoge_biweekly.db")
    db_path.unlink(missing_ok=True)
    db = DB(db_path)
    today = date(2026, 8, 16)

    # Prior window: Jul 20 – Aug 2  (today-27 .. today-14)
    # Current window: Aug 3 – Aug 16
    for i in range(28):
        d = today - timedelta(days=27 - i)
        # Alternate runs; later window a bit faster at same HR
        if d.weekday() in (1, 3, 5):
            faster = d >= today - timedelta(days=13)
            miles = 2.4 if faster else 2.0
            store_payload(db, athlete, d, fake_run(d, 36, 129, miles, z2_share=0.75 if faster else 0.55))
        db.upsert_daily(
            d.isoformat(),
            sleep_hours=7.0 if d >= today - timedelta(days=13) else 5.8,
            hrv=60 if d >= today - timedelta(days=13) else 48,
            resting_hr=60 if d >= today - timedelta(days=13) else 66,
            prehab_done=1,
        )

    # Prescribe a few sessions; leave one unmatched in current window
    for d in (today - timedelta(days=2), today - timedelta(days=4), today - timedelta(days=6)):
        db.upsert_plan_row({
            "day": d.isoformat(), "sport": "run", "session_type": "endurance",
            "planned_min": 35, "title": "Z2 run", "status": "planned",
            "source": "import", "version": "v-bi",
        })
    # A planned day with no session
    miss = today - timedelta(days=1)
    if miss.weekday() not in (1, 3, 5):
        db.upsert_plan_row({
            "day": miss.isoformat(), "sport": "run", "session_type": "endurance",
            "planned_min": 30, "title": "missed on purpose", "status": "planned",
            "source": "import", "version": "v-bi",
        })

    m = compute_biweekly_metrics(db, athlete, today)
    assert m["current_window"]["start"] == "2026-08-03"
    assert m["prior_window"]["start"] == "2026-07-20"
    assert m["deltas"]["mean_sleep_h"] is not None and m["deltas"]["mean_sleep_h"] > 0
    assert m["deltas"]["mean_hrv"] > 0
    assert m["deltas"]["mean_resting_hr"] < 0
    assert m["benchmark"]["available"] is True
    assert m["benchmark"]["match"]["avg_hr"] == 129
    print(f"  completion curr={m['current_window']['completion_rate_pct']}%")
    print(f"  z2 curr={m['current_window']['mean_z2_pct']} prior={m['prior_window']['mean_z2_pct']}")
    print(f"  {m['benchmark']['verdict']}")

    ctx = format_biweekly_context(m, athlete, today)
    assert "SESSION COMPLETION" in ctx
    assert "BENCHMARK" in ctx
    assert "11:05" in ctx
    print("\nOK — biweekly cadence and metrics work.")


if __name__ == "__main__":
    main()
