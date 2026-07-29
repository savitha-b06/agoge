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
    from .weekly import run
    out = run(_day(args.date) if args.date else None, rebuild_profile=not args.no_profile)
    console.print(out["report"])
    if out.get("profile"):
        console.print(f"\n[dim]Profile rebuilt → {settings.profile_path}[/dim]")


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


def main(argv=None):
    p = argparse.ArgumentParser(prog="agoge", description=f"agoge {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        s = sub.add_parser(name, **kw)
        s.set_defaults(func=fn)
        s.add_argument("--date", help="YYYY-MM-DD, 'today', or 'yesterday'")
        return s

    add("status", cmd_status, help="Readiness, load, and what to do today")

    s = add("log", cmd_log, help='Log anything: agoge log "knee fine, prehab done, 208lb"')
    s.add_argument("message", nargs="+")

    s = add("resolved", cmd_resolved, help="Mark an injury symptom cleared")
    s.add_argument("injury")

    s = add("nightly", cmd_nightly, help="Pull yesterday from COROS and write the note")
    s.add_argument("--no-fetch", action="store_true", help="Skip COROS, just write the note")

    s = add("weekly", cmd_weekly, help="Sunday review and week ahead")
    s.add_argument("--no-profile", action="store_true")

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

    args = p.parse_args(argv)
    try:
        args.func(args)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
