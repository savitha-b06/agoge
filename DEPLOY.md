# Deploying agoge — step by step

Read this top to bottom once before starting. Total time is about 90 minutes,
most of it waiting. Nothing here is irreversible.

Order matters: each phase is useful on its own, and if you stop after any of
them you still have something working.

---

## Phase 0 — Five minutes, no VPS, do this first

Before any servers, connect COROS directly to Claude and live with it for a few
days. If this alone is enough, you've saved yourself $5/month and a weekend.

1. Open the Claude **desktop app** (not the browser).
2. Settings → Connectors → Add Custom Connector.
3. URL: `https://mcp.coros.com/mcp`
4. Log in with your COROS account when prompted, then Save.
5. Test it: *"Show me my workouts from the past two weeks."*

If real numbers come back, you're connected. Requires a paid Claude plan.

**Stop here for a few days.** Everything below is about automating this, and
automation is only worth it once you know you want the thing being automated.

---

## Phase 1 — The server

### 1.1 Create it

[Hetzner Cloud](https://www.hetzner.com/cloud) is the value pick. Sign up,
create a project, then create a server:

- Location: Ashburn or Hillsboro (US)
- Image: **Ubuntu 24.04**
- Type: **CX22** (2 vCPU, 4GB) — about €4/month, far more than you need
- SSH key: add one (see below)
- Name: `agoge`

If you don't have an SSH key, on your laptop:

```bash
ssh-keygen -t ed25519 -C "agoge"
cat ~/.ssh/id_ed25519.pub
```

Paste that output into Hetzner's SSH key box. **Do not use password login.**

Write down the server's IP address.

### 1.2 Connect and set the timezone

```bash
ssh root@YOUR_SERVER_IP
timedatectl set-timezone America/Chicago
```

Change this when you move: `America/Chicago` now, `America/Chicago` in Nashville
too, `Europe/Madrid` in January, `Asia/Tokyo` in March. Cron uses server time, so
if you forget, your 4am job runs at a strange hour.

---

## Phase 2 — Install agoge

### 2.1 Push the code to GitHub first

On your **laptop**, in the `agoge` folder I gave you:

```bash
cd agoge
git init
git add -A
git commit -m "agoge v0.1"
gh repo create agoge --public --source=. --push
```

Before you run this, confirm what's actually being committed:

```bash
git status --short
```

`athlete.yaml` must **not** appear in that list. If it does, stop — the
`.gitignore` isn't being picked up, and that file has your injury history in it.

### 2.2 Run the setup script

From your laptop:

```bash
ssh root@YOUR_SERVER_IP 'bash -s' < deploy/setup_vps.sh
```

Edit `deploy/setup_vps.sh` first and change `YOURNAME` to your GitHub username.

This creates an `agoge` user, installs Python, clones your repo, builds a
virtualenv, and enables a firewall that allows only SSH.

### 2.3 Configure

```bash
ssh root@YOUR_SERVER_IP
su - agoge
cd ~/agoge
nano .env
```

Set `ANTHROPIC_API_KEY`. Get one at console.anthropic.com — this is separate
from your Claude subscription and is billed per token. Expect well under
$5/month at this volume.

Then your athlete config:

```bash
nano athlete.yaml
```

I've pre-filled yours: zones off your 196 max HR, both injuries with the
overnight rule, all six training blocks through to race week, and your
checkpoints. Read it once and correct anything that's drifted.

```bash
chmod 600 .env athlete.yaml
```

### 2.4 Check it runs

```bash
~/agoge/.venv/bin/agoge status
```

Empty database, so it'll be sparse. No traceback means you're fine.

---

## Phase 3 — COROS

```bash
~/agoge/.venv/bin/agoge coros tools
```

**This is where you're most likely to hit a wall**, and it's worth knowing why
before it happens. COROS's MCP uses OAuth, which normally wants a browser — and
your server doesn't have one. Two paths:

**If it prints a URL:** copy it into your laptop's browser, log in, and paste
the resulting code back into the terminal.

**If it fails on auth:** do the OAuth flow on your laptop instead, then copy the
cached token up:

```bash
# on your laptop, with agoge installed locally
agoge coros tools
scp data/.coros_tokens.json agoge@YOUR_SERVER_IP:~/agoge/data/
```

Either way, once `agoge coros tools` prints a list of tool names, run:

```bash
~/agoge/.venv/bin/agoge coros discover
```

That caches the tool-name mapping. If some roles come back unmatched, open
`data/coros_tools.json`, look at `all_tools`, and fill in the right names by
hand. This is the one part I couldn't test for you — COROS hasn't published
stable tool names, so the discovery is pattern-matching against guesses.

Then pull a real day:

```bash
~/agoge/.venv/bin/agoge nightly --date yesterday
```

You should get a paragraph about your training. **That's the system working.**

### Backfill

```bash
for i in $(seq 1 30); do
  ~/agoge/.venv/bin/agoge nightly --date $(date -d "$i days ago" +%F)
  sleep 2
done
```

Then build the profile:

```bash
~/agoge/.venv/bin/agoge profile --rebuild
cat ~/agoge/data/athlete_profile.md
```

---

## Phase 4 — Cronometer (optional, do it last)

Skip this until Phase 3 works. It's the most fragile piece.

### 4.1 Build cronosync

```bash
sudo apt-get install -y golang-go
cd ~ && git clone https://github.com/YOURNAME/cronosync && cd cronosync
go mod tidy
go build -o cronosync .
mkdir -p ~/bin && mv cronosync ~/bin/
```

`go mod tidy` may complain about function signatures — the gocronometer README
documents the calls but not their exact types. If it does, run
`go doc github.com/jrmycanady/gocronometer` and adjust `main.go` to match.

### 4.2 Test it immediately

```bash
export CRONOMETER_USERNAME='you@example.com'
export CRONOMETER_PASSWORD='...'
~/bin/cronosync -start $(date -d yesterday +%F)
```

**If login fails with correct credentials, the GWT magic values are stale.**
That's expected — the library's last release was October 2025. Capture fresh
ones per the gocronometer README and set `CRONOMETER_GWT_PERMUTATION` and
`CRONOMETER_GWT_HEADER`.

If you don't want to deal with that today, use the manual path instead:
Cronometer → Settings → Account → Export Data, then

```bash
~/agoge/.venv/bin/agoge nutrition import --file ~/cronometer-export.csv
```

That path cannot break.

### 4.3 Wire it in

Add your Cronometer credentials to `~/agoge/.env`, then:

```bash
~/agoge/.venv/bin/agoge nutrition sync --date yesterday
~/agoge/.venv/bin/agoge nutrition show
```

**Use a unique password for Cronometer.** This is full account access sitting in
a file on a rented server, not a scoped token you can revoke.

---

## Phase 5 — Automate

```bash
crontab -e
```

Paste the contents of `deploy/crontab.example`. Then check the logs tomorrow:

```bash
cat ~/agoge/data/logs/nightly.log
```

---

## Phase 6 — Daily use

```bash
agoge status                          # morning: readiness and what to do
agoge log "knee fine, prehab done"    # after a session
agoge sessions                        # recent training
agoge weekly                          # Sunday
agoge physio --since 2026-07-01       # before your late-August appointment
```

Add this to your laptop's `~/.zshrc` so you can run it from anywhere:

```bash
alias agoge='ssh agoge@YOUR_SERVER_IP "~/agoge/.venv/bin/agoge"'
```

Chat comes next. Every command already exists — WhatsApp is a wrapper around
this, not a rewrite.

---

## When something breaks

| Symptom | Cause | Fix |
|---|---|---|
| `No athlete config` | `athlete.yaml` missing | `cp athlete.example.yaml athlete.yaml` |
| `ANTHROPIC_API_KEY not set` | `.env` not loaded | Run from `~/agoge`, check the file |
| `No COROS tool matched role` | Pattern didn't match | Edit `data/coros_tools.json` by hand |
| Cronometer login fails | Stale GWT values | Set the two env overrides, or import CSV |
| Cron silent | Wrong paths | Cron has no PATH — use absolute paths |
| Timezone wrong after travel | Server clock | `sudo timedatectl set-timezone ...` |

Back up before you fiddle: `cp ~/agoge/data/agoge.db ~/agoge.db.bak`

---

## Running costs

| | |
|---|---|
| Hetzner CX22 | ~€4/mo |
| Anthropic API | $2–5/mo |
| Cronometer Gold (if you don't have it) | ~$5/mo |
| **Total** | **~$10–15/mo** |

---

## Where to stop

If your session budget runs out mid-way, Phase 0 alone is genuinely useful and
takes five minutes. Phases 1–3 give you the full nightly loop. Phase 4 is a
nice-to-have on a fragile foundation.

Do not let building this replace training. You have three runs a week and
thirteen days to your Phase 1 checkpoints.
