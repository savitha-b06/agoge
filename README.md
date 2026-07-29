# agoge

> *ἀγωγή* — the Spartan training programme. Not a coach, not a god. The regimen itself.

A training agent that actually remembers you. Pulls from the COROS MCP every
night, stores what your watch records alongside what it can't, and writes back
in prose you'll read on a phone.

Built for one athlete training for a 70.3, but the athlete lives entirely in a
config file — point it at your own zones, injuries, and race and it works.

## Why this exists

Wearable apps show you charts. They don't know that your right knee is two years
post-ACL, that swelling which survives the night means the last session was too
much, or that a 129 bpm average means something different on five hours of sleep.
That context is the whole game, and it lives nowhere.

`agoge` keeps it in a SQLite file you own.

## Design rules

**Ingestion is deterministic.** MCP tools are called directly and parsed in
Python. The model never touches raw data and never invents a number — it only
writes prose from values already computed and stored.

**Judgment is arithmetic, not vibes.** Readiness, zone compliance, the load ramp
cap, injury gates — all in `analysis.py`, all traceable. If it tells you to back
off you can see exactly why.

**Your data is not in this repo.** `athlete.yaml`, `data/`, and `.env` are
gitignored. The code is public; the athlete isn't. Keep it that way — health
history has no business in a portfolio repo.

## Setup

Full step-by-step in [DEPLOY.md](DEPLOY.md). Short version:

```bash
git clone https://github.com/savitha-b06/agoge && cd agoge
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env              # add ANTHROPIC_API_KEY
cp athlete.example.yaml athlete.yaml   # zones, injuries, blocks, race
agoge coros tools                 # authorise COROS, see what it exposes
agoge nightly --date yesterday
```

On a VPS: `ssh root@IP 'bash -s' < deploy/setup_vps.sh`, then install
`deploy/crontab.example`.

## Commands

| Command | What it does |
|---|---|
| `agoge status` | Readiness, load headroom, open injury questions, what to do today |
| `agoge log "..."` | Log anything in plain English — symptoms, weight, prehab, sleep |
| `agoge nightly` | Pull yesterday from COROS, score it, write the daily note |
| `agoge weekly` | Sunday review, next week's plan, rebuild the profile |
| `agoge sessions` | Recent sessions with zone compliance |
| `agoge checkpoints` | Progress against your phase targets |
| `agoge physio --since 2026-07-01` | Symptom + load summary to hand a clinician |
| `agoge profile --rebuild` | Regenerate the standing profile |
| `agoge coros tools` | Inspect what the COROS MCP actually exposes |
| `agoge nutrition sync` | Pull Cronometer via the cronosync binary |
| `agoge nutrition import --file x.csv` | Manual nutrition import, never breaks |
| `agoge sleep` | Sleep regularity and debt, descriptive only |
| `agoge import file.json` | Manual ingest when the MCP is down |

## The profile

`data/athlete_profile.md` is the "model of yourself" — a document the agent
rewrites weekly from the last eight weeks of data and loads as context every
time you talk to it.

It's a file, not weights. When it gets something wrong, open it and fix the
line. That's a feature.

## What's stored

- `sessions` — objective, from COROS, plus computed zone compliance
- `daily` — sleep, HRV, resting HR, load, weight, prehab, readiness
- `symptoms` — injury site, severity, swelling, **whether it survived the night**
- `events` — rolls, illness, travel, anything flagged
- `notes` — the daily and weekly prose
- `plan` — what was prescribed, and whether it happened

## Roadmap

- [ ] WhatsApp channel via OpenClaw so `agoge log` becomes a text message
- [ ] Image and voice ingest over chat (gym console photos, post-run voice notes)
- [ ] Literature grounding from PubMed with study type and population attached
- [ ] n-of-1 sleep experiment mode (randomised, not observational)
- [ ] Calendar and weather awareness for scheduling
- [ ] Shoe mileage tracking
- [ ] Hevy API write-back for lifting routines
- [ ] COROS write-back when it ships (they've signposted it for before Sept 2026)
- [ ] Travel mode for timezone shifts
- [ ] Brick and race-simulation session types

## Renaming

`agoge` appears in `src/agoge/`, `pyproject.toml`, and the deploy scripts:

```bash
git grep -l agoge | xargs sed -i 's/agoge/chiron/g' && mv src/agoge src/chiron
```

## A note on scope

This gives training guidance to people with injuries. It is not medical advice,
the injury gates are conservative on purpose, and you should not treat any of it
as a substitute for a physio who can actually look at your knee.

Personal project. PRs welcome, no support promised.

## Optional: Cronometer

Nutrition comes from a **separate** GPLv2 binary,
[cronosync](https://github.com/savitha-b06/cronosync), which wraps
[gocronometer](https://github.com/jrmycanady/gocronometer). agoge shells out to
it and parses stdout — nothing here links against it, which is why this repo can
stay MIT. It is optional and everything works without it.

## Licence

MIT.
