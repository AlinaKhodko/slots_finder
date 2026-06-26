# pasport.org.ua e-queue slot checker

Polls three `pasport.org.ua` e-queue pages and sends a Telegram message when
free appointment slots appear:

- Kharkiv – https://kharkiv.pasport.org.ua/solutions/e-queue
- Kortrijk – https://kortrijk.pasport.org.ua/solutions/e-queue
- Cologne – https://cologne.pasport.org.ua/solutions/e-queue

The pages are client-rendered, so the script drives a headless Chromium via
Playwright, waits for the booking widget to load, and looks for the
"no free slots" message (and conversely, dates / `HH:MM` time slots).

## 1. Get a Telegram bot token + chat id

1. Open Telegram, talk to [@BotFather](https://t.me/BotFather) → `/newbot` →
   pick a name → copy the token it gives you (looks like `123456:ABC...`).
2. Get your numeric chat id: message [@userinfobot](https://t.me/userinfobot)
   or [@getidsbot](https://t.me/getidsbot). For a group, add the bot to the
   group and use the group's id (starts with `-100...` for supergroups).
3. Send `/start` to your new bot once so it is allowed to message you.

## 2. Run locally

macOS / Linux:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env                 # then edit .env with your real values

# Sanity-check Telegram credentials BEFORE running the script:
python -c "from dotenv import load_dotenv; import os, requests; load_dotenv(); \
  r = requests.post(f\"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage\", \
  data={'chat_id': os.getenv('TELEGRAM_CHAT_ID'), 'text': 'hello from check_slots'}); \
  print(r.status_code, r.text)"

python check_slots.py --dry-run -v               # parses pages, doesn't notify
python check_slots.py --dry-run -v --dump-text ./dump   # also save page text
python check_slots.py -v                          # real run, will notify
```

Windows (PowerShell):

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium

# Set env vars manually for this shell:
$env:TELEGRAM_BOT_TOKEN = "..."
$env:TELEGRAM_CHAT_ID   = "..."

python check_slots.py --dry-run -v
```

`state.json` is written next to the script and remembers per-city last status
plus the timestamp of the last notification so you don't get spammed. Set
`NOTIFY_COOLDOWN_MIN` to change the throttle (default 60 minutes). Dry runs
do **not** update the cooldown, so you can `--dry-run` freely.

## 3. Run on GitHub Actions

1. Push this repo to GitHub.
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. The workflow at `.github/workflows/check-slots.yml` runs every 5 minutes
   (and on manual `workflow_dispatch`). State is persisted between runs via
   `actions/cache`.

Note: GitHub's `schedule` triggers commonly run a few minutes late under load,
and won't run more frequently than once a minute. If you need tighter polling,
run it on a small VM or your own machine via `cron` / `launchd`.

## 4. Tuning the detector

`has_free_slots()` in `check_slots.py` decides per-city like this:

1. If any string in `NO_SLOTS_MARKERS` appears → **no slots**.
2. Otherwise, if **≥3** distinct `HH:MM` times are present → **slots available**.
3. Otherwise, if **≥2** `DD <ukrainian-month>` date headers are present →
   **slots available**.
4. Else → no slots (conservative — we'd rather miss than spam).

To tune it against the live site, run with `--dump-text ./dump`:

```bash
python check_slots.py --dry-run -v --dump-text ./dump
```

That writes `./dump/Kharkiv.txt`, `./dump/Kortrijk.txt`, `./dump/Cologne.txt` —
exactly what the script saw. Open the file for a city that you know has no
slots and copy its real "no available slots" phrasing into the
`NO_SLOTS_MARKERS` tuple at the top of `check_slots.py`.

## 5. Adding / removing cities

Edit the `URLS` dict at the top of `check_slots.py`. Any subdomain of
`pasport.org.ua` exposing `/solutions/e-queue` should work — for example
`berlin`, `warszawa`, `prague`, `london`, `milan`.
