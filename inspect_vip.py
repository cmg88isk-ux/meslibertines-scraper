"""Dump the real "Abonnement" HTML of a premium account's dashboard.

The VIP-days parser (meslibertines_profile.detect_vip_days) was written against a
guessed wording ("Expire le JJ/MM/AAAA"). A live premium account is the only way
to see what the site actually prints, so this logs in with one account and writes
every HTML region that mentions the subscription to output/vip_dump/.

Read-only: it does not touch output/results*; it only writes under
output/vip_dump/ so a live checker run is not disturbed.

Usage:
    python inspect_vip.py --user lucia.18 --password '1987@@'
    python inspect_vip.py --user lucia.18 --password '1987@@' --provider kernel
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import sys
from pathlib import Path

import httpx

import login_meslibertines as ML
import meslibertines_profile as mp
from config import load_dotenv, provider_keys
from hygiene import apply_identity
from transports import KernelTransport, open_browser

ROOT = Path(__file__).resolve().parent
BASE_URL = "https://www.meslibertines.com"
DUMP_DIR = ROOT / "output" / "vip_dump"

# Keywords that probably sit next to the package expiry / day count.
HINTS = (
    "abonnement",
    "package",
    "premium",
    "expire",
    "expir",
    "jour",
    "restant",
    "jusqu",
    "valable",
    "duree",
    "durée",
    "fin ",
    "date",
)


def snippets(html: str, *, radius: int = 260) -> list[str]:
    """Windows of raw HTML around every hint, deduped."""
    low = html.lower()
    seen: list[str] = []
    for hint in HINTS:
        start = 0
        while True:
            idx = low.find(hint, start)
            if idx == -1:
                break
            window = html[max(0, idx - radius) : idx + radius]
            if window not in seen:
                seen.append(window)
            start = idx + len(hint)
    return seen


async def dump_for(user: str, password: str, provider: str) -> int:
    keys = provider_keys({"kernel": "KERNEL_API_KEY", "browserbase": "BROWSERBASE_API_KEY"}[provider])
    if not keys:
        print(f"no {provider} key in .env", file=sys.stderr)
        return 2
    transport = KernelTransport(keys) if provider == "kernel" else None
    if transport is None:
        from transports import BrowserbaseTransport

        transport = BrowserbaseTransport(keys)

    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30.0) as http:
        await http.get(BASE_URL)

    async with open_browser(transport, keys[0]) as browser:
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
        page.set_default_timeout(60_000)
        with contextlib.suppress(Exception):
            await apply_identity(browser)

        await page.goto(ML.LOGIN_URL, wait_until="domcontentloaded", timeout=120_000)
        if not await ML.clear_challenge(page, manual=False):
            print("challenge non resolu", file=sys.stderr)
            return 3
        for _ in range(10):
            if await ML.find_username_field(page):
                break
            await page.wait_for_timeout(1_000)
        if not await ML.attempt_login(page, user, password):
            print("login refuse", file=sys.stderr)
            return 2
        print(f"connecte: {page.url}")

        landing = page.url
        if "/profiles" in landing:
            dash = landing
        elif "/multi_dashes" in landing:
            dash = landing
        else:
            dash = BASE_URL + "/member_dashes/index/"

        html = ""
        for attempt in range(3):
            await page.goto(dash, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(2_500)
            html = await page.content()
            if mp.detect_premium(html) == mp.PREMIUM_YES:
                break
        premium = mp.detect_premium(html)
        print(f"dash={dash} premium={premium!r}")
        print(f"vip_jours (parser actuel) = {mp.detect_vip_days(html)!r}")
        print(f"vip_expiry (parser actuel) = {mp.detect_vip_expiry(html)!r}")

        (DUMP_DIR / "dash.html").write_text(html, encoding="utf-8")
        found = snippets(html)
        (DUMP_DIR / "snippets.txt").write_text("\n\n---\n\n".join(found), encoding="utf-8")
        text = await page.inner_text("body")
        (DUMP_DIR / "dash.txt").write_text(text, encoding="utf-8")
        print(f"\n{len(found)} region(s) ecrite(s) -> {DUMP_DIR}")
        for region in found[:12]:
            clean = re.sub(r"\s+", " ", region).strip()
            print(f"\n  ...{clean[:400]}...")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--provider", default="kernel", choices=("kernel", "browserbase"))
    args = parser.parse_args()
    load_dotenv(str(ROOT / ".env"))
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(dump_for(args.user, args.password, args.provider))


if __name__ == "__main__":
    sys.exit(main())
