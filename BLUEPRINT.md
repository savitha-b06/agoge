# agoge — Product Blueprint

*A training agent that remembers you. Built for one athlete, designed so anyone can point it at themselves.*

Status key used throughout: **[BUILT]** = implemented and tested with fixture data · **[DESIGNED]** = specified below, not yet built · **[OPEN]** = a real decision still needs to be made before this can be built correctly.

---

## 1. Vision

Wearable apps show charts. Food trackers show macros. Neither knows that a swollen knee that's still puffy the next morning means yesterday's session was too much, or that a 129 bpm average means something different on five hours of sleep versus eight. That context — the stuff that actually gates training decisions — lives nowhere. It's supposed to live in a coach's head, and most people training for their first endurance race don't have one.

agoge is that missing layer. It pulls objective data from the athlete's own devices every night, asks for the subjective data no device can capture, and turns both into judgment: a readiness score, a go/no-go on today's run, a Sunday plan that respects a hard ramp-rate cap. It talks back over text, the same channel the athlete already lives in, not a dashboard they have to remember to open.

It is explicitly *not* trying to be smarter than a real coach or a real physician. It is trying to be more consistent than a 19-year-old's memory at 6pm on a Tuesday.

---

## 2. Design philosophy — three rules that shape every decision below

**2.1 — Ingestion is deterministic. Judgment is arithmetic. The model only writes prose.**

Every number that enters the system — heart rate, distance, sleep hours, calories — is fetched via API and parsed in code. An LLM never touches raw source data and never computes a derived number. Readiness score, zone compliance percentage, the weekly load ramp cap, injury escalation — all plain functions over stored data, traceable line by line. The LLM's only job is to read numbers that are *already computed and already correct* and turn them into something readable on a phone screen at 6am. This is what makes the system trustworthy: it cannot hallucinate a heart rate, because it never sees a heart rate that hasn't already been through deterministic code.

**2.2 — The "model of the athlete" is a document, not a set of weights.**

Early temptation with a project like this is to think "fine-tune a model on my data." Don't. There's nowhere near enough data to make that better than the base model, and the thing being asked for — reasoning about physiology — is already in the base model's training. What's actually needed is a *state layer*: a running markdown document the agent rewrites weekly from the last two months of data, and reloads as context on every conversation. Same practical effect as "the agent knows me," achieved with ~150 lines of code instead of an ML pipeline, and — critically — it's readable and correctable. When it gets something wrong, open the file and fix the line. You cannot do that with weights.

**2.3 — Personal data never leaves the athlete's machine. Code is public, data is not.**

