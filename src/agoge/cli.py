"""agoge — command line. Everything the agent will eventually do over text,
you can do here first. Build the CLI, then point a chat channel at it.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta

from rich.console import Console
from rich.table import Table

from . import __version__
from .analysis import (checkpoint_status, energy_availability, gap_check, load_check,
                       protein_status, race_status, readiness, sleep_regularity)
from .config import Athlete, settings
from .db import DB

console = Console()


def _ctx():
    return DB(settings.db_path), Athlete.load(settings.athlete_file)


def _day(s: str | None) -> date:
    if not s or s == "today":
        return date.today()
    if s == "yesterday":
        return date.today() - timedelta(days=1)
    return date.fromisoformat(s)


# ------------------------------------------------------------------ commands

def cmd_status(args):
    db, athlete = _ctx()
    today = _day(args.date)
    r = readiness(db, athlete, today.isoformat())
    race = race_status(db, athlete, today)
    load = load_check(db, athlete, today)

    colour = {"green": "green", "amber": "yellow", "red": "red"}[r["flag"]]
    console.print(f"\n[bold]{athlete.name}[/bold] — {today.isoformat()} ({today.strftime('%A')})")
    console.print(f"[{colour}]Readiness {r['score']}/100 ({r['flag']})[/{colour}]")
    if r["reasons"]:
        for reason in r["reasons"]:
            console.print(f"  · {reason}")
    console.print(f"\n{r['guidance']}\n")
    console.print(f"Block: {race['block']}  |  {race['days_to_race']} days to race "
                  f"({race['weeks_to_race']} weeks)")
    console.print(f"Load: {load['this_week_min']:.0f} min this week vs "
                  f"{load['last_week_min']:.0f} last"
                  + (f"  |  cap {load['cap_min']:.0f}, headroom {load['headroom_min']:.0f}"
                     if load['cap_min'] else ""))
    if load["breach"]:
        console.print("[red]Ramp cap breached. Next week does not go up.[/red]")
    console.print(f"Consistency: {gap_check(db, today)['action']}")

    ea = energy_availability(db, athlete, today.isoformat())
    if ea.get("available") and ea["flag"] != "ok":
        console.print(f"\n[red]{ea['message']}[/red]")

    prot = protein_status(db, athlete, today.isoformat())
    if prot.get("available") and prot["avg"] is not None:
        console.print(f"Protein: {prot['avg']:.0f} g/day avg over {prot['days_logged']} "
                      f"logged days (target {prot['target']:.0f}, "
                      f"hit {prot['days_hit']}/{prot['days_logged']})")

    from .annotate import resolve_open_symptoms
    for q in resolve_open_symptoms(db, athlete, today):
        console.print(f"\n[yellow]?[/yellow] {q}")


def cmd_log(args):
    from .annotate import apply, parse
    db, athlete = _ctx()
    day = _day(args.date)
    message = " ".join(args.message)
    parsed = parse(message, athlete, day)
    applied = apply(db, athlete, parsed, day)
    if not applied:
        console.print("[dim]Nothing loggable found in that message.[/dim]")
        return
    console.print(f"[green]Logged for {day.isoformat()}:[/green]")
    for a in applied:
        console.print(f"  · {a}")
    db.upsert_daily(day.isoformat(),
                    readiness=readiness(db, athlete, day.isoformat())["score"])


def cmd_resolved(args):
    from .annotate import mark_resolved
    db, _ = _ctx()
    n = mark_resolved(db, args.injury, _day(args.date))
    console.print(f"Marked {n} open {args.injury} symptom(s) resolved.")


def cmd_nightly(args):
    from .nightly import run
    result = run(_day(args.date) if args.date else None, skip_fetch=args.no_fetch)
    if result.get("ingest_error"):
        console.print(f"[yellow]Ingest warning:[/yellow] {result['ingest_error']}")
    console.print(f"\n[bold]{result['day']}[/bold]\n")
    console.print(result["note"])
    r = result["readiness"]
    console.print(f"\n[dim]Readiness {r['score']}/100 ({r['flag']})[/dim]")


def cmd_weekly(args):
    from .analysis import format_weekly_progression, weekly_progression
    from .weekly import run
    day = _day(args.date) if args.date else None
    if args.progression_only:
        db, athlete = _ctx()
        prog = weekly_progression(db, athlete, day or date.today())
        console.print(format_weekly_progression(prog))
        return
    out = run(
        day,
        rebuild_profile=not args.no_profile,
        force_biweekly=args.biweekly,
    )
    console.print(out["report"])
    if out.get("biweekly"):
        console.print("\n[bold]── Biweekly deep review ──[/bold]\n")
        console.print(out["biweekly"]["report"])
    elif args.biweekly:
        console.print("[yellow]Biweekly was forced but produced no report.[/yellow]")
    if out.get("profile"):
        console.print(f"\n[dim]Profile rebuilt → {settings.profile_path}[/dim]")


def cmd_biweekly(args):
    """Force the biweekly deep review (normally auto-fired from Sunday weekly)."""
    from .biweekly import compute_biweekly_metrics, format_biweekly_context, run
    db, athlete = _ctx()
    day = _day(args.date)
    if args.metrics_only:
        m = compute_biweekly_metrics(db, athlete, day)
        console.print(format_biweekly_context(m, athlete, day))
        return
    out = run(db, athlete, day, force=True)
    if not out:
        console.print("[yellow]No biweekly report generated.[/yellow]")
        return
    console.print(out["report"])


def cmd_sessions(args):
    db, _ = _ctx()
    end = _day(args.date)
    start = end - timedelta(days=args.days)
    t = Table(title=f"Sessions {start} → {end}")
    for col in ("Day", "Sport", "Min", "Miles", "Avg HR", "Z2 %"):
        t.add_column(col)
    for r in db.sessions_between(start.isoformat(), end.isoformat()):
        t.add_row(r["day"], r["sport"],
                  f"{r['duration_min']:.0f}" if r["duration_min"] else "—",
                  f"{r['distance_mi']:.2f}" if r["distance_mi"] else "—",
                  str(r["avg_hr"] or "—"),
                  f"{r['z2_pct']:.0f}" if r["z2_pct"] is not None else "—")
    console.print(t)


def cmd_checkpoints(args):
    db, athlete = _ctx()
    t = Table(title="Phase checkpoints")
    for col in ("Metric", "Now", "Target", "Due", "Days", "Status"):
        t.add_column(col)
    for c in checkpoint_status(db, athlete, _day(args.date)):
        t.add_row(c["metric"], str(c["actual"] if c["actual"] is not None else "—"),
                  str(c["target"]), c["due"], str(c["days_left"]),
                  "[green]on track[/green]" if c["on_track"] else "[yellow]behind[/yellow]")
    console.print(t)


def cmd_physio(args):
    from .report import write_physio_report
    db, athlete = _ctx()
    path = write_physio_report(db, athlete, _day(args.since), _day(args.date))
    console.print(f"[green]Written:[/green] {path}")


def cmd_profile(args):
    from .profile import read_profile, update_profile
    db, athlete = _ctx()
    if args.rebuild:
        console.print(update_profile(db, athlete, _day(args.date)))
    else:
        console.print(read_profile() or "[dim]No profile yet. Run: agoge profile --rebuild[/dim]")


def cmd_coros(args):
    from .coros import CorosClient
    c = CorosClient()
    if args.action == "tools":
        for t in c.list_tools():
            console.print(f"[bold]{t['name']}[/bold]  {t['description'][:110]}")
    elif args.action == "discover":
        console.print(json.dumps(c.discover(), indent=2))
    elif args.action == "day":
        console.print(json.dumps(c.fetch_day(_day(args.date)), indent=2, default=str)[:4000])


def cmd_nutrition(args):
    from .cronometer import CronometerError, ingest, ingest_csv_file
    db, athlete = _ctx()
    if args.action == "sync":
        start = _day(args.since) if args.since else _day(args.date)
        try:
            res = ingest(db, start, _day(args.date))
        except CronometerError as exc:
            console.print(f"[red]Cronometer sync failed:[/red] {exc}")
            console.print("[dim]Fall back to: agoge nutrition import <file.csv>[/dim]")
            sys.exit(2)
        console.print(f"[green]Synced[/green] {res['days']} day(s) "
                      f"({res['nutrition_days']} nutrition, {res['biometric_days']} biometric)")
    elif args.action == "import":
        n = ingest_csv_file(db, args.file, kind=args.kind)
        console.print(f"[green]Imported[/green] {n} day(s) from {args.file}")
    elif args.action == "show":
        d = args.date or date.today().isoformat()
        ea = energy_availability(db, athlete, _day(d).isoformat())
        prot = protein_status(db, athlete, _day(d).isoformat())
        console.print(json.dumps({"energy_availability": ea, "protein": prot},
                                 indent=2, default=str))


def cmd_sleep(args):
    db, _ = _ctx()
    console.print(json.dumps(
        sleep_regularity(db, _day(args.date).isoformat(), window=args.window),
        indent=2, default=str))


def cmd_import(args):
    """Manual path for when the MCP is unavailable — feed it a JSON payload
    shaped like fetch_day output."""
    from .nightly import store_payload
    db, athlete = _ctx()
    payload = json.loads(open(args.file).read())
    out = store_payload(db, athlete, _day(args.date), payload)
    console.print(f"Stored {len(out['sessions'])} session(s) for {_day(args.date)}")


def cmd_today(args):
    """What do I need to do today — plan + readiness override."""
    from .plan_today import what_today
    db, athlete = _ctx()
    result = what_today(db, athlete, _day(args.date))
    console.print(f"\n[bold]{athlete.name}[/bold] — {result['day']}")
    r = result["readiness"]
    colour = {"green": "green", "amber": "yellow", "red": "red"}[r["flag"]]
    console.print(f"[{colour}]Readiness {r['score']}/100 ({r['flag']})[/{colour}]")
    console.print()
    console.print(result["reply"])
    if result["conflicts"]:
        console.print()
        for c in result["conflicts"]:
            console.print(f"[yellow]![/yellow] {c['message']}")
    if args.json:
        console.print_json(data={
            k: v for k, v in result.items() if k != "reply"
        })


def cmd_telegram(args):
    """Long-poll Telegram and route messages to the existing CLI commands."""
    from .telegram_bot import run as run_telegram
    run_telegram()


def cmd_plan(args):
    from .plan_import import import_plan, revision_context
    db, athlete = _ctx()
    if args.action == "import":
        if not args.file:
            console.print("[red]Need --file path.csv (or .xlsx)[/red]")
            sys.exit(2)
        res = import_plan(
            db, athlete, args.file,
            from_day=_day(args.since) if args.since else (
                _day(args.date) if args.date else None
            ),
            reason=args.reason or "",
            today=_day(args.date) if args.date else None,
        )
        console.print(
            f"[green]Imported[/green] {res['rows_written']} row(s) "
            f"(skipped {res['rows_skipped']}) as {res['version']}"
        )
        console.print(f"  from {res['from_day']}  —  {res['reason']}")
        for c in res["conflicts"]:
            console.print(f"[yellow]![/yellow] {c['message']}")
    elif args.action == "show":
        start = _day(args.since) if args.since else date.today()
        end = start + timedelta(days=args.days)
        t = Table(title=f"Plan {start} → {end}")
        for col in ("Day", "Sport", "Type", "Min", "HR", "Title", "Ver"):
            t.add_column(col)
        for r in db.plan_between(start.isoformat(), end.isoformat()):
            hr = ""
            if r["target_hr_low"] or r["target_hr_high"]:
                hr = f"{r['target_hr_low'] or '—'}-{r['target_hr_high'] or '—'}"
            t.add_row(
                r["day"], r["sport"] or "—", r["session_type"] or "—",
                f"{r['planned_min']:.0f}" if r["planned_min"] else "—",
                hr or "—", r["title"] or "—", (r["version"] or "—")[-8:],
            )
        console.print(t)
    elif args.action == "history":
        for r in db.plan_imports():
            console.print(
                f"{r['imported_at']}  {r['version']}  "
                f"from={r['from_day']}  wrote={r['rows_written']}  "
                f"skipped={r['rows_skipped']}  — {r['reason']}"
            )
    elif args.action == "revision-context":
        console.print(revision_context(db, athlete, _day(args.date)))


def cmd_fitness(args):
    from .fitness import fitness_trend, format_fitness_trend
    db, athlete = _ctx()
    trend = fitness_trend(db, athlete, _day(args.date))
    console.print(format_fitness_trend(trend))
    if args.json:
        console.print_json(data=trend)


def main(argv=None):
    p = argparse.ArgumentParser(prog="agoge", description=f"agoge {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        s = sub.add_parser(name, **kw)
        s.set_defaults(func=fn)
        s.add_argument("--date", help="YYYY-MM-DD, 'today', or 'yesterday'")
        return s

    add("status", cmd_status, help="Readiness, load, and what to do today")

    s = add("today", cmd_today, help="What do I need to do today? (plan + readiness)")
    s.add_argument("--json", action="store_true")

    s = add("log", cmd_log, help='Log anything: agoge log "knee fine, prehab done, 208lb"')
    s.add_argument("message", nargs="+")

    s = add("resolved", cmd_resolved, help="Mark an injury symptom cleared")
    s.add_argument("injury")

    s = add("nightly", cmd_nightly, help="Pull yesterday from COROS and write the note")
    s.add_argument("--no-fetch", action="store_true", help="Skip COROS, just write the note")

    s = add("weekly", cmd_weekly, help="Sunday review and week ahead")
    s.add_argument("--no-profile", action="store_true")
    s.add_argument("--biweekly", action="store_true",
                   help="Also run the biweekly deep review (auto on every other Sunday)")
    s.add_argument("--progression-only", action="store_true",
                   help="Print week-over-week progression numbers, skip the LLM")

    s = add("biweekly", cmd_biweekly,
            help="Biweekly deep review (14d vs prior 14d); normally auto from weekly")
    s.add_argument("--metrics-only", action="store_true",
                   help="Print deterministic metrics, skip the LLM")

    s = add("sessions", cmd_sessions, help="Recent sessions table")
    s.add_argument("--days", type=int, default=21)

    add("checkpoints", cmd_checkpoints, help="Progress against phase targets")

    s = add("physio", cmd_physio, help="Export a symptom + load summary for a clinician")
    s.add_argument("--since", default=(date.today() - timedelta(days=60)).isoformat())

    s = add("profile", cmd_profile, help="Read or rebuild the standing profile")
    s.add_argument("--rebuild", action="store_true")

    s = add("coros", cmd_coros, help="Inspect the COROS MCP connection")
    s.add_argument("action", choices=["tools", "discover", "day"])

    s = add("nutrition", cmd_nutrition, help="Cronometer sync, import, or show")
    s.add_argument("action", choices=["sync", "import", "show"])
    s.add_argument("--since", help="start date for a backfill sync")
    s.add_argument("--file", help="CSV path for import")
    s.add_argument("--kind", default="nutrition", choices=["nutrition", "biometrics"])

    s = add("sleep", cmd_sleep, help="Sleep regularity and debt (descriptive only)")
    s.add_argument("--window", type=int, default=14)

    s = add("import", cmd_import, help="Import a JSON payload manually")
    s.add_argument("file")

    s = add("plan", cmd_plan, help="Import / show / audit the training plan")
    s.add_argument("action", choices=["import", "show", "history", "revision-context"])
    s.add_argument("--file", help="CSV or xlsx path for import")
    s.add_argument("--since", "--from", dest="since",
                   help="Only overwrite plan rows on/after this date")
    s.add_argument("--reason", default="",
                   help="Why this revision, e.g. behind on volume, cutting 15%%")
    s.add_argument("--days", type=int, default=21, help="Window for plan show")

    s = add("fitness", cmd_fitness, help="Long-horizon fitness trend (not daily readiness)")
    s.add_argument("--json", action="store_true")

    add("telegram", cmd_telegram, help="Run the Telegram bot (long-poll)")

    args = p.parse_args(argv)
    try:
        args.func(args)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)
    except (ValueError, ImportError) as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(2)


if __name__ == "__main__":
    main()
