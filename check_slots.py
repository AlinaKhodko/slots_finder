#!/usr/bin/env python3
"""Check pasport.org.ua e-queue pages for free appointment slots.

Sends a Telegram notification when free slots become available.

Usage:
    python check_slots.py            # check all configured cities
    python check_slots.py --dry-run  # do everything except sending Telegram
    python check_slots.py -v         # verbose logging

Env vars:
    TELEGRAM_BOT_TOKEN   bot token from @BotFather
    TELEGRAM_CHAT_ID     chat id to send to (your user id, or a group/channel)
    NOTIFY_COOLDOWN_MIN  optional, minutes between repeat notifications (default 60)
    STATE_FILE           optional, path to JSON file holding per-city state
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import (
    Page,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)

# Load .env from the same directory as this script (so it works no matter
# what cwd you run it from). Real env vars — e.g. GitHub Actions secrets —
# still win over values in .env.
load_dotenv(Path(__file__).resolve().parent / ".env")

LOG = logging.getLogger("check-slots")

# URLs to check. Add / remove freely.
URLS: dict[str, str] = {
    "Kyiv": "https://ukraina.pasport.org.ua/solutions/e-queue",
    "Kortrijk": "https://kortrijk.pasport.org.ua/solutions/e-queue",
    "Cologne":  "https://cologne.pasport.org.ua/solutions/e-queue",
}

# Substrings (case-insensitive) that indicate NO slots are available.
# Confirmed wording from cologne.pasport.org.ua / kortrijk.pasport.org.ua.
NO_SLOTS_MARKERS: tuple[str, ...] = (
    "наразі всі місця зайняті",     # confirmed exact phrase
    "всі місця зайняті",
    "немає вільних місць",
    "вільні місця відсутні",
)

# Markers that indicate the registration form is visible — i.e. slots ARE
# available. Confirmed on kharkiv.pasport.org.ua: the page shows surname/
# first-name/phone fields and a "Продовжити" submit button only when there
# is at least one slot to book.
FORM_MARKERS: tuple[str, ...] = (
    "прізвище",
    "продовжити",
)

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
NOTIFY_COOLDOWN_MIN = int(os.environ.get("NOTIFY_COOLDOWN_MIN", "60"))


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            LOG.warning("State file corrupt, ignoring")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def classify(text: str) -> tuple[bool, str]:
    """Decide whether to notify, and explain why.

    Returns (notify, reason):
      * (False, "all-taken")    -> page shows "Наразі всі місця зайняті" or
                                    one of the fallback markers. Stay silent.
      * (True,  "form-visible") -> registration form is on screen → slots open.
      * (True,  "unparsed")     -> neither signal recognised → still notify so
                                    the user can check manually (better a false
                                    alarm than a missed opening).
    """
    lowered = text.lower()
    if any(marker in lowered for marker in NO_SLOTS_MARKERS):
        return False, "all-taken"
    if all(marker in lowered for marker in FORM_MARKERS):
        return True, "form-visible"
    return True, "unparsed"


def has_free_slots(text: str) -> bool:
    """Backwards-compatible boolean wrapper around `classify`."""
    notify, _ = classify(text)
    return notify


def fetch_rendered_text(page: Page, url: str) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    # Some pasport.org.ua pages poll in the background and never go fully
    # idle, so don't depend on networkidle. Wait for it briefly, then for the
    # booking widget content to appear — but don't block on it: if neither
    # the "all taken" message nor a calendar/button appears, we still read
    # whatever is on the page and apply the simple marker rule.
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PWTimeoutError:
        LOG.debug("networkidle timed out for %s — continuing", url)

    try:
        page.wait_for_function(
            """() => {
                const t = (document.body.innerText || '').toLowerCase();
                // No-slots message
                if (t.includes('наразі всі місця') ||
                    t.includes('всі місця зайняті') ||
                    t.includes('немає вільних') ||
                    t.includes('вільні місця відсутні')) return true;
                // Registration form visible (slots available)
                if (t.includes('прізвище') && t.includes('продовжити')) return true;
                return false;
            }""",
            timeout=30_000,
        )
    except PWTimeoutError:
        LOG.warning(
            "%s: neither 'all taken' message nor registration form "
            "appeared within 30s — reading the page anyway", url,
        )

    time.sleep(2)  # short grace period for any final paints
    return page.inner_text("body")


def send_telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        LOG.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping notify")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=30,
    )
    r.raise_for_status()
    LOG.info("Telegram notification sent")


def check(dry_run: bool = False, dump_dir: str | None = None) -> int:
    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat()
    dump_path = Path(dump_dir) if dump_dir else None
    if dump_path:
        dump_path.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            locale="uk-UA",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()

        for city, url in URLS.items():
            LOG.info("Checking %s ...", city)
            try:
                text = fetch_rendered_text(page, url)
            except Exception as exc:
                LOG.error("Failed to fetch %s: %s", city, exc)
                continue

            if dump_path:
                (dump_path / f"{city}.txt").write_text(text, encoding="utf-8")
                LOG.debug("Dumped %s page text to %s/%s.txt", city, dump_path, city)

            # Log the part of the body where the "all taken" message would
            # appear, so a false positive is easy to diagnose from the log.
            snippet = " | ".join(
                ln.strip() for ln in text.splitlines() if ln.strip()
            )[:400]
            LOG.debug("%s body snippet: %s", city, snippet)

            free, reason = classify(text)
            prev = state.get(city, {})
            LOG.info(
                "%s: free=%s reason=%s (previous free=%s)",
                city, free, reason, prev.get("free"),
            )

            if free:
                # Throttle: don't re-notify more often than NOTIFY_COOLDOWN_MIN
                last_notify = prev.get("last_notify")
                should_send = True
                if last_notify:
                    elapsed = (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(last_notify)
                    ).total_seconds() / 2.0
                    if elapsed < NOTIFY_COOLDOWN_MIN:
                        should_send = False
                        LOG.info(
                            "%s: notify cooldown active (%.1f / %d min)",
                            city, elapsed, NOTIFY_COOLDOWN_MIN,
                        )

                if should_send:
                    if reason == "form-visible":
                        why = "Видно форму реєстрації — імовірно, є вільні слоти."
                    else:  # unparsed
                        why = (
                            "Не зміг розпізнати сторінку (немає ні «Наразі "
                            "всі місця зайняті», ні форми) — перевір вручну."
                        )
                    msg = (
                        f"<b>\U0001F7E2 ПЕРЕВІР {city}!</b>\n"
                        f"{why}\n"
                        f'<a href="{url}">{url}</a>'
                    )
                    if dry_run:
                        # Dry runs do NOT update last_notify, so a real run
                        # right after can still fire a Telegram notification.
                        LOG.info("[DRY RUN] would send Telegram:\n%s", msg)
                    else:
                        try:
                            send_telegram(msg)
                            prev["last_notify"] = now_iso
                        except Exception as exc:
                            LOG.error("Telegram send failed: %s", exc)

            prev["free"] = free
            prev["last_checked"] = now_iso
            state[city] = prev

        browser.close()

    save_state(state)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check pasport.org.ua e-queue pages for free slots.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run normally but don't actually call Telegram.",
    )
    parser.add_argument(
        "--dump-text",
        metavar="DIR",
        default=None,
        help="Also write each city's rendered text to <DIR>/<city>.txt "
        "(debug aid — useful for tuning NO_SLOTS_MARKERS).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    sys.exit(check(dry_run=args.dry_run, dump_dir=args.dump_text))


if __name__ == "__main__":
    main()