Two categories, enforced structurally, not by convention: code (public, MIT-licensed, safe for a GitHub profile) and data (injury history, weight, nutrition — gitignored, lives only on the athlete's own rented server). This split exists so the project can be open-sourced honestly. Nobody should have to choose between building this in public and keeping their ACL surgery off a repo a recruiter might browse.

---

## 3. System architecture

```
 Watch + HR strap                 Food logging app
       |                                |
       v                                v
  COROS cloud                     Cronometer (unofficial
  (official MCP server,            export API, via a
  OAuth, read-only)                 separate Go binary)
       |                                |
       +---------------+----------------+
                        |
                        v
            [ Always-on rented server ]
                        |
        +---------------+----------------+
        |               |                |
   Nightly job     SQLite database   Weekly job
   (04:00 cron)    (single file,     (Sun 18:00)
        |            owned by the         |
        |            athlete)             |
        v               ^                 v
   Deterministic    <---+---->      Deterministic
   analysis layer                   analysis layer
   (readiness,                      (load-capped plan,
   zone %, load                     checkpoint status)
   guard, injury                          |
   gates, fuelling                        v
   floor)                          LLM (Sonnet-class):
        |                          writes the weekly
        v                          review + rebuilds
   LLM (Haiku-class):               the rolling profile
   writes the daily
   note from numbers
   already computed
        |
        v
   Chat interface (WhatsApp / iMessage / CLI)
   <----------------------------------->
   Athlete texts, photographs, or voice-notes in;
   agent replies; both directions log to the DB.
```

Two things to notice about this diagram. First, the database sits in the middle and everything else is a spoke off it — the LLM calls are leaves, not the trunk. Second, there are two separate LLM tiers doing two different jobs: a cheap, fast model for the nightly note (high frequency, low stakes, needs to be nearly free), and a stronger model for the weekly review and profile rebuild (low frequency, needs to reason across two months of trend data and hold a hard numeric constraint like the ramp cap without violating it).

---

## 4. Tech stack, and why each piece is there

| Layer | Choice | Why this and not the obvious alternative |
|---|---|---|
| Host | Small always-on VPS ($6–7/mo) | The nightly job has to run at 4am whether or not a laptop is open, and the athlete relocates three times in the training cycle (Austin → Nashville → Madrid → Tokyo). A rented box in a datacenter doesn't care about any of that. A Raspberry Pi under a desk does. |
| Database | SQLite, single file | One user, low write volume, needs to be trivially backed up (`cp` one file) and trivially inspected (`sqlite3 agoge.db`). Postgres or a hosted DB would add an entire service to operate for a workload this small — wrong tool. |
| Core logic | Python | Fast to iterate, good stdlib for CSV/JSON wrangling, `sqlite3` and `PyYAML` cover everything needed without heavy dependencies. |
| Training data source | COROS's official MCP server (OAuth, hosted by COROS) | Unlike Garmin, COROS ships an official, documented, read-only API surface via MCP. No scraping, no fragile session cookies for the primary data source. |
| Nutrition data source | An **unofficial**, separate Go binary (`cronosync`) wrapping the community `gocronometer` library | Cronometer has no official API. This is the one deliberately fragile piece in the system — see §7 and §10. |
| LLM | Anthropic API, two tiers (fast/cheap for nightly, stronger for weekly) | Nightly notes are one-paragraph summarization of already-structured data — cheap. Weekly review has to hold a hard numeric constraint (the ramp cap) across a full week of data and not violate it — worth paying more for reliability there. |
| Scheduling | plain cron | The entire schedule is "once or twice a day, at fixed times." Nothing here needs a workflow engine. |
| Chat interface | WhatsApp (via a self-hosted bridge) or Telegram | iMessage requires an always-on Mac; WhatsApp/Telegram run natively from a Linux box and survive international travel without any special handling. |

**The one non-obvious architectural decision worth explaining:** `cronosync` is a *separate program*, communicating with the Python codebase only via a JSON blob on stdout — not a linked library. This is a licensing boundary, not a style preference: the community Cronometer library is GPLv2, and the author explicitly scoped it to personal export use. Keeping it as an arm's-length subprocess lets the main `agoge` codebase stay MIT-licensed and shippable as an open-source portfolio piece, while the GPLv2 piece lives in its own repo, clearly labeled, doing exactly what its author intended and nothing more.

---

## 5. Data model

Six tables, all in one SQLite file:

- **`sessions`** — one row per training session. Objective fields (duration, distance, avg/max HR, cadence, elevation) come from COROS. One derived field, `z2_pct`, is computed by the analysis layer, not supplied by the watch.
- **`daily`** — one row per calendar day. Sleep, HRV, resting HR, training load, steps, and VO2max from COROS; calories, protein, and micronutrients from Cronometer; weight, prehab completion, and a computed `readiness` score and flag.
- **`symptoms`** — the injury log. Site, severity, whether swelling was present, pain type, and critically, whether it was **still present overnight** — this single boolean is what the escalation logic keys off.
- **`events`** — anything flagged: ankle rolls, illness, missed sessions, unparsed text that a human should glance at later.
- **`notes`** — the LLM-authored prose: daily notes, weekly reviews, distinct from the rolling profile document (which lives as its own markdown file, not a DB row, because it's meant to be hand-edited).
- **`plan`** — what was prescribed for a given day, and its status, so the "did the plan actually happen" question is answerable in six months, not just "what happened."

Everything personal — the filled-in `athlete.yaml` (zones, injury history, race target) and the `.db` file itself — is gitignored. Only example/template versions of the config ship in the public repo.

---

## 6. Feature set, organized by build phase

### Phase 1 — Core loop **[BUILT]**

- Nightly ingest from COROS (sessions + daily wellness), normalized into the schema above
- Deterministic zone-compliance scoring (time actually spent in target HR zone, not just average HR)
- Deterministic readiness score (HRV vs. 28-day baseline, resting HR delta, sleep, capped hard by any open injury flag)
- Deterministic injury escalation: if a symptom is logged as present overnight for N consecutive days, readiness is force-capped and the guidance changes from "train as planned" to "no running today"
- Deterministic weekly load-ramp guard: computes this week's vs last week's training minutes, enforces a configurable max percentage increase, and the weekly-plan LLM call is given the resulting cap as a hard constraint it cannot exceed
- Natural-language logging (`agoge log "knee fine, prehab done, 208lb"`) — an LLM parses free text into the structured symptom/event/daily schema, but never invents a value the athlete didn't state
- Morning follow-up questions, generated automatically when an injury symptom was left open overnight — the system asks, rather than relying on the athlete to remember to report back
- Weekly Sunday review + next week's plan, with the load cap and injury gates enforced as hard constraints on what the LLM is allowed to prescribe
- The rolling athlete profile: a markdown document rebuilt weekly from ~8 weeks of data, read (not written) by the athlete, loaded as context on every future interaction
- Physio/physician export: a clean, dated markdown report of training load and symptom history, for the athlete to bring to an actual appointment
- Nutrition ingest via the separate `cronosync` binary, with a CSV-import fallback path that cannot break
- Energy-availability floor alarm: computed from intake, estimated exercise cost, and fat-free mass; deliberately built to be silent when fine and loud only when under-fuelling risks undermining recovery — never a daily deficit scoreboard
- Descriptive-only sleep regularity stats (mean, standard deviation, 7-day debt), explicitly labeled as non-causal until a much larger sample exists

### Phase 1.5 — Structured plan import & long-horizon fitness trend **[DESIGNED, not yet built]**

This phase sits between the core loop and the conversational layer because it extends a table that already exists (`plan`, §5) rather than introducing new architecture — it's closer to "finish the core loop" than "add a new capability."

- **Plan import from a spreadsheet.** Today the `plan` table is populated implicitly, one week at a time, by the Sunday LLM call. This extends it to accept a bulk import — several weeks or months of prescribed training, parsed once from a spreadsheet into that same table. Recommended columns: `date, week_of_block, sport, session_type (endurance / interval / strength / rest), planned_duration_min, target_hr_low, target_hr_high, segments, lift_focus, notes`. The `segments` column carries structured sub-workouts as plain text — e.g. `15min warmup Z1 | 145min steady <160bpm | 20min surge 170-180bpm` — which the import step parses into discrete blocks rather than one flat duration/zone pair. That structure is what a session like a long ride with an easy majority and a hard finish actually needs.
- **The imported plan is checked against the injury and load gates, never executed blindly.** A spreadsheet written weeks in advance cannot know this morning's readiness or an open symptom flag. If a row prescribes intensity work during a block the athlete's own rules mark as base-building only, the system surfaces that conflict rather than relaying it as written. This is the §8.1 non-negotiable — injury gate outranks everything — applied to a new input source, not a new rule.
- **"What do I need to do today" query flow**, for a message like *"what do I need to do today?"*:
  1. Look up today's row in `plan`. If a nearby prior day's prescribed session has no matching COROS session and nothing was logged, that gap is surfaced first — *"did you get to yesterday's run?"* — because the answer changes what today should be, per the athlete's own miss-rules (skip one and resume; drop to 70% after two or three).
  2. Cross-check the prescription against today's computed readiness. Green relays the plan as written. Amber or red — especially an open injury flag — overrides the prescribed intensity and says so explicitly, rather than reciting a target written before today's data existed.
  3. Reply in the athlete's language, not the spreadsheet's — e.g. *"Bike, 3 hours. Keep it under 160 for the first two-forty, then open it up to 170–180 for the last twenty. Knee was clear yesterday, no restrictions today."* A strength day needs no zone logic — just the prescribed day and focus relayed directly.
- **Long-horizon fitness trend, deliberately separate from daily readiness.** Readiness (Phase 1) answers "is today okay?" against a 28-day rolling baseline — built to catch short-term fatigue. This is a slower, second signal: trailing pace or power achieved at a fixed average heart rate across Zone 2 sessions, tracked week over week; a multi-month slope on resting HR and HRV rather than a daily delta; VO2max trend where COROS reports it. This belongs in the weekly review, not the daily note — it's a "is the training working" question, answered on a timescale of months, and it's what eventually lets the system say something like *"Z2 pace at the same 130bpm has moved from 3.7 to 4.4mph over six weeks — the aerobic base is building"* — direct evidence of adaptation, independent of how any single day felt.
- **Plan revisions are scoped re-imports, not a one-time fixed upload.** Re-running the import against a new spreadsheet only overwrites `plan` rows for dates that haven't happened yet — anything already logged or already past stays as historical record, so the athlete can later see not just what happened, but what the plan originally said before a revision changed it. Each import carries a version stamp and a one-line reason (`agoge plan import file.xlsx --from 2027-03-01 --reason "behind on volume, cutting the block by 15%"`), which doubles as an entry in the Phase 3 decision log — the plan's own history becomes auditable, not just today's plan.
- **Day-to-day noise never requires a re-upload.** The readiness override two bullets up already absorbs the normal case — one bad night, one flare-up, one missed session. Re-importing is reserved for structural revisions: a multi-week stretch of falling behind or running ahead of the original plan, a race-date change, a deliberate shift in emphasis between blocks. Conflating the two would mean either re-uploading every time ordinary life happens, or never revising even once the plan has clearly gone stale.
- **A revision should be informed by agoge's own data, not drafted blind.** A new plan written in a separate context — a fresh conversation with no visibility into the athlete's actual training — is necessarily working from memory rather than evidence. The rolling profile (§2.2) already exists to summarize the last two months of real data into something portable; the same export is the right input to hand to whatever drafts the revision, so the new plan is anchored to demonstrated fitness rather than the original guess.
- **Sustained divergence should be surfaced, not just quietly tracked.** If checkpoint status or the load trend shows several consecutive weeks below planned volume, or the athlete consistently clearing checkpoints early, the weekly review is where that gets said explicitly — *"you've been at 70% of planned volume for three weeks; the current block's targets may not match where you actually are"* — rather than continuing to prescribe against a plan that has already stopped reflecting reality. Whether this only surfaces the observation or actively proposes a revision is the same decide-vs-suggest question already open in §9.

### Phase 2 — Conversational layer **[DESIGNED, not yet built]**

- **WhatsApp (or Telegram) as the primary interface.** Every CLI command becomes a text message; the athlete never opens a dashboard.
- **Conditional check-ins, not a fixed-time nag.** The system should only initiate contact when it has a question it can't answer itself — an unresolved overnight symptom, a planned session that never synced by evening. If nothing is outstanding, it says nothing. A daily "did you work out today?" sent regardless of context trains the athlete to stop reading it within two weeks.
- **Image ingest.** A photo sent over chat — gym cardio console, bathroom scale, a physio's handwritten notes — is downloaded, sent to a vision-capable model with an extraction prompt, and the structured result written to the same tables natural-language text logging uses. This is the only way to capture data from equipment with no export path (commercial gym bikes, treadmills in three different countries over the training cycle).
- **Voice note ingest.** Transcribe, then run the transcript through the same annotation pipeline as typed text. The value here is specifically post-session, hands-occupied logging — "that was rough, knee's a bit warm" said out loud while walking, versus never getting typed at all.

- **Standing preferences — distinct from, and not automatically inherited from, Claude.ai's own memory feature.** An API key gives agoge the same language understanding the athlete gets talking to Claude directly; it does not carry along Claude.ai's memory system, which is a separate product feature Anthropic runs specifically inside its own chat interface. If the athlete texts *"from now on, always include cadence when you tell me a run,"* that instruction needs somewhere to land and something reading it before every future run prescription — otherwise it's just words that vanish after the reply. Mechanism: incoming messages pass through a lightweight intent classifier first (data log / status query / plan query / standing instruction), and anything classified as the last category is appended to a small preferences store, loaded into context on every future response-generating call — the daily note, the "what do I do today" reply, the weekly review — the same way the rolling profile already is. Small addition, and it's the difference between a chat interface that only reacts and one that actually accumulates how the athlete wants to be talked to over the course of the block.

### Phase 3 — Intelligence layer **[DESIGNED, not yet built]**

- **Literature grounding, done carefully.** Not open-web RAG — the failure mode there is invisible (a confident answer built on a blog post). Instead: a curated corpus pulled from PubMed/Europe PMC, filtered toward systematic reviews and meta-analyses, with every stored chunk tagged by study type, sample size, and **population**. The population tag is the load-bearing part — sports-science findings are disproportionately drawn from trained, often elite athletes, and a finding from that population is not evidence about a deconditioned 19-year-old with a reconstructed knee. The agent states the mismatch explicitly whenever it applies. Hard rule, no exceptions: literature informs *why*, the athlete's own data decides *what*, and an open injury gate outranks any paper regardless of evidence quality.
- **n-of-1 sleep experiment mode**, in place of mining observational sleep data for "optimal" duration — which, on a sample of a few dozen sessions confounded by a strengthening fitness trend, a seasonal move schedule, and a nicotine taper running on the same calendar axis, would very likely produce a confident and wrong answer. Instead: randomize a nightly lights-out target between two values inside a healthy range for a defined block, let the agent assign each night's target, and analyze the result as a real (if small) controlled experiment. Randomization is what defeats the confounding that no amount of clever regression fixes on this sample size.
- **Calendar awareness** — reading the athlete's calendar so a session can be moved *before* a conflict rather than reported as missed after the fact. Probably the single highest-leverage feature for a student athlete, since most training plan failures for students are the plan assuming an empty week that was never empty.
- **Weather awareness** — flagging a dangerous training window (Austin heat, for instance) days ahead, not the morning of.
- **Shoe mileage tracking** — relevant here specifically because the athlete's ankle and knee history make training in a dead stability shoe a real, not theoretical, risk.
- **Automated overtraining detection** — the athlete's own documented warning-sign list (elevated resting HR, soreness past 72 hours, degrading sleep, dreading sessions), checked automatically rather than relying on self-recognition from inside a fatigued state.
- **Race pacing model** — projected swim/bike/run splits against the actual race cutoffs, updated as fitness data accumulates.
- **Decision log** — one line, every time the plan changes, on *why*. Makes the system's own judgment auditable after the fact, which matters both for trust and for catching a bad pattern before it repeats.

### Phase 4 — Multi-athlete / open source **[DESIGNED, partially open]**

- All personal specifics (zones, injuries, race, blocks) already live in one config file, so a second athlete is a second config file, a second server, a second private database — not a fork.
- **[OPEN]** What's shared between training partners versus private. Current recommendation: session occurrence (did they train, what discipline, how long) is shared; anything health-flagged (HRV, weight, symptoms) stays private to each athlete. Accountability without surveillance.
- A setup wizard and real onboarding docs, so a stranger can go from "cloned the repo" to "receiving nightly notes" without needing the original athlete's help.
- Explicit scope note in the public README: this gives training guidance to people with real injuries; it is not medical advice, injury gates default conservative, and it's offered as "this works for me, PRs welcome, no support promised" rather than a maintained product.

---

## 7. Known fragility — stated honestly, because a builder acting on false confidence wastes real time

- **COROS's MCP tool names are not documented publicly as a stable contract.** The integration discovers available tools at runtime and pattern-matches them to the roles it needs (activity list, daily wellness, sleep, fitness/VO2). This works, but is the single most likely thing to silently need a manual fix after any COROS-side update.
- **Cronometer has no official API at all.** The `cronosync` binary mimics the internal requests Cronometer's own web app makes, using several "magic values" that Cronometer changes without notice whenever they ship a frontend update. When that happens, login fails with correct credentials, and the fix requires manually re-capturing fresh values from the browser. The CSV-export fallback path exists specifically because this will happen.
- **Energy-availability thresholds are drawn from research on trained athletes**, applied here to someone at the very start of a conditioning block. Treat the numeric floor as a reasonable-guess alarm threshold, not a clinically validated cutoff for this specific athlete.

---

## 8. Non-negotiables for anyone building from this document

1. An open injury gate always outranks every other signal — training plan, literature, athlete's stated preference. No exception path.
2. The LLM is never the source of a number. If a value isn't already in the database, the model says so instead of estimating it into an answer.
3. Nutrition guidance operates as a floor alarm, never a deficit target shown to the athlete. This is a young athlete in an intentional deficit; a visible daily calorie scoreboard is a mechanic with real downside and no offsetting benefit the floor alarm doesn't already provide.
4. Sleep-outcome correlations are reported as descriptive statistics with sample size attached, never as a causal "your optimal sleep is X hours" claim, until a genuinely adequate sample or a randomized block exists.
5. Nothing health-specific — injury history, weight, symptom log — is ever committed to the public repository, under any circumstance, including example data that could be mistaken for real data.
6. A prescribed plan — whether authored by the weekly LLM call or bulk-imported from a spreadsheet — is a suggestion the deterministic layer is always allowed to override, never a fixed instruction relayed unmodified. A plan written in advance cannot see today's readiness; today's readiness wins.

---

## 9. Open questions before a full build-ready blueprint is written

- **[OPEN]** How much should the agent *decide* versus *suggest*? A system that silently moves Thursday's run to Wednesday because of a calendar conflict is a materially different product from one that always asks first. This choice affects the design of nearly every Phase 2–3 feature above.
- **[OPEN]** Exact chat platform: WhatsApp (needs a second phone number to avoid personal messages becoming agent input) versus Telegram (no phone number needed, slightly less "native" feeling).
- **[OPEN]** Whether literature grounding ships as a Phase 3 feature at all, or gets deferred indefinitely — it's the most build-effort-intensive item on this list relative to its impact on day-to-day training decisions, versus something like calendar awareness which is comparatively cheap and immediately useful.
- **[OPEN]** For the training partner: does he get his own copy of this exact system, or a deliberately simplified version? His plan has no abroad gap and presumably fewer injury constraints — the config already supports this, but the *coaching voice* (how directive vs. suggestive the agent is) may reasonably differ per athlete even on identical code.

---

*This document describes a system with a working, tested core (Phase 1) and a specified-but-unbuilt roadmap (Phases 2–4). A build-ready blueprint — with exact schemas, prompts, and file-by-file specs for the unbuilt phases — is the natural next artifact once the open questions above are resolved.*
